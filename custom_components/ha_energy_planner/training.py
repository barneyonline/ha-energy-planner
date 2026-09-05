"""Single-flight background history training with safe publication boundaries."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BYPASS_SAFETY_GATES,
    CONF_DAIKIN_POWER,
    CONF_EV_CHARGE_RATE_KW,
    CONF_EV_CHARGING,
    CONF_EV_SOC,
    CONF_HOUSEHOLD_LOAD,
    DOMAIN,
)
from .recorder_import import async_update_builtin_load_forecast, async_update_ev_charge_calibration
from .safety import strict_bool

_LOGGER = logging.getLogger(__name__)
_LOCKS_KEY = f"{DOMAIN}_history_training_locks"


@dataclass(frozen=True, slots=True)
class TrainingRequest:
    """Immutable configuration identity and detached model snapshot."""

    entry_data: dict[str, Any]
    charge_rate_kw: float
    timezone: str
    bypass_safety_gates: bool
    ev_model: dict[str, Any]
    load_model: dict[str, Any]

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            *(self.entry_data.get(key) for key in (
                CONF_HOUSEHOLD_LOAD, CONF_EV_CHARGING, CONF_EV_SOC, CONF_DAIKIN_POWER,
            )),
            self.charge_rate_kw, self.timezone, self.bypass_safety_gates,
        )


@dataclass(frozen=True, slots=True)
class TrainingResult:
    """Models and bounded importer outcomes from one source configuration."""

    ev_model: dict[str, Any]
    load_model: dict[str, Any]
    ev_changed: bool
    load_changed: bool
    ev_reason: str
    load_reason: str


def training_request(
    entry_data: dict[str, Any], options: dict[str, Any], store_data: dict[str, Any], timezone: str
) -> TrainingRequest:
    """Detach all model/configuration roots before yielding to background work."""
    return TrainingRequest(
        dict(entry_data), float(options[CONF_EV_CHARGE_RATE_KW]), timezone,
        strict_bool(options.get(CONF_BYPASS_SAFETY_GATES), default=False),
        dict(store_data.get("ev_charge_calibration", {})),
        dict(store_data.get("built_in_load_forecast", {})),
    )


class HistoryTraining:
    """Train without holding planner locks; never overlap imports across reload."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        publish: Callable[[int, TrainingRequest, TrainingResult], Awaitable[None]],
    ) -> None:
        self.hass = hass
        self.publish = publish
        self.generation = 0
        self.closed = False
        self.task: asyncio.Task[None] | None = None
        self.pending: TrainingRequest | None = None
        self.identity: tuple[Any, ...] | None = None
        self.load_training_attempted = False
        self.last_result: TrainingResult | None = None
        # A stopped coordinator cannot cancel a thread already in Recorder. The
        # next lifetime waits on this same entry lock until that work completes.
        locks = hass.data.setdefault(_LOCKS_KEY, {})
        self._lock: asyncio.Lock = locks.setdefault(entry_id, asyncio.Lock())

    def request(self, request: TrainingRequest) -> None:
        """Coalesce requests, invalidating publication when configuration changes."""
        if self.closed:
            return
        changed = request.identity != self.identity
        if changed:
            self.generation += 1
            self.identity = request.identity
            self.load_training_attempted = False
            self.last_result = None
        if self.task is not None and not self.task.done() and not changed:
            return
        self.pending = request
        if self.task is None or self.task.done():
            self.task = self.hass.async_create_background_task(self._run(), "energy planner history training")

    def stop(self) -> None:
        """Invalidate results immediately, retaining executor completion ownership."""
        self.closed = True
        self.generation += 1
        self.pending = None

    def is_current(self, generation: int) -> bool:
        return not self.closed and generation == self.generation

    async def _train(self, request: TrainingRequest) -> TrainingResult:
        now = dt_util.utcnow()
        ev, ev_changed, ev_reason = await async_update_ev_charge_calibration(
            self.hass, request.entry_data, request.ev_model,
            charge_rate_kw=request.charge_rate_kw, now=now,
        )
        load, load_changed, load_reason = await async_update_builtin_load_forecast(
            self.hass, request.entry_data, request.load_model, now=now, timezone=request.timezone,
            force=not self.load_training_attempted,
            bypass_conservative_bound_gate=request.bypass_safety_gates,
        )
        return TrainingResult(ev, load, ev_changed, load_changed, ev_reason, load_reason)

    async def _run(self) -> None:
        try:
            while self.pending is not None and not self.closed:
                request, self.pending = self.pending, None
                generation = self.generation
                async with self._lock:
                    if not self.is_current(generation):
                        continue
                    job = self.hass.async_create_background_task(self._train(request), "energy planner recorder job")
                    cancelled = False
                    while not job.done():
                        try:
                            await asyncio.shield(job)
                        except asyncio.CancelledError:
                            # Even repeated cancellation must not release the
                            # entry lock while a Recorder thread still runs.
                            cancelled = True
                    if cancelled:
                        raise asyncio.CancelledError
                    result = job.result()
                if not self.is_current(generation):
                    continue
                self.last_result = result
                if result.load_reason != "load_forecast_household_load_unavailable":
                    self.load_training_attempted = True
                await self.publish(generation, request, result)
        except Exception as err:  # noqa: BLE001 - training cannot fail a planner refresh.
            _LOGGER.warning("History training failed: %s", type(err).__name__)
