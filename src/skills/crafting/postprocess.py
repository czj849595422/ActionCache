from __future__ import annotations

from typing import Any

from src.agents.action.result_postprocessor import (
    error_reason,
    format_items,
    format_missing,
    item_name,
    result_ok,
    result_payload,
)


def describe_result(command: str, action_result: dict[str, Any]) -> str | None:
    payload = result_payload(action_result)
    if command == "query_recipe":
        return describe_query_recipe(action_result, payload)
    if command == "craft_with_recipe":
        return describe_craft_with_recipe(action_result, payload)
    if command == "craft_item":
        return describe_craft_item(action_result, payload)
    return None


def describe_query_recipe(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    item = item_name(payload.get("item"))
    if not result_ok(action_result, payload):
        return f"查询 {item} 配方失败：{error_reason(action_result, payload)}。"

    recipe_count = payload.get("recipe_count", len(payload.get("recipes") or []))
    table_text = "全部需要合成台" if payload.get("requires_table_all") else (
        "部分需要合成台" if payload.get("requires_table_any") else "不需要合成台"
    )
    recipes = []
    for recipe in list(payload.get("recipes") or [])[:5]:
        recipes.append(
            f"recipe_index={recipe.get('recipe_index')} 需要 {format_items(recipe.get('materials'))}"
        )
    extra = len(payload.get("recipes") or []) - len(recipes)
    recipe_text = "；".join(recipes) if recipes else "无可用配方"
    if extra > 0:
        recipe_text += f"；另有 {extra} 个配方"
    return f"查询 {item} 配方成功，共 {recipe_count} 个合法配方，{table_text}。{recipe_text}。"


def describe_craft_with_recipe(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    item = payload.get("item") or "目标物品"
    recipe_index = payload.get("recipe_index")
    if not result_ok(action_result, payload):
        missing = payload.get("missing")
        if missing:
            return f"按 recipe_index={recipe_index} 合成 {item} 失败：{format_missing(missing)}。"
        return f"按 recipe_index={recipe_index} 合成 {item} 失败：{error_reason(action_result, payload)}。"

    count = payload.get("crafted_item_count") or payload.get("craft_count") or 1
    table = "需要合成台" if payload.get("requires_table") else "不需要合成台"
    consumed = format_items(payload.get("consumed_ingredients"))
    return f"已用 recipe_index={recipe_index} 合成 {item} x{count}，消耗 {consumed}，{table}。"


def describe_craft_item(action_result: dict[str, Any], payload: dict[str, Any]) -> str:
    item = payload.get("item") or "目标物品"
    selected = payload.get("selected_recipe_index")
    if not result_ok(action_result, payload):
        candidates = []
        for recipe in list(payload.get("candidate_recipes") or [])[:4]:
            candidates.append(
                f"recipe_index={recipe.get('recipe_index')} 缺少 {format_missing(recipe.get('missing'))}"
            )
        if candidates:
            return f"自动合成 {item} 失败：" + "；".join(candidates) + "。"
        missing = payload.get("missing")
        if missing:
            return f"自动合成 {item} 失败：{format_missing(missing)}。"
        return f"自动合成 {item} 失败：{error_reason(action_result, payload)}。"

    count = payload.get("crafted_item_count") or payload.get("craft_count") or 1
    table = "需要合成台" if payload.get("requires_table") else "不需要合成台"
    consumed = format_items(payload.get("consumed_ingredients"))
    return f"已自动选择 recipe_index={selected} 合成 {item} x{count}，消耗 {consumed}，{table}。"
