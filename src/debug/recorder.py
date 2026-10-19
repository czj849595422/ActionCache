"""Debug 会话元数据、事件流和完整 Artifact 记录。"""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading
from typing import Any
from uuid import uuid4

from .schemas import DebugResult, to_jsonable, utc_timestamp


_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


class DebugRecorder:
    """线程安全地持久化调试输入、结果及大型载荷。"""

    def __init__(
        self,
        root: str | Path,
        session_metadata: dict[str, Any],
        *,
        session_id: str | None = None,
        artifact_threshold: int = 4096,
    ) -> None:
        if artifact_threshold <= 0:
            raise ValueError("artifact_threshold must be positive")
        self.session_id = session_id or (
            f"{utc_timestamp().replace(':', '').replace('+00:00', 'Z')}-"
            f"{uuid4().hex[:8]}"
        )
        self.session_dir = Path(root).resolve() / self.session_id
        self.artifacts_dir = self.session_dir / "artifacts"
        self.events_file = self.session_dir / "events.jsonl"
        self.artifact_threshold = artifact_threshold
        self._lock = threading.RLock()
        self._sequence = 0
        self._events: list[dict[str, Any]] = []
        self.artifacts_dir.mkdir(parents=True, exist_ok=False)
        metadata = {
            "session_id": self.session_id,
            "created_at": utc_timestamp(),
            **to_jsonable(session_metadata),
        }
        (self.session_dir / "session.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def record_result(
        self,
        result: DebugResult,
        *,
        request: Any = None,
        force_artifact: bool = False,
    ) -> DebugResult:
        full = result.to_dict()
        payload_text = json.dumps(full, ensure_ascii=False, default=str)
        use_artifact = force_artifact or len(payload_text) > self.artifact_threshold
        with self._lock:
            if use_artifact:
                result.artifact_path = str(
                    self._write_artifact_locked(result.operation, result.to_dict())
                )
            self._sequence += 1
            event = {
                "sequence": self._sequence,
                "timestamp": result.timestamp,
                "operation": result.operation,
                "ok": result.ok,
                "request": to_jsonable(request),
                "error": to_jsonable(result.error),
                "artifact_path": result.artifact_path,
                "result": (
                    _summarize(result.result)
                    if result.artifact_path is not None
                    else to_jsonable(result.result)
                ),
            }
            self._append_event_locked(event)
        return result

    def record_event(
        self,
        operation: str,
        result: Any = None,
        *,
        ok: bool = True,
        error: dict[str, Any] | None = None,
    ) -> None:
        envelope = DebugResult(
            ok=ok,
            operation=operation,
            result=result,
            error=error,
        )
        self.record_result(envelope)

    def history(self, count: int = 20) -> list[dict[str, Any]]:
        if count <= 0:
            raise ValueError("history count must be positive")
        with self._lock:
            return [dict(item) for item in self._events[-count:]]

    def _write_artifact_locked(self, operation: str, payload: Any) -> Path:
        safe_name = _SAFE_NAME_RE.sub("_", operation).strip("._") or "result"
        index = self._sequence + 1
        path = self.artifacts_dir / f"{index:05d}-{safe_name}.json"
        path.write_text(
            json.dumps(
                to_jsonable(payload),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            ),
            encoding="utf-8",
        )
        return path

    def _append_event_locked(self, event: dict[str, Any]) -> None:
        normalized = to_jsonable(event)
        with self.events_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(normalized, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        self._events.append(normalized)


def _summarize(value: Any) -> Any:
    value = to_jsonable(value)
    if isinstance(value, dict):
        return {"type": "object", "keys": list(value)[:20], "size": len(value)}
    if isinstance(value, list):
        return {"type": "array", "size": len(value)}
    text = str(value)
    return text if len(text) <= 240 else f"{text[:237]}..."
