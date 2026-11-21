"""Bounded elapsed-time accounting for one refresh, including exceptional exits."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from time import perf_counter
from typing import Any


class RefreshTiming:
    """Partition elapsed wall time; compatibility aliases are reported separately."""

    def __init__(self) -> None:
        self.started = self.boundary = perf_counter()
        self.owner = asyncio.current_task()
        self.name = "unattributed_ms"
        self.phases: dict[str, float] = {}
        self.aliases: dict[str, float] = {}

    def stage(self, name: str) -> None:
        """Close the previous exclusive phase, even when its await failed."""
        now = perf_counter()
        self.phases[self.name] = self.phases.get(self.name, 0) + max(now - self.boundary, 0) * 1000
        self.boundary, self.name = now, name

    def finish(self) -> dict[str, Any]:
        """Return one coherent snapshot; aliases must not be summed with phases."""
        self.stage("unattributed_ms")
        return {
            "duration_ms": round(sum(self.phases.values()), 3),
            "exclusive_phases_ms": {key: round(value, 3) for key, value in self.phases.items()},
            "phases": {**{key: round(value, 3) for key, value in self.phases.items()}, **self.aliases},
        }


CURRENT_REFRESH: ContextVar[RefreshTiming | None] = ContextVar("energy_planner_refresh", default=None)


def current_timing() -> RefreshTiming | None:
    """Do not let child tasks publish timings into their parent's refresh."""
    timing = CURRENT_REFRESH.get()
    return timing if timing is not None and timing.owner is asyncio.current_task() else None


def stage(name: str) -> None:
    """Advance the calling refresh only (direct helper calls need no context)."""
    if (timing := current_timing()) is not None:
        timing.stage(name)


def request_owner() -> asyncio.Task[Any] | None:
    """Callbacks outside a running task cannot supply request attribution."""
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None
