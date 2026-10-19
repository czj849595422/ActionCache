"""
Ordered perception verbalizer pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import VerbalizeContext, VerbalizeRule, normalize_language


@dataclass
class VerbalizerPipeline:
    """
    Render filtered perception JSON into text by applying section rules in order.

    Every rule reads the same observation object. Rule order controls text
    ordering only; rules must not mutate the observation.
    """

    rules: list[VerbalizeRule] = field(default_factory=list)
    language: str = "zh"
    separator: str = "\n"
    skip_empty: bool = True

    def __post_init__(self) -> None:
        self.language = normalize_language(self.language)

    def render(
        self,
        observation: dict[str, Any],
        *,
        context: VerbalizeContext | None = None,
    ) -> str:
        active_context = context or VerbalizeContext(language=self.language)
        segments: list[str] = []
        for rule in self.rules:
            segment = rule.render(observation, active_context)
            if segment is None:
                if not self.skip_empty:
                    segments.append("")
                continue
            segment = str(segment).strip()
            if segment or not self.skip_empty:
                segments.append(segment)
        return self.separator.join(segments)


ObservationVerbalizer = VerbalizerPipeline
