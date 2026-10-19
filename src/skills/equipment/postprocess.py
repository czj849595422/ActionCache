from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import (
    result_payload,
)


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    payload = result_payload(action_result)
    if command == "equip_items":
        return describe_equip_items(payload)
    if command == "unequip_items":
        return describe_unequip_items(payload)
    return None


def describe_equip_items(payload: dict[str, Any]) -> str:
    succeeded = payload.get("succeeded") or []
    failed = payload.get("failed") or []
    if not payload.get("ok"):
        return f"装备失败：全部失败（{_failed_text(failed)}）。"
    if succeeded and failed:
        return (
            f"装备部分成功：成功 {len(succeeded)} 项（{_success_text(succeeded, 'equip')}）；"
            f"失败 {len(failed)} 项（{_failed_text(failed)}）。"
        )
    return f"装备完成：成功装备 {len(succeeded)} 项（{_success_text(succeeded, 'equip')}）。"


def describe_unequip_items(payload: dict[str, Any]) -> str:
    succeeded = payload.get("succeeded") or []
    failed = payload.get("failed") or []
    if not payload.get("ok"):
        return f"卸下失败：全部失败（{_failed_text(failed)}）。"
    if succeeded and failed:
        return (
            f"卸下部分成功：成功 {len(succeeded)} 项（{_success_text(succeeded, 'unequip')}）；"
            f"失败 {len(failed)} 项（{_failed_text(failed)}）。"
        )
    return f"卸下完成：成功卸下 {len(succeeded)} 个槽位（{_success_text(succeeded, 'unequip')}）。"


def _success_text(succeeded: list[dict[str, Any]], kind: str) -> str:
    parts = []
    for item in succeeded:
        slot = item.get("destination")
        if kind == "equip":
            name = (item.get("item") or {}).get("name") or "unknown"
            parts.append(f"{slot}:{name}")
        else:
            parts.append(str(slot))
    return "、".join(parts) or "无"


def _failed_text(failed: list[dict[str, Any]]) -> str:
    parts = []
    for item in failed:
        slot = item.get("destination")
        reason = item.get("reason") or item.get("error_code") or "unknown"
        parts.append(f"{slot}:{reason}" if slot else str(reason))
    return "、".join(parts) or "无"
