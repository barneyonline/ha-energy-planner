"""Bounded climate learning with chronological validation and no HA I/O."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from math import ceil
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from .climate_inputs import finite, instant
from .climate_models import (
    BASELINE_VERSION,
    HISTORY_DAYS,
    MAX_ENERGY_ERROR,
    MAX_OBSERVATIONS,
    MAX_TEMPERATURE_MAE,
    MAX_TEMPERATURE_P90,
    MIN_ACTIVE_EPISODES,
    MIN_ACTIVE_RECALL,
    MIN_HISTORY_DAYS,
    MIN_STATE_ACCURACY,
    MIN_VALIDATION_WINDOWS,
    PHYSICAL_VERSION,
    VALIDATION_VERSION,
    BaselinePrediction,
    ValidationResult,
)
from .models import DecisionContext

MIN_NEIGHBOURS = 5
MAX_NEIGHBOURS = 32
MIN_PHYSICAL_SAMPLES = 20
NORMAL_TEMPERATURE_RESOLUTION_C = 0.25


def quantile(values: list[float], fraction: float) -> float:
    """Deterministic empirical nearest-rank quantile."""
    ordered = sorted(values)
    return ordered[min(max(ceil(len(ordered) * fraction) - 1, 0), len(ordered) - 1)] if ordered else 0.0


def observe(state: dict[str, Any], context: DecisionContext, rest_minutes: int) -> dict[str, Any]:
    """Collect five-minute measurements, explicitly marking intervention washout."""
    identity = context.climate_inputs.get("identity")
    comfort_signature = json.dumps(
        [
            context.occupied_temperature_low_c,
            context.occupied_temperature_high_c,
            {
                key: {field: room.get(field) for field in ("low", "high", "maximum_humidity")}
                for key, room in context.climate_inputs.get("zones", {}).items()
            },
        ],
        sort_keys=True,
        default=str,
    )

    if state.get("identity") != identity or state.get("comfort_signature") != comfort_signature:
        state = {"identity": identity, "ever_active": bool(state.get("ever_active"))}
    else:
        state = dict(state)
    state["comfort_signature"] = comfort_signature
    now = context.created_at
    bucket = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    owned = bool(context.hvac_control and set(context.hvac_control) != {"released_until"})
    manual = any(
        o.kind == "manual_hvac" and (o.expires_at is None or now < o.expires_at) for o in context.active_overrides
    )
    if owned or manual:
        state["normal_after"] = (now + timedelta(minutes=rest_minutes)).isoformat()
    normal_after = instant(state.get("normal_after"))
    provenance = (
        "planner" if owned else "manual" if manual else ("washout" if normal_after and now < normal_after else "normal")
    )
    rows = [
        dict(row)
        for row in state.get("observations", [])
        if isinstance(row, dict) and (at := instant(row.get("at"))) and now - timedelta(days=HISTORY_DAYS) <= at <= now
    ]
    last = instant(rows[-1].get("at")) if rows else None
    # A command or manual change inside an already sampled bucket contaminates
    # the whole interval, even though its first sample preceded the intervention.
    if last == bucket and provenance != "normal":
        rows[-1]["provenance"] = provenance
    values = [
        context.current_hvac_temperature_c,
        context.current_outdoor_temperature_c,
        context.current_hvac_power_kw,
        context.occupied_temperature_low_c,
        context.occupied_temperature_high_c,
    ]
    if (
        all(finite(value) is not None for value in values)
        and not context.input_issues
        and context.current_hvac_mode in {"heat", "cool", "off"}
        and str(context.occupancy_state) in {"occupied", "away"}
        and (last is None or bucket - last >= timedelta(minutes=5))
    ):
        rows.append(
            {
                "at": bucket.isoformat(),
                "temperature": values[0],
                "outdoor": values[1],
                "power_kw": values[2],
                "low": values[3],
                "high": values[4],
                "mode": context.current_hvac_mode,
                "occupied": str(context.occupancy_state),
                "target": context.climate_inputs.get("target"),
                "provenance": provenance,
                "humidity": context.climate_inputs.get("humidity"),
                "outdoor_humidity": context.climate_inputs.get("outdoor_humidity"),
                "irradiance": context.climate_inputs.get("irradiance"),
                "zones": context.climate_inputs.get("zones", {}),
            }
        )
    state["observations"] = rows[-MAX_OBSERVATIONS:]
    finish_observation(state, rows, now)
    return state


def neighbours(rows: list[dict[str, Any]], sample: dict[str, Any], timezone: str) -> BaselinePrediction | None:
    """Match only earlier, normal-operation measurements in comparable conditions."""
    at = instant(sample.get("at"))
    if at is None:
        return None
    local = at.astimezone(ZoneInfo(timezone))
    matches: list[tuple[float, dict[str, Any]]] = []
    sample = {**sample, "temperature": normal_temperature(float(sample["temperature"]))}
    for row in normal_history_for_slot(rows, sample, timezone):
        other = instant(row["at"])
        assert other is not None
        other = other.astimezone(ZoneInfo(timezone))
        minutes = abs((other.hour * 60 + other.minute) - (local.hour * 60 + local.minute))
        minutes = min(minutes, 1440 - minutes)
        outdoor = abs(float(row["outdoor"]) - float(sample["outdoor"]))
        relative = abs(
            (float(row["temperature"]) - float(row["low"])) - (float(sample["temperature"]) - float(sample["low"]))
        )
        if relative <= 2:
            matches.append((minutes / 90 + outdoor / 5 + relative / 2, row))
    matches.sort(key=lambda pair: (pair[0], str(pair[1]["at"])))
    selected = matches[:MAX_NEIGHBOURS]
    if len(selected) < MIN_NEIGHBOURS:
        return None
    weights = [1 / (0.1 + distance) for distance, _ in selected]
    powers = [max(float(row["power_kw"]), 0.0) for _, row in selected]
    target_pairs = [(weight, finite(row.get("target"))) for weight, (_, row) in zip(weights, selected, strict=True)]
    targets = [(weight, value) for weight, value in target_pairs if value is not None]
    return BaselinePrediction(
        sum(w * p for w, p in zip(weights, powers, strict=True)) / sum(weights),
        quantile(powers, 0.9),
        sum(w * t for w, t in targets) / sum(w for w, _ in targets) if targets else None,
        sum(w for w, p in zip(weights, powers, strict=True) if p >= 0.1) / sum(weights),
        len(selected),
        max(
            {str(row["mode"]) for _, row in selected},
            key=lambda mode: (
                sum(w for w, (_, row) in zip(weights, selected, strict=True) if row["mode"] == mode),
                mode,
            ),
        ),
    )


def ridge(features: list[list[float]], targets: list[float]) -> list[float] | None:
    """Solve a small regularized normal system using pivoted elimination."""
    if not features or len(features) != len(targets):
        return None
    width = len(features[0])
    matrix = [
        [sum(row[i] * row[j] for row in features) + (0.01 if i == j else 0.0) for j in range(width)]
        + [sum(row[i] * y for row, y in zip(features, targets, strict=True))]
        for i in range(width)
    ]
    for column in range(width):
        pivot = max(range(column, width), key=lambda index: abs(matrix[index][column]))
        matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        scale = matrix[column][column]
        if abs(scale) < 1e-12:
            return None
        matrix[column] = [value / scale for value in matrix[column]]
        for index in range(width):
            if index != column:
                factor = matrix[index][column]
                matrix[index] = [a - factor * b for a, b in zip(matrix[index], matrix[column], strict=True)]
    result = [row[-1] for row in matrix]
    return result if all(finite(value) is not None for value in result) else None


def physical_features(row: dict[str, Any], *, enhanced: bool = False) -> list[float]:
    """Temperature gradient and electrical power, optionally solar and active zones."""
    result = [float(row["outdoor"]) - float(row["temperature"]), float(row["power_kw"])]
    if enhanced:
        result.extend(
            [
                float(row.get("irradiance") or 0) / 1000,
                sum(bool(zone.get("enabled")) for zone in row.get("zones", {}).values()) / 16,
            ]
        )
    return result


def fit_physical(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fit bounded responses using valid same-regime measurement pairs."""
    result: dict[str, Any] = {"version": PHYSICAL_VERSION}
    pairs: dict[str, list[tuple[dict[str, Any], float]]] = {"heat": [], "cool": [], "off": []}
    for left, right in zip(rows, rows[1:], strict=False):
        start, end = instant(left.get("at")), instant(right.get("at"))
        if start is None or end is None:
            continue
        hours = (end - start).total_seconds() / 3600
        mode = str(left.get("mode")) if float(left["power_kw"]) >= 0.1 else "off"
        other_mode = str(right.get("mode")) if float(right["power_kw"]) >= 0.1 else "off"
        if mode not in pairs or mode != other_mode or not 5 / 60 <= hours <= 0.5:
            continue
        rate = (float(right["temperature"]) - float(left["temperature"])) / hours
        if abs(rate) <= 6:
            pairs[mode].append((left, rate))
    for mode, samples in pairs.items():
        if len(samples) < MIN_PHYSICAL_SAMPLES:
            continue
        cut = max(1, int(len(samples) * 0.8))
        train, held = samples[:cut], samples[cut:]
        best: dict[str, Any] | None = None
        for enhanced in (False, True):
            if enhanced and any(row.get("irradiance") is None for row, _ in samples):
                continue
            coefficients = ridge(
                [physical_features(row, enhanced=enhanced) for row, _ in train], [rate for _, rate in train]
            )
            if coefficients is None or not 0 <= coefficients[0] <= 3:
                continue
            if mode == "heat" and not 0 < coefficients[1] <= 10:
                continue
            if mode == "cool" and not -10 <= coefficients[1] < 0:
                continue
            residuals = [
                rate - sum(c * x for c, x in zip(coefficients, physical_features(row, enhanced=enhanced), strict=True))
                for row, rate in held
            ]
            error = mean(abs(value) for value in residuals)
            if best is None or error < best["error"] * 0.95:
                best = {
                    "coefficients": coefficients,
                    "enhanced": enhanced,
                    "error": error,
                    "residuals": residuals[-100:],
                    "samples": len(samples),
                    "outdoor_low": min(float(row["outdoor"]) for row, _ in samples),
                    "outdoor_high": max(float(row["outdoor"]) for row, _ in samples),
                    "active_power": mean(float(row["power_kw"]) for row, _ in samples),
                }
        if best is not None:
            result[mode] = best
    return result


def temperature_step(model: dict[str, Any], row: dict[str, Any], hours: float, residual: float = 0.0) -> float | None:
    """Predict only within learned outdoor support; no uncontrolled extrapolation."""
    mode = str(row["mode"]) if float(row["power_kw"]) >= 0.1 else "off"
    fit = model.get(mode)
    if (
        not fit
        or (fit.get("enhanced") and row.get("irradiance") is None)
        or not fit["outdoor_low"] <= float(row["outdoor"]) <= fit["outdoor_high"]
    ):
        return None
    rate = sum(
        c * value
        for c, value in zip(fit["coefficients"], physical_features(row, enhanced=fit["enhanced"]), strict=True)
    )
    return float(float(row["temperature"]) + (rate + residual) * hours)


def validate(rows: list[dict[str, Any]], mode: str, timezone: str, window_minutes: int) -> ValidationResult:
    """Roll forward complete nonoverlapping windows, never fitting on their outcomes."""
    errors: list[float] = []
    actual_energy = predicted_energy = energy_absolute_error = 0.0
    correct = states = active = recalled = episodes = windows = 0
    last_window_at = None
    previous_active_end: datetime | None = None
    normal = [row for row in rows if row.get("provenance") == "normal"]
    days = {str(row["at"])[:10] for row in normal}
    cutoff: datetime | None = None
    fit_cache: dict[str, dict[str, Any]] = {}
    first_validation = max(len(normal) - 28 * 288, 0)
    for index in range(first_validation, len(normal)):
        start = normal[index]
        at = instant(start["at"])
        if at is None or (cutoff and at < cutoff) or start["mode"] != mode:
            continue
        # Train on preceding complete days, with the latest day held out.
        train = [row for row in rows if str(row["at"])[:10] < str(start["at"])[:10]]
        if len({str(row["at"])[:10] for row in train if row.get("provenance") == "normal"}) < 7:
            continue
        day = str(start["at"])[:10]
        if day not in fit_cache:
            fit_cache[day] = fit_physical(train)
        model = fit_cache[day]
        trajectory = [start]
        end = at + timedelta(minutes=max(window_minutes, 30))
        for row in normal[index + 1 :]:
            row_at, previous_at = instant(row["at"]), instant(trajectory[-1]["at"])
            if row_at is None or previous_at is None or row_at - previous_at != timedelta(minutes=5):
                break
            trajectory.append(row)
            if row_at >= end:
                break
        if instant(trajectory[-1]["at"]) != end:
            continue
        temperature = float(start["temperature"])
        predictions: list[tuple[float, float, float]] = []
        window_errors: list[float] = []
        for observed in trajectory[:-1]:
            sample = {**observed, "temperature": temperature, "mode": mode, "occupied": start["occupied"]}
            predicted = neighbours(train, sample, timezone)
            if predicted is None:
                break
            sample["power_kw"] = predicted.power_kw
            sample["mode"] = predicted.mode
            next_temperature = temperature_step(model, sample, 5 / 60)
            if next_temperature is None:
                break
            temperature = next_temperature
            window_errors.append(temperature - float(trajectory[len(predictions) + 1]["temperature"]))
            predictions.append((predicted.power_kw, float(observed["power_kw"]), predicted.active_fraction))
        if len(predictions) != len(trajectory) - 1:
            continue
        errors.extend(window_errors)
        windows += 1
        last_window_at = end.isoformat()
        window_active = False
        energy_absolute_error += abs(sum(forecast - observed for forecast, observed, _ in predictions)) * 5 / 60
        for forecast, observed_power, active_fraction in predictions:
            predicted_energy += forecast * 5 / 60
            actual_energy += observed_power * 5 / 60
            is_active = observed_power >= 0.1
            forecast_active = active_fraction >= 0.5
            correct += is_active == forecast_active
            states += 1
            active += is_active
            recalled += is_active and forecast_active
            window_active |= is_active
        if window_active and previous_active_end != at:
            episodes += 1
        previous_active_end = end if window_active else None
        cutoff = end
    mae = mean(abs(value) for value in errors) if errors else 999.0
    p90 = quantile([abs(value) for value in errors], 0.9) if errors else 999.0
    energy_error = energy_absolute_error / actual_energy if actual_energy > 0 else 999.0
    accuracy = correct / states if states else 0.0
    recall = recalled / active if active else 0.0
    blockers = tuple(
        name
        for name, failed in (
            ("history_days", len(days) < MIN_HISTORY_DAYS),
            ("validation_windows", windows < MIN_VALIDATION_WINDOWS),
            ("active_episodes", episodes < MIN_ACTIVE_EPISODES),
            ("temperature_mae", mae > MAX_TEMPERATURE_MAE),
            ("temperature_p90", p90 > MAX_TEMPERATURE_P90),
            ("energy_error", energy_error > MAX_ENERGY_ERROR),
            ("state_accuracy", accuracy < MIN_STATE_ACCURACY),
            ("active_recall", recall < MIN_ACTIVE_RECALL),
        )
        if failed
    )
    return ValidationResult(
        mode,
        len(days),
        windows,
        episodes,
        mae,
        p90,
        energy_error,
        accuracy,
        recall,
        blockers,
        tuple(errors[-100:]),
        last_window_at,
    )


def train_climate(state: dict[str, Any], timezone: str, window_minutes: int, now: datetime) -> dict[str, Any]:
    """Detached daily training; caller publishes only for its current identity."""
    rows = state.get("observations", [])
    result: dict[str, Any] = {
        "identity": state.get("identity"),
        "baseline_version": BASELINE_VERSION,
        "validation_version": VALIDATION_VERSION,
        "trained_at": now.isoformat(),
        "physical": fit_physical(rows),
        "humidity": fit_humidity(rows),
        "rooms": fit_rooms(rows, timezone, window_minutes),
        "validation": {},
        "normal": [r for r in rows if r.get("provenance") == "normal"],
    }
    for mode in ("heat", "cool"):
        result["validation"][mode] = asdict(validate(rows, mode, timezone, window_minutes))
    return result


def fit_humidity(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Learn relative humidity drift, retaining held-out residuals in %RH/hour."""
    features: list[list[float]] = []
    targets: list[float] = []
    use_outdoor = bool(rows) and all(finite(row.get("outdoor_humidity")) is not None for row in rows)
    for left, right in zip(rows, rows[1:], strict=False):
        start, end = instant(left.get("at")), instant(right.get("at"))
        before, after = finite(left.get("humidity")), finite(right.get("humidity"))
        if start is None or end is None or before is None or after is None:
            continue
        hours = (end - start).total_seconds() / 3600
        if not 5 / 60 <= hours <= 0.5 or left["mode"] != right["mode"]:
            continue
        features.append([1.0, float(left["temperature"]) / 30, float(left["power_kw"]), before / 100])
        if use_outdoor:
            features[-1].append((float(left["outdoor_humidity"]) - before) / 100)
        targets.append((after - before) / hours)
    if len(features) < MIN_PHYSICAL_SAMPLES:
        return {}
    cut = int(len(features) * 0.8)
    coefficients = ridge(features[:cut], targets[:cut])
    if coefficients is None:
        return {}
    residuals = [
        y - sum(c * x for c, x in zip(coefficients, row, strict=True))
        for row, y in zip(features[cut:], targets[cut:], strict=True)
    ]
    return {
        "coefficients": coefficients,
        "residual": quantile([abs(value) for value in residuals], 0.9),
        "ready": mean(abs(value) for value in residuals) <= 5,
        "outdoor": use_outdoor,
        "samples": len(features),
    }


def fit_rooms(rows: list[dict[str, Any]], timezone: str = "UTC", window_minutes: int = 30) -> dict[str, Any]:
    """Fit each room against shared measured power, without adding zone power loads."""
    result: dict[str, Any] = {}
    entities = sorted({entity for row in rows for entity in row.get("zones", {})})
    for entity in entities:
        observations = [
            {
                **row,
                "temperature": row["zones"][entity]["temperature"],
                "humidity": row["zones"][entity].get("humidity"),
            }
            for row in rows
            if finite(row.get("zones", {}).get(entity, {}).get("temperature")) is not None
        ]
        result[entity] = {
            "physical": fit_physical(observations),
            "humidity": fit_humidity(observations),
            "days": len({str(row["at"])[:10] for row in observations if row.get("provenance") == "normal"}),
            "validation": {
                mode: asdict(validate(observations, mode, timezone, window_minutes)) for mode in ("heat", "cool")
            },
        }
    return result


def cop_at(table: list[dict[str, float]], outdoor: float) -> float | None:
    """Interpolate a supplied performance curve within its covered range only."""
    if len(table) < 2 or not table[0]["temperature"] <= outdoor <= table[-1]["temperature"]:
        return None
    for left, right in zip(table, table[1:], strict=False):
        if left["temperature"] <= outdoor <= right["temperature"]:
            fraction = (outdoor - left["temperature"]) / (right["temperature"] - left["temperature"])
            return left["cop"] + fraction * (right["cop"] - left["cop"])
    return None


def electrical_power(
    rows: list[dict[str, Any]], mode: str, outdoor: float, table: list[dict[str, float]]
) -> float | None:
    """Condition electrical demand on weather; use COP only after empirical calibration."""
    active = [row for row in rows if row["mode"] == mode and float(row["power_kw"]) >= 0.1]
    local = [row for row in active if abs(float(row["outdoor"]) - outdoor) <= 3]
    if len(local) < MIN_NEIGHBOURS:
        return None
    empirical = mean(float(row["power_kw"]) for row in local)
    cop = cop_at(table, outdoor)
    calibrated = [(float(row["power_kw"]), cop_at(table, float(row["outdoor"]))) for row in active]
    supported = [(power, value) for power, value in calibrated if value is not None]
    if cop is not None and len(supported) >= MIN_PHYSICAL_SAMPLES:
        cut = int(len(supported) * 0.8)
        capacity = mean(power * value for power, value in supported[:cut])
        relative_error = mean(abs(capacity / value - power) / power for power, value in supported[cut:])
        if relative_error <= MAX_ENERGY_ERROR:
            return capacity / cop
    return empirical


def finish_observation(state: dict[str, Any], rows: list[dict[str, Any]], now: datetime) -> None:
    """Close a previously saved prediction using only complete non-intervention evidence."""
    start, end = instant(state.get("observation_started_at")), instant(state.get("observation_until"))
    if start is None or end is None or now < end or state.get("observation_closed_at") == end.isoformat():
        return
    selected = [r for r in rows if (at := instant(r.get("at"))) is not None and start <= at <= end]
    prediction = state.get("observation_prediction", {})
    reference = prediction.get("baseline_powers_kw", [])
    dates = [instant(value) for value in prediction.get("baseline_slots", [])]
    interval = timedelta(minutes=float(prediction.get("interval_minutes", 5)))
    forecast_energy = 0.0
    coverage_end = start
    for at, power in zip(dates, reference, strict=False):
        if at is None or finite(power) is None:
            continue
        overlap_start, overlap_end = max(start, at), min(end, at + interval)
        if overlap_start < overlap_end and overlap_start == coverage_end:
            forecast_energy += float(power) * (overlap_end - overlap_start).total_seconds() / 3600
            coverage_end = overlap_end
    valid = bool(
        selected
        and reference
        and coverage_end == end
        and instant(selected[0]["at"]) == start
        and instant(selected[-1]["at"]) == end
        and all(r["provenance"] == "normal" for r in selected)
    )
    for left, right in zip(selected, selected[1:], strict=False):
        left_at, right_at = instant(left["at"]), instant(right["at"])
        assert left_at is not None and right_at is not None
        valid &= right_at - left_at == timedelta(minutes=5)
    summary: dict[str, Any] = {
        "observation_end": end.isoformat(),
        "valid": valid,
        "reason": "complete" if valid else "incomplete_or_intervened",
    }
    if valid:
        actual = sum(float(r["power_kw"]) for r in selected[:-1]) / 12
        predicted = forecast_energy
        summary.update(
            actual_hvac_kwh=actual, predicted_hvac_kwh=predicted, energy_absolute_error_kwh=abs(actual - predicted)
        )
    comparisons = list(state.get("comparisons", []))
    state["comparisons"] = [*comparisons, summary][-100:]
    state["observation_closed_at"] = end.isoformat()


def humidity_rate(
    model: dict[str, Any], temperature: float, power: float, humidity: float, outdoor: float | None
) -> float | None:
    """Respect the humidity model's feature contract at prediction time."""
    features = [1.0, temperature / 30, power, humidity / 100]
    if model.get("outdoor"):
        if outdoor is None:
            return None
        features.append((outdoor - humidity) / 100)
    return float(sum(c * value for c, value in zip(model["coefficients"], features, strict=True)))


def normal_temperature(value: float) -> float:
    """Use the same explicit lookup grid in validation and planning."""
    return round(value / NORMAL_TEMPERATURE_RESOLUTION_C) * NORMAL_TEMPERATURE_RESOLUTION_C


def normal_history_for_slot(rows: list[dict[str, Any]], sample: dict[str, Any], timezone: str) -> list[dict[str, Any]]:
    """Prefilter reusable time/weather/occupancy support before thermal lookup."""
    at = instant(sample.get("at"))
    if at is None:
        return []
    local = at.astimezone(ZoneInfo(timezone))
    matches: list[dict[str, Any]] = []
    for row in rows:
        previous = instant(row.get("at"))
        if (
            row.get("provenance") != "normal"
            or previous is None
            or previous >= at
            or at - previous > timedelta(days=HISTORY_DAYS)
            or row.get("occupied") != sample.get("occupied")
            or row.get("mode") not in {sample.get("mode"), "off"}
        ):
            continue
        other = previous.astimezone(ZoneInfo(timezone))
        if (other.weekday() >= 5) != (local.weekday() >= 5):
            continue
        minutes = abs((other.hour * 60 + other.minute) - (local.hour * 60 + local.minute))
        minutes = min(minutes, 1440 - minutes)
        outdoor = abs(float(row["outdoor"]) - float(sample["outdoor"]))
        if minutes <= 90 and outdoor <= 5:
            matches.append(row)
    return matches
