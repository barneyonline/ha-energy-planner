"""Internal command contracts shared by manual and planned execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .models import ActionAsset, ActionKind


class ControlAction(Protocol):
    """Common command fields; scheduling remains a PlanAction responsibility."""

    action_id: str
    asset: ActionAsset
    kind: ActionKind
    desired_state: dict[str, Any]


@dataclass(slots=True)
class ManualControlAction:
    """Explicit command that shares control gates without inventing a schedule."""

    action_id: str
    asset: ActionAsset
    kind: ActionKind
    desired_state: dict[str, Any]
