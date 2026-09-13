"""Versioned, detached contracts for economic climate planning.

These records contain measurements and predictions, never Home Assistant objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

BASELINE_VERSION = 1
PHYSICAL_VERSION = 1
VALIDATION_VERSION = 1
LIFECYCLE_VERSION = 1
HISTORY_DAYS = 60
MAX_OBSERVATIONS = HISTORY_DAYS * 288
MIN_HISTORY_DAYS = 14
MIN_VALIDATION_WINDOWS = 20
MIN_ACTIVE_EPISODES = 5
MAX_TEMPERATURE_MAE = 0.5
MAX_TEMPERATURE_P90 = 1.0
MAX_ENERGY_ERROR = 0.20
MIN_STATE_ACCURACY = 0.90
MIN_ACTIVE_RECALL = 0.80
VALIDATION_EXPIRY_DAYS = 14
RECOVERY_TOLERANCE_C = 0.25
TERMINAL_BATTERY_TOLERANCE_KWH = 0.01


@dataclass(frozen=True, slots=True)
class ZoneObservation:
    """A room attached to an existing actuator, not a separate compressor."""

    entity_id: str
    temperature: float | None
    humidity: float | None = None
    occupied: bool | None = None
    low: float | None = None
    high: float | None = None
    maximum_humidity: float | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ClimateObservation:
    """One provenance-tagged observation of the shared climate system."""

    at: datetime
    temperature: float
    outdoor: float
    power_kw: float
    mode: str
    target: float | None
    occupied: str
    low: float
    high: float
    provenance: str
    humidity: float | None = None
    outdoor_humidity: float | None = None
    irradiance: float | None = None
    zones: tuple[ZoneObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class BaselinePrediction:
    """Empirical matched-neighbour prediction; spread is not a guarantee."""

    power_kw: float
    upper_power_kw: float
    target: float | None
    active_fraction: float
    neighbours: int
    mode: str


@dataclass(frozen=True, slots=True)
class ClimateTrajectory:
    """Aligned end-of-slot predictions; electrical load is counted once."""

    temperatures: tuple[float, ...]
    powers_kw: tuple[float, ...]
    humidities: tuple[float | None, ...] = ()
    zones: dict[str, tuple[float, ...]] = field(default_factory=dict)
    temperature_lower: tuple[float, ...] = ()
    temperature_upper: tuple[float, ...] = ()
    zone_lower: dict[str, tuple[float, ...]] = field(default_factory=dict)
    zone_upper: dict[str, tuple[float, ...]] = field(default_factory=dict)
    normal_targets: tuple[float | None, ...] = ()


@dataclass(frozen=True, slots=True)
class SiteCost:
    """Tariff-valued energy with a conserved battery state."""

    cost: float
    import_kwh: float
    export_kwh: float
    hvac_kwh: float
    terminal_battery_kwh: float


@dataclass(frozen=True, slots=True)
class ClimateCandidate:
    """An acquisition followed by coasting and return to normal operation."""

    start: int
    stop: int
    release: int
    mode: str
    target: float
    trajectory: ClimateTrajectory
    expected_saving: float
    conservative_saving: float
    baseline_cost: SiteCost
    candidate_cost: SiteCost
    baseline_trajectory: ClimateTrajectory | None = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Chronological, complete-window evidence for one operating mode."""

    mode: str
    days: int
    windows: int
    active_episodes: int
    temperature_mae: float
    temperature_p90: float
    energy_error: float
    state_accuracy: float
    active_recall: float
    blockers: tuple[str, ...]
    residuals: tuple[float, ...] = ()
    last_window_at: str | None = None
