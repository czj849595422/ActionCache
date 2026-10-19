"""记忆模块：统一接口 + 通用内存存储 + PPA 团队工作记忆。"""

from .base import (
    EntryCallback,
    MemoryPersistencePort,
    MemoryReplaceAllPort,
    MemorySemanticSearchPort,
    MemoryStore,
    MemorySubscriptionPort,
    SubscriptionToken,
    require_capability,
)
from .registry import MemoryRegistry
from .inmemory import InMemoryMemoryStore
from .team import DEFAULT_TEAM_CHAT_WINDOW, TeamMemory, register_team_memory
from .history import (
    HISTORY_EVENT_TYPES,
    HISTORY_KEY,
    HISTORY_SOURCES,
    action_text,
    render_history,
)
from .schemas import (
    MemoryCapabilityError,
    MemoryConflictError,
    MemoryEntry,
    MemoryError,
    MemoryKind,
    MemoryNotFoundError,
    MemoryQuery,
    MemoryScope,
    MemoryStats,
    UNSET,
    UpdateSemantics,
)

__all__ = [
    "EntryCallback",
    "MemoryCapabilityError",
    "MemoryConflictError",
    "MemoryEntry",
    "MemoryError",
    "MemoryKind",
    "MemoryNotFoundError",
    "MemoryPersistencePort",
    "MemoryQuery",
    "MemoryRegistry",
    "MemoryReplaceAllPort",
    "MemoryScope",
    "MemorySemanticSearchPort",
    "MemoryStats",
    "MemoryStore",
    "MemorySubscriptionPort",
    "DEFAULT_TEAM_CHAT_WINDOW",
    "InMemoryMemoryStore",
    "TeamMemory",
    "SubscriptionToken",
    "UNSET",
    "UpdateSemantics",
    "register_team_memory",
    "require_capability",
    "action_text",
    "render_history",
    "HISTORY_EVENT_TYPES",
    "HISTORY_KEY",
    "HISTORY_SOURCES",
]
