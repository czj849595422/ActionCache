"""LLM 调用记录保存：JSON 完整记录，可选时间后缀。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .schemas import CallRecord, LLMResponse

_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"


def save_call(
    record: CallRecord | LLMResponse | dict[str, Any],
    save_dir: str | Path,
    filename: str,
    *,
    with_timestamp: bool = False,
    messages: Sequence[Mapping[str, Any]] | None = None,
    ensure_ascii: bool = False,
) -> Path:
    """保存一次调用记录为 JSON 文件，返回最终路径。

    with_timestamp=True 时在文件名后追加时间后缀（%Y%m%d-%H%M%S），
    同名冲突时追加 -2/-3... 序号。
    """
    save_path = Path(save_dir)
    save_path.mkdir(parents=True, exist_ok=True)
    target = _resolve_path(save_path, filename, with_timestamp=with_timestamp)
    payload = _to_payload(record, messages=messages)
    target.write_text(
        json.dumps(payload, ensure_ascii=ensure_ascii, indent=2),
        encoding="utf-8",
    )
    return target


def _resolve_path(save_dir: Path, filename: str, *, with_timestamp: bool) -> Path:
    name = Path(filename)
    stem, suffix = name.stem, name.suffix
    if with_timestamp:
        stem = f"{stem}_{datetime.now().strftime(_TIMESTAMP_FORMAT)}"
    candidate = save_dir / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = save_dir / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _to_payload(
    record: CallRecord | LLMResponse | dict[str, Any],
    *,
    messages: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    if isinstance(record, CallRecord):
        data = record.to_dict()
    elif isinstance(record, LLMResponse):
        data = CallRecord.from_response(record, messages=messages).to_dict()
    elif isinstance(record, dict):
        data = dict(record)
    else:
        raise TypeError(
            "record must be CallRecord, LLMResponse, or dict, "
            f"got {type(record).__name__}"
        )
    if messages is not None and not data.get("messages"):
        data["messages"] = [dict(message) for message in messages]
    return data
