"""记忆模块共享类型：分类、条目、查询、统计与错误。

统一接口面向不同形式的记忆模块（工作记忆/情景记忆/语义记忆/程序记忆等），
类型体系保持通用：具体实现通过 MemoryKind 注册进 MemoryRegistry。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence
from uuid import uuid4

UNSET = object()
"""update() 中区分“未提供”与显式 None 的哨兵值。"""


class MemoryKind(str, Enum):
    """记忆形态分类（可扩展）。"""

    TEAM_WORKING = "team_working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    GUIDANCE = "guidance"


class MemoryScope(str, Enum):
    """记忆作用域。"""

    AGENT = "agent"
    TEAM = "team"
    GLOBAL = "global"


class UpdateSemantics(str, Enum):
    """条目更新语义（供实现与文档声明使用）。"""

    OVERWRITE = "overwrite"
    APPEND = "append"
    WINDOW = "window"
    CURRENT = "current"
    REPLACE_ALL = "replace_all"


class MemoryError(RuntimeError):
    """记忆模块错误基类。"""

    def __init__(self, message: str, *, operation: str | None = None):
        super().__init__(message)
        self.operation = operation


class MemoryNotFoundError(MemoryError):
    """条目不存在。"""


class MemoryConflictError(MemoryError):
    """冲突（例如重复注册同一种记忆形态）。"""


class MemoryCapabilityError(MemoryError):
    """调用实现方未声明支持的能力。"""


def _coerce_enum(value: Any, enum_cls: type[Enum], name: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    return enum_cls(value)


def _as_enum_tuple(
    value: Any,
    enum_cls: type[Enum],
    name: str,
) -> tuple[Any, ...] | None:
    if value is None:
        return None
    if isinstance(value, enum_cls) or isinstance(value, str):
        items = [value]
    else:
        items = list(value)
    return tuple(_coerce_enum(item, enum_cls, name) for item in items)


@dataclass(slots=True)
class MemoryEntry:
    """一条记忆记录。"""

    entry_id: str
    kind: MemoryKind
    scope: MemoryScope
    content: Any
    agent_id: str | None = None
    key: str | None = None
    sequence: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.kind = _coerce_enum(self.kind, MemoryKind, "kind")
        self.scope = _coerce_enum(self.scope, MemoryScope, "scope")
        if not self.entry_id:
            raise MemoryError("entry_id must not be empty")
        if self.sequence < 0:
            raise MemoryError("sequence must not be negative")
        if self.updated_at < self.created_at:
            raise MemoryError("updated_at must not be earlier than created_at")

    @classmethod
    def create(
        cls,
        kind: MemoryKind | str,
        scope: MemoryScope | str,
        content: Any,
        *,
        agent_id: str | None = None,
        key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "MemoryEntry":
        now = time.time()
        return cls(
            entry_id=f"mem-{uuid4().hex[:12]}",
            kind=_coerce_enum(kind, MemoryKind, "kind"),
            scope=_coerce_enum(scope, MemoryScope, "scope"),
            content=content,
            agent_id=agent_id,
            key=key,
            sequence=0,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "content": self.content,
            "agent_id": self.agent_id,
            "key": self.key,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryEntry":
        required = ("entry_id", "kind", "scope", "content")
        missing = [name for name in required if name not in data]
        if missing:
            raise MemoryError(
                "MemoryEntry missing fields: " + ", ".join(missing),
                operation="from_dict",
            )
        return cls(
            entry_id=str(data["entry_id"]),
            kind=_coerce_enum(data["kind"], MemoryKind, "kind"),
            scope=_coerce_enum(data["scope"], MemoryScope, "scope"),
            content=data["content"],
            agent_id=data.get("agent_id"),
            key=data.get("key"),
            sequence=int(data.get("sequence", 0)),
            created_at=float(data.get("created_at", 0.0)),
            updated_at=float(data.get("updated_at", 0.0)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(slots=True)
class MemoryQuery:
    """记忆查询条件。"""

    kinds: MemoryKind | str | Sequence[MemoryKind | str] | None = None
    scopes: MemoryScope | str | Sequence[MemoryScope | str] | None = None
    agent_id: str | None = None
    key: str | None = None
    since: float | None = None
    until: float | None = None
    limit: int | None = None
    offset: int = 0
    order: str = "desc"

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit <= 0:
            raise MemoryError("limit must be positive when provided")
        if self.offset < 0:
            raise MemoryError("offset must not be negative")
        if self.order not in {"asc", "desc"}:
            raise MemoryError("order must be 'asc' or 'desc'")
        if self.since is not None and self.until is not None and self.until < self.since:
            raise MemoryError("until must not be earlier than since")
        self.kinds = _as_enum_tuple(self.kinds, MemoryKind, "kinds")
        self.scopes = _as_enum_tuple(self.scopes, MemoryScope, "scopes")


@dataclass(slots=True)
class MemoryStats:
    """记忆统计信息。"""

    total: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    by_scope: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_kind": dict(self.by_kind),
            "by_scope": dict(self.by_scope),
        }
