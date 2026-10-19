"""并发运行时底座：单槽覆盖队列、后台循环 worker、多 agent 编排器。

供 PPA 双线程及其它方法复用的通用并发模块，不依赖任何方法实现。
"""

from .buffer import SingleSlotOverwriteBuffer
from .liveness import (
    EntityLivenessGate,
    agent_is_live,
    mark_agent_live,
    mark_agents_live,
    set_agent_live_by_name,
    snapshot_live_state,
)
from .team_runner import (
    AgentLike,
    CompletionCheck,
    ServerLike,
    TeamRunResult,
    TeamRunner,
    WorkerFactory,
)
from .worker import LoopWorker

__all__ = [
    "AgentLike",
    "CompletionCheck",
    "LoopWorker",
    "ServerLike",
    "SingleSlotOverwriteBuffer",
    "EntityLivenessGate",
    "TeamRunResult",
    "TeamRunner",
    "WorkerFactory",
    "agent_is_live",
    "mark_agent_live",
    "mark_agents_live",
    "set_agent_live_by_name",
    "snapshot_live_state",
]
