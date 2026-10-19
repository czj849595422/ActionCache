"""团队工作记忆：集中式观测与统一历史事件流。

对应 MemoryKind.TEAM_WORKING：单一底层 MemoryStore，用 key 区分三个分区：
  - 观测 (obs)：每 agent 一个条目，覆盖式更新（论文 M_{t+1} = {O_{t+1}, ...}）；
  - 历史 (history)：自身/队友/系统事件追加式记录，按来源独立取窗口后合并；

观测轮询刷新由上层（PPA 团队层）驱动，本模块只负责存储与取用，不依赖感知层。
"""

from __future__ import annotations

from typing import Any, Callable

from .base import MemoryStore
from .inmemory import InMemoryMemoryStore
from .registry import MemoryRegistry
from .schemas import (
    MemoryEntry,
    MemoryError,
    MemoryKind,
    MemoryQuery,
    MemoryScope,
    MemoryStats,
    UNSET,
)
from .history import (
    HISTORY_KEY,
    validate_history_event,
)

_OBSERVATION_KEY = "obs"
DEFAULT_TEAM_CHAT_WINDOW = 20

ObservationRenderer = Callable[[Any], str]


class TeamMemory:
    """团队集中式工作记忆（论文 CMS 的落地实现）。"""

    def __init__(
        self,
        store: MemoryStore | None = None,
        *,
        default_chat_window: int = DEFAULT_TEAM_CHAT_WINDOW,
    ) -> None:
        if default_chat_window <= 0:
            raise MemoryError(
                "default_chat_window must be positive",
                operation="TeamMemory",
            )
        self._store = store if store is not None else InMemoryMemoryStore()
        self.default_chat_window = default_chat_window

    @classmethod
    def create(
        cls,
        *,
        store: MemoryStore | None = None,
        default_chat_window: int = DEFAULT_TEAM_CHAT_WINDOW,
    ) -> "TeamMemory":
        return cls(store=store, default_chat_window=default_chat_window)

    # ------------------------------------------------------------------
    # 观测（覆盖式：每 agent 只保留最新）
    # ------------------------------------------------------------------

    def update_observation(
        self,
        agent_id: str,
        observation: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        """写入/覆盖指定 agent 的最新观测。"""
        if not agent_id:
            raise MemoryError("agent_id must not be empty", operation="update_observation")
        entry = MemoryEntry.create(
            MemoryKind.TEAM_WORKING,
            MemoryScope.AGENT,
            observation,
            agent_id=agent_id,
            key=_OBSERVATION_KEY,
            metadata=metadata,
        )
        entry.entry_id = f"obs-{agent_id}"
        return self._store.put(entry)

    def get_observation(self, agent_id: str) -> MemoryEntry | None:
        return self._store.get(f"obs-{agent_id}")

    def observations(self) -> dict[str, MemoryEntry]:
        """返回 {agent_id: 最新观测条目}。"""
        entries = self._store.query(MemoryQuery(key=_OBSERVATION_KEY))
        return {entry.agent_id: entry for entry in entries if entry.agent_id}

    # ------------------------------------------------------------------
    # 统一历史事件流
    # ------------------------------------------------------------------

    def append_history_event(
        self,
        *,
        agent_id: str,
        source: str,
        event_type: str,
        content: dict[str, Any],
        record_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        if not agent_id:
            raise MemoryError("agent_id must not be empty", operation="append_history_event")
        validate_history_event(
            source=source,
            event_type=event_type,
            content=content,
        )
        event_metadata = {
            **dict(metadata or {}),
            "source": source,
            "event_type": event_type,
        }
        if record_id is not None:
            event_metadata["record_id"] = record_id
        if record_id is None:
            entry = self._store.append(
                dict(content),
                kind=MemoryKind.TEAM_WORKING,
                scope=MemoryScope.TEAM,
                agent_id=agent_id,
                key=HISTORY_KEY,
                metadata=event_metadata,
            )
        else:
            entry = MemoryEntry.create(
                MemoryKind.TEAM_WORKING,
                MemoryScope.TEAM,
                dict(content),
                agent_id=agent_id,
                key=HISTORY_KEY,
                metadata=event_metadata,
            )
            entry.entry_id = record_id
            entry = self._store.put(entry)
        return entry

    def update_history_event(
        self,
        record_id: str,
        *,
        content: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry | None:
        existing = self._store.get(record_id)
        if existing is None or existing.key != HISTORY_KEY:
            return None
        merged_content = dict(existing.content) if isinstance(existing.content, dict) else {}
        if content is not None:
            merged_content.update(content)
        merged_metadata = dict(existing.metadata)
        if metadata is not None:
            merged_metadata.update(metadata)
        return self._store.update(
            record_id,
            content=merged_content,
            metadata=merged_metadata,
        )

    def prompt_history(
        self,
        agent_id: str,
        *,
        team_limit: int = DEFAULT_TEAM_CHAT_WINDOW,
        system_limit: int = DEFAULT_TEAM_CHAT_WINDOW,
    ) -> list[dict[str, Any]]:
        if team_limit <= 0 or system_limit <= 0:
            raise MemoryError("history limits must be positive", operation="prompt_history")
        entries = self._store.query(MemoryQuery(key=HISTORY_KEY, order="asc"))
        team_candidates = []
        for entry in entries:
            source = entry.metadata.get("source")
            if source == "self" and entry.agent_id == agent_id:
                team_candidates.append(entry)
                continue
            if source not in {"self", "teammate"}:
                continue
            content = entry.content if isinstance(entry.content, dict) else {}
            if (
                entry.metadata.get("event_type") == "communication"
                or content.get("communication")
                or content.get("result_status") is not None
                or content.get("result_description")
            ):
                team_candidates.append(entry)
        team = team_candidates[-team_limit:]
        system = [
            entry for entry in entries
            if entry.metadata.get("source") == "system"
        ][-system_limit:]
        selected = sorted(
            [*team, *system],
            key=lambda entry: (
                entry.created_at,
                entry.metadata.get("history_sequence", entry.sequence),
            ),
        )
        result: list[dict[str, Any]] = []
        for entry in selected:
            source = entry.metadata.get("source")
            if source == "self" and entry.agent_id != agent_id:
                source = "teammate"
            result.append(
                {
                    "entry_id": entry.entry_id,
                    "agent_id": entry.agent_id,
                    "source": source,
                    "event_type": entry.metadata.get("event_type"),
                    "content": dict(entry.content) if isinstance(entry.content, dict) else entry.content,
                    "metadata": dict(entry.metadata),
                    "created_at": entry.created_at,
                    "sequence": entry.metadata.get("history_sequence", entry.sequence),
                }
            )
        return result

    def to_prompt_context(
        self,
        agent_id: str,
        *,
        chat_window: int | None = None,
        system_window: int | None = None,
        observation_renderer: ObservationRenderer | None = None,
    ) -> dict[str, Any]:
        """组装规划线程上下文；历史统一为一个来源已裁剪的列表。"""
        team_window = chat_window if chat_window is not None else self.default_chat_window
        system_window = system_window if system_window is not None else team_window
        observation_entry = self.get_observation(agent_id)
        observation = observation_entry.content if observation_entry else None
        if observation is not None and observation_renderer is not None:
            observation = observation_renderer(observation)

        return {
            "agent_id": agent_id,
            "observation": observation,
            "observation_updated_at": (
                observation_entry.updated_at if observation_entry else None
            ),
            "history": self.prompt_history(
                agent_id,
                team_limit=team_window,
                system_limit=system_window,
            ),
        }

    # ------------------------------------------------------------------
    # MemoryStore 透传（兼容注册表与契约测试）
    # ------------------------------------------------------------------

    def get(self, entry_id: str) -> MemoryEntry | None:
        return self._store.get(entry_id)

    def put(self, entry: MemoryEntry) -> MemoryEntry:
        return self._store.put(entry)

    def append(
        self,
        content: Any,
        *,
        kind: MemoryKind | str,
        scope: MemoryScope | str,
        agent_id: str | None = None,
        key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        return self._store.append(
            content,
            kind=kind,
            scope=scope,
            agent_id=agent_id,
            key=key,
            metadata=metadata,
        )

    def update(
        self,
        entry_id: str,
        *,
        content: Any = UNSET,
        metadata: dict[str, Any] | None = UNSET,
    ) -> MemoryEntry | None:
        return self._store.update(entry_id, content=content, metadata=metadata)

    def delete(self, entry_id: str) -> bool:
        return self._store.delete(entry_id)

    def query(self, spec: MemoryQuery) -> list[MemoryEntry]:
        return self._store.query(spec)

    def recent(
        self,
        *,
        scope: MemoryScope | str | None = None,
        agent_id: str | None = None,
        kind: MemoryKind | str | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        return self._store.recent(
            scope=scope,
            agent_id=agent_id,
            kind=kind,
            limit=limit,
        )

    def current(
        self,
        key: str,
        *,
        scope: MemoryScope | str | None = None,
        agent_id: str | None = None,
    ) -> MemoryEntry | None:
        return self._store.current(key, scope=scope, agent_id=agent_id)

    def clear(self) -> None:
        self._store.clear()

    def snapshot(self) -> list[MemoryEntry]:
        return self._store.snapshot()

    def stats(self) -> MemoryStats:
        return self._store.stats()

    def close(self) -> None:
        self._store.close()

    # ------------------------------------------------------------------
    # 能力透传（持久化 / 整集替换）
    # ------------------------------------------------------------------

    def replace_all(self, entries: list[MemoryEntry]) -> None:
        self._store.replace_all(entries)

    def export_json(self, path: str) -> str:
        return self._store.export_json(path)

    def load_json(self, path: str) -> list[MemoryEntry]:
        return self._store.load_json(path)


def register_team_memory(
    registry: MemoryRegistry,
    *,
    overwrite: bool = False,
) -> None:
    """把 TeamMemory 注册为 MemoryKind.TEAM_WORKING 的默认实现。"""
    from .base import MemoryPersistencePort, MemoryReplaceAllPort

    registry.register(
        MemoryKind.TEAM_WORKING,
        TeamMemory.create,
        capabilities=(MemoryReplaceAllPort, MemoryPersistencePort),
        overwrite=overwrite,
    )
