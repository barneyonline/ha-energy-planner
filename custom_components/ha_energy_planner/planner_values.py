"""Finite numeric values shared by pure planning policies and presentation."""

from __future__ import annotations

from math import isfinite
from typing import Any


def finite_float(value: Any) -> float | None:
    """Return a finite lifecycle scalar."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def nonnegative_float_or_none(value: Any) -> float | None:
    number = finite_float(value)
    if number is None:
        return None
    return max(number, 0.0)
