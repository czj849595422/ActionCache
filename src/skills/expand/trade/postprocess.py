from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import (
    error_reason,
    format_items,
    result_ok,
    result_payload,
)


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    payload = result_payload(action_result)
    if not result_ok(action_result, payload):
        return _describe_failure(command, action_result, payload)
    if command == "inspect_own_containers":
        personal = payload.get("personal_box") or {}
        shop = payload.get("shop") or {}
        return (
            f"已查看自有容器：私人箱 {format_items(personal.get('items'))}；"
            f"商铺 {format_items(shop.get('items'))}；"
            f"背包 {format_items(payload.get('inventory_items'))}。"
        )
    if command == "inspect_shop":
        return (
            f"已查看 {payload.get('shop_name')}（卖家 {payload.get('owner')}）："
            f"在售 {format_items(payload.get('sale_items'))}。"
        )
    if command == "transfer_owned_items":
        container = payload.get("container_contents") or {}
        return (
            f"已{_direction_text(payload.get('direction'))} "
            f"{format_items(payload.get('moved'), count_keys=('count',))}；"
            f"{container.get('name') or payload.get('container')} 当前为 "
            f"{format_items(container.get('items'))}。"
        )
    if command == "buy_from_shop":
        return (
            f"交易 {payload.get('transaction_id')} 已提交：从 {payload.get('seller')} 的"
            f" {payload.get('shop_name')} 购买 "
            f"{format_items([payload.get('purchased')], count_keys=('count',))}，"
            f"总价 {payload.get('total_price')} 金粒，剩余 {payload.get('buyer_currency_remaining')} 金粒。"
        )
    if command == "craft_target":
        return (
            f"已完成 {len(payload.get('completed_steps') or [])} 个合成步骤，"
            f"背包中 {payload.get('item')} 当前数量为 {payload.get('current_count')}。"
        )
    return None


def _describe_failure(
    command: str,
    action_result: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    code = error_reason(action_result, payload)
    if code == "missing_materials":
        return f"目标合成材料不足：{format_items(payload.get('missing'))}。"
    if code == "purchase_inconsistent":
        return (
            f"购买事务 {payload.get('transaction_id')} 回滚不完整，"
            "容器与背包实际状态保留在 raw JSON 中，禁止假定交易成功。"
        )
    if code in {"container_item_missing", "inventory_item_missing"}:
        return f"批量转移预检失败：{format_items(payload.get('missing'))}。"
    labels = {
        "inspect_own_containers": "查看自有容器",
        "inspect_shop": "查看商铺",
        "transfer_owned_items": "转移物品",
        "buy_from_shop": "购买",
        "craft_target": "合成目标",
    }
    return f"{labels.get(command, 'Trade 动作')}失败：{code}。"


def _direction_text(direction: Any) -> str:
    return "取出" if direction == "take" else "存入"
