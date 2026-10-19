"""
Lightweight perception data types.

The first development stage returns raw dictionaries from Mineflayer. These
aliases keep the Python API explicit without forcing premature schemas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PerceptionData = dict[str, Any]


@dataclass
class PerceptionRequest:
    mode: str
    sub_mode: str
    radius: float | None = None
    block_radius: float | None = None
    fov_degrees: float = 90.0
    metadata: dict[str, Any] = field(default_factory=dict)
