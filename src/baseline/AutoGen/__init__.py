"""AutoGen-style hierarchical A2A baseline implementation."""

from .a2a import A2AStore
from .protocol import (
    AutoGenProtocolError,
    CommanderParseResult,
    parse_commander_response,
    parse_executor_response,
)
from .runner import AutoGenAgentRuntime, AutoGenRunner
from .schemas import (
    A2AMessage,
    AgentSubtaskState,
    AutoGenConfig,
    AutoGenRunResult,
    ExecutorDecision,
    SubtaskAssignment,
    SubtaskReport,
)
from .sequential import AutoGenSequentialRunner
from .workers import AutoGenCommanderWorker, AutoGenSubtaskPlanningWorker


__all__ = [
    "A2AMessage",
    "A2AStore",
    "AgentSubtaskState",
    "AutoGenAgentRuntime",
    "AutoGenCommanderWorker",
    "AutoGenConfig",
    "AutoGenProtocolError",
    "AutoGenRunResult",
    "AutoGenRunner",
    "AutoGenSequentialRunner",
    "AutoGenSubtaskPlanningWorker",
    "CommanderParseResult",
    "ExecutorDecision",
    "SubtaskAssignment",
    "SubtaskReport",
    "parse_commander_response",
    "parse_executor_response",
]
