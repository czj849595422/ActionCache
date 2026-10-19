"""
Cross-module interface schemas.

These dataclasses define stable protocol objects shared by environment,
agents, perception, action planning, and future HCAP modules.
"""

from .schemas import (
    ActionResultEnvelope,
    ActionUnit,
    AgentIdentity,
    LLMSemanticUnit,
    PerceptionSnapshot,
    Plan,
    Report,
    SessionTrace,
)

__all__ = [
    "AgentIdentity",
    "PerceptionSnapshot",
    "Report",
    "Plan",
    "ActionUnit",
    "ActionResultEnvelope",
    "SessionTrace",
    "LLMSemanticUnit",
]
