"""Simple instruction layer: short aliases with essential params for LLM actions.

The LLM only writes a short instruction name and the important parameters;
the module maps the alias back to the canonical ``<skill>.<command>`` action,
enforces the essential parameters, fills manifest defaults, and picks the
execution mode.  Canonical action names remain callable as a fallback.

Some entries support variable-length ``item, num`` arguments (all-positional
calls) where a bare trailing item uses the entry's default count policy.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import keyword
from pathlib import Path
import re
from typing import Any

from .registry import SkillRegistry


DEFAULT_INSTRUCTIONS_PATH = Path(__file__).resolve().parent / "simple_instructions.json"
_POSITION_FIELDS = ("x", "y", "z")
_BOSS_ENTITY_NAMES = frozenset({"wither", "ender_dragon"})


class SimpleInstructionError(ValueError):
    """Simple instruction catalog or expansion error."""


@dataclass(frozen=True)
class SimpleInstruction:
    """One curated simple instruction entry."""

    alias: str
    action: str
    params: tuple[Any, ...]
    description: str = ""
    description_en: str = ""
    variadic: tuple[Any, ...] | None = None
    default_count: int | str = 1
    optional: tuple[str, ...] = ()

    def description_for(self, language: str) -> str:
        """Return the description for the requested language (zh is canonical)."""
        if language == "en":
            return self.description_en or self.description
        return self.description or self.description_en


def _normalize_params(raw: Any) -> tuple[Any, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SimpleInstructionError("params 必须是字符串或字符串列表的列表")
    result: list[Any] = []
    for item in raw:
        if isinstance(item, str):
            if not item:
                raise SimpleInstructionError("params 不能包含空字符串")
            result.append(item)
        elif isinstance(item, list) and item and all(
            isinstance(name, str) and name for name in item
        ):
            result.append(tuple(dict.fromkeys(item)))
        else:
            raise SimpleInstructionError(f"非法参数项: {item!r}")
    return tuple(result)


def _normalize_default_count(value: Any) -> int | str:
    if value == "all":
        return "all"
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    raise SimpleInstructionError("default_count 必须是 'all' 或 >=1 的整数")


def parse_action_call(text: str) -> tuple[str, dict[str, Any], list[Any]]:
    """Parse one ACTION call text into (name, keyword_params, positional_args).

    ``name`` may be a simple alias or a canonical ``<skill>.<command>`` name;
    keyword and positional arguments are both parsed as Python literals.
    """
    parsed_text = _escape_keyword_callable(text)
    try:
        expression = ast.parse(parsed_text, mode="eval")
    except SyntaxError as exc:
        raise SimpleInstructionError(f"invalid ACTION syntax: {exc.msg}") from exc
    call = expression.body
    if not isinstance(call, ast.Call):
        raise SimpleInstructionError("ACTION must be a function call")
    func = call.func
    if isinstance(func, ast.Name) and isinstance(func.ctx, ast.Load):
        action = func.id
    elif (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and isinstance(func.ctx, ast.Load)
    ):
        action = f"{func.value.id}.{func.attr}"
    else:
        raise SimpleInstructionError(
            "ACTION name must be <skill>.<command> or a simple name"
        )
    action = _unescape_keyword_name(action)
    positional: list[Any] = []
    for arg in call.args:
        try:
            positional.append(_literal_or_name(arg))
        except (ValueError, TypeError) as exc:
            raise SimpleInstructionError(
                "ACTION positional argument must be a literal"
            ) from exc
    params: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise SimpleInstructionError("ACTION does not allow **kwargs")
        if keyword.arg in params:
            raise SimpleInstructionError(f"duplicate ACTION parameter: {keyword.arg}")
        try:
            params[keyword.arg] = _literal_or_name(keyword.value)
        except (ValueError, TypeError) as exc:
            raise SimpleInstructionError(
                f"ACTION parameter {keyword.arg} must be a literal"
            ) from exc
    return action, params, positional


def _literal_or_name(node: ast.AST) -> Any:
    """Evaluate a literal, tolerating simple bare identifiers as strings.

    LLMs frequently omit quotes around short values and dict keys
    (``fight_melee(iron_sword, zombie_65)``, ``{x:-45, y:0, z:-25}``).
    Only plain identifiers (not ``__dunder`` names) are accepted; calls,
    attribute access and other expressions stay rejected.
    """
    if isinstance(node, ast.Name) and node.id.isidentifier() and not node.id.startswith("__"):
        return node.id
    if (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Sub)
        and isinstance(node.left, ast.Name)
        and isinstance(node.right, ast.Name)
    ):
        # 带连字符的实体名（如 Bob）会被解析成减法表达式，恢复为字符串。
        return f"{node.left.id}-{node.right.id}"
    if isinstance(node, ast.Dict):
        result: dict[Any, Any] = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:
                raise ValueError("dict unpacking is not allowed")
            result[_literal_or_name(key_node)] = _literal_or_name(value_node)
        return result
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = [_literal_or_name(item) for item in node.elts]
        if isinstance(node, ast.List):
            return items
        if isinstance(node, ast.Tuple):
            return tuple(items)
        return set(items)
    return ast.literal_eval(node)


def _escape_keyword_callable(text: str) -> str:
    """Escape Python keywords used as the callable name so ast.parse succeeds.

    ``break(position=...)`` or ``block.break(position=...)`` are invalid Python
    expressions because ``break`` is a keyword; the leading callable token is
    prefixed with ``_kw_`` for parsing and restored afterwards.
    """
    dotted = re.match(
        r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)(\s*\()",
        text,
    )
    if dotted and keyword.iskeyword(dotted.group(2)):
        prefix = dotted.group(1) + "."
        index = text.index(prefix) + len(prefix)
        return (
            text[:index]
            + "_kw_"
            + dotted.group(2)
            + text[index + len(dotted.group(2)):]
        )
    bare = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(\s*\()", text)
    if bare and keyword.iskeyword(bare.group(1)):
        return (
            "_kw_"
            + bare.group(1)
            + text[len(bare.group(1)):]
        )
    return text


def _unescape_keyword_name(name: str) -> str:
    return name.replace("._kw_", ".").removeprefix("_kw_")


_CATALOG_CACHE: dict[tuple[int, tuple[str, ...], int, str], str] = {}


def render_action_space(
    registry: SkillRegistry,
    instructions: "SimpleInstructionRegistry | None" = None,
    language: str = "zh",
    *,
    empty_text: str = "没有可用技能。",
) -> str:
    """Render the allowed action space as simple-instruction catalog lines."""
    actions = tuple(
        sorted(
            f"{skill_name}.{command_name}"
            for skill_name, manifest in registry.manifests.items()
            for command_name in manifest.commands
            if registry.is_allowed(f"{skill_name}.{command_name}")
        )
    )
    cache_key = (
        id(registry),
        actions,
        id(instructions) if instructions is not None else 0,
        language,
    )
    cached = _CATALOG_CACHE.get(cache_key)
    if cached is not None:
        return cached
    lines: list[str] = []
    for action in actions:
        _, spec = registry.resolve(action)
        schema = spec.raw.get("params_schema") or {}
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        description = _manifest_description_for(spec.raw, language)
        instruction = (
            instructions.instruction_for_action(action)
            if instructions is not None
            else None
        )
        if instruction is not None:
            display_name = instruction.alias
            if instruction.variadic is not None:
                arguments = [
                    _render_param_group(group) for group in instruction.variadic
                ] + ["item", "num", "..."]
            else:
                arguments = [
                    _render_param_group(group) for group in instruction.params
                ]
            description = (
                instruction.description_for(language) or description
            )
        else:
            display_name = action
            arguments = [
                name
                for name, property_schema in properties.items()
                if name in required
                or not (
                    isinstance(property_schema, dict)
                    and "default" in property_schema
                )
            ]
        signature = f"{display_name}({', '.join(arguments)})"
        lines.append(f"- {signature} : #{description}")
    result = "\n".join(lines) or empty_text
    _CATALOG_CACHE[cache_key] = result
    return result


def _manifest_description_for(raw: dict[str, Any], language: str) -> str:
    if language == "en":
        return str(
            raw.get("llm_description") or raw.get("llm_description_zh") or ""
        ).strip()
    return str(
        raw.get("llm_description_zh") or raw.get("llm_description") or ""
    ).strip()


def _render_param_group(group: str | tuple[str, ...]) -> str:
    return group if isinstance(group, str) else "/".join(group)


def _normalize_variadic(raw: Any) -> tuple[Any, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SimpleInstructionError("variadic 必须是映射")
    prefix = raw.get("prefix")
    if not isinstance(prefix, list) or not prefix:
        raise SimpleInstructionError("variadic.prefix 必须是非空列表")
    normalized = _normalize_params(prefix)
    if not normalized:
        raise SimpleInstructionError("variadic.prefix 不能为空")
    return normalized


class SimpleInstructionCatalog:
    """Load and validate the simple instruction mapping file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_INSTRUCTIONS_PATH
        self.instructions: dict[str, SimpleInstruction] = {}
        self.by_action: dict[str, SimpleInstruction] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SimpleInstructionError(f"无法加载简单指令映射 {self.path}: {exc}") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise SimpleInstructionError("simple_instructions.json version 必须为 1")
        entries = data.get("instructions")
        if not isinstance(entries, list):
            raise SimpleInstructionError("instructions 必须是列表")
        for entry in entries:
            if not isinstance(entry, dict):
                raise SimpleInstructionError("instructions 元素必须是映射")
            alias = str(entry.get("alias") or "")
            action = str(entry.get("action") or "")
            if not alias or "." in alias:
                raise SimpleInstructionError(f"alias 必须是非空且不含 '.' 的简单名: {alias!r}")
            if action.count(".") != 1 or not all(action.split(".")):
                raise SimpleInstructionError(f"action 必须是 <skill>.<command>: {action!r}")
            if alias in self.instructions:
                raise SimpleInstructionError(f"alias 重复: {alias}")
            variadic = _normalize_variadic(entry.get("variadic"))
            default_count = _normalize_default_count(
                (entry.get("variadic") or {}).get("default_count", 1)
                if isinstance(entry.get("variadic"), dict)
                else entry.get("default_count", 1)
            )
            instruction = SimpleInstruction(
                alias=alias,
                action=action,
                params=_normalize_params(entry.get("params")),
                description=str(entry.get("description") or ""),
                description_en=str(entry.get("description_en") or ""),
                variadic=variadic,
                default_count=default_count,
                optional=_normalize_params(entry.get("optional")),
            )
            self.instructions[alias] = instruction
            self.by_action.setdefault(action, instruction)


class SimpleInstructionRegistry:
    """Resolve simple names and expand them into canonical actions."""

    def __init__(
        self,
        catalog: SimpleInstructionCatalog | None = None,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self.catalog = catalog or SimpleInstructionCatalog()
        self.skills = skill_registry or SkillRegistry()
        self._validate_entries()

    def _validate_entries(self) -> None:
        for instruction in self.catalog.instructions.values():
            try:
                _, spec = self.skills.resolve(
                    instruction.action,
                    enforce_allowlist=False,
                )
            except (KeyError, PermissionError) as exc:
                raise SimpleInstructionError(
                    f"简单指令 {instruction.alias!r} 指向未知动作 {instruction.action}: {exc}"
                ) from exc
            properties = set(
                (spec.raw.get("params_schema") or {}).get("properties") or {}
            )
            for group in instruction.params:
                names = (group,) if isinstance(group, str) else tuple(group)
                for name in names:
                    if name not in properties and not (
                        name == "position" and set(_POSITION_FIELDS) <= properties
                    ):
                        raise SimpleInstructionError(
                            f"{instruction.action} 没有参数 {name!r}（{instruction.alias}）"
                        )
            if instruction.variadic is not None:
                for group in instruction.variadic:
                    names = (group,) if isinstance(group, str) else tuple(group)
                    for name in names:
                        if name not in properties and not (
                            name == "position" and set(_POSITION_FIELDS) <= properties
                        ):
                            raise SimpleInstructionError(
                                f"{instruction.action} 没有 variadic 前缀参数 {name!r}"
                            )
                if "items" not in properties:
                    raise SimpleInstructionError(
                        f"{instruction.action} 缺少 items 参数（variadic 需要）"
                    )

    def instruction_for_action(self, action: str) -> SimpleInstruction | None:
        return self.catalog.by_action.get(action)

    def resolve_name(self, name: str) -> SimpleInstruction:
        """Resolve a simple alias; canonical ``<skill>.<command>`` falls back."""
        instruction = self.catalog.instructions.get(name)
        if instruction is not None:
            return instruction
        if "." in name:
            return SimpleInstruction(alias=name, action=name, params=())
        raise KeyError(f"未知动作指令: {name!r}")

    def expand(
        self,
        name: str,
        params: dict[str, Any],
        positional: list[Any] | None = None,
    ) -> tuple[str, dict[str, Any], str]:
        """Expand ``name(params)`` into (action, filled_params, mode)."""
        instruction = self.resolve_name(name)
        _, spec = self.skills.resolve(
            instruction.action,
            enforce_allowlist=False,
        )
        schema = spec.raw.get("params_schema") or {}
        properties = schema.get("properties") or {}

        provided = set(params)
        filled = dict(params)
        if instruction.variadic is not None:
            positionals = list(positional or [])
            prefix_groups = instruction.variadic
            if len(positionals) < len(prefix_groups):
                raise SimpleInstructionError(
                    f"{instruction.action} 缺少位置参数前缀 "
                    f"(需要 {len(prefix_groups)} 个，实际 {len(positionals)} 个)"
                )
            for index, group in enumerate(prefix_groups):
                names = (group,) if isinstance(group, str) else tuple(group)
                value = positionals[index]
                target = self._resolve_prefix_name(names, value, properties)
                filled[target] = value
                provided.add(target)
            filled["items"] = self._parse_item_pairs(
                positionals[len(prefix_groups):],
                instruction.default_count,
            )
            provided.add("items")
        elif positional:
            names_by_index = [
                (group,) if isinstance(group, str) else tuple(group)
                for group in instruction.params
            ]
            # position 位置参数兼容：goto(x,y,z) 三参数 或 goto((x,y,z)) 元组
            if (
                positional
                and names_by_index
                and "position" in names_by_index[0]
                and not any(name in provided for name in ("position", "entity_name"))
            ):
                first = positional[0]
                numeric = (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    for value in (first if isinstance(first, (tuple, list)) else ())
                )
                if (
                    isinstance(first, (tuple, list))
                    and len(first) == 3
                    and all(numeric)
                ):
                    filled["position"] = {
                        "x": first[0],
                        "y": first[1],
                        "z": first[2],
                    }
                    provided.add("position")
                    positional = positional[1:]
                elif len(positional) >= 3 and all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    for value in positional[:3]
                ):
                    filled["position"] = {
                        "x": positional[0],
                        "y": positional[1],
                        "z": positional[2],
                    }
                    provided.add("position")
                    positional = positional[3:]
            if len(positional) > len(names_by_index):
                raise SimpleInstructionError(
                    f"{instruction.action} 位置参数过多 "
                    f"(需要 <= {len(names_by_index)}，实际 {len(positional)})"
                )
            for index, value in enumerate(positional):
                names = names_by_index[index]
                candidates = [entry for entry in names if entry not in provided] or list(names)
                # 位置参数智能绑定：goto(实体名) 字符串应绑 entity_name，坐标对象绑 position
                if (
                    "position" in candidates
                    and "entity_name" in candidates
                    and not isinstance(value, dict)
                ):
                    target = "entity_name"
                else:
                    target = candidates[0]
                filled[target] = value
                provided.add(target)

        optional_names = set(instruction.optional)
        for group in instruction.params:
            names = (group,) if isinstance(group, str) else tuple(group)
            if not any(entry in provided for entry in names):
                # 标记为 optional 的参数允许缺省
                if not any(name in optional_names for name in names):
                    raise SimpleInstructionError(
                        f"{instruction.action} 缺少必要参数: {'/'.join(names)}"
                    )

        if "position" in filled and "position" not in properties:
            if set(_POSITION_FIELDS) <= set(properties):
                position = filled.pop("position")
                if not isinstance(position, dict):
                    raise SimpleInstructionError("position 必须是 {x, y, z} 对象")
                for key in _POSITION_FIELDS:
                    if key not in position:
                        raise SimpleInstructionError(f"position 缺少字段 {key}")
                    filled[key] = position[key]

        # combat 类简写的 target 若给字符串（常见省略引号或只给实体名），
        # 包装为实体对象；观测渲染的 "<种类>_<id>"（如 zombie_79）解析出
        # entity_id，供技能按实体 ID 精确匹配。
        if instruction.action.startswith("combat.") and isinstance(
            filled.get("target"),
            (str, int),
        ):
            filled["target"] = _wrap_combat_target(filled["target"])

        for name_, property_schema in properties.items():
            if (
                name_ not in filled
                and isinstance(property_schema, dict)
                and "default" in property_schema
            ):
                filled[name_] = property_schema["default"]

        modes = set(spec.modes)
        if "continuous" in modes:
            mode = "continuous"
        elif "oneshot" in modes:
            mode = "oneshot"
        else:
            raise SimpleInstructionError(f"{instruction.action} 没有支持的执行模式")
        return instruction.action, filled, mode

    @staticmethod
    def _resolve_prefix_name(
        names: tuple[str, ...],
        value: Any,
        properties: dict[str, Any],
    ) -> str:
        if len(names) == 1:
            return names[0]
        if isinstance(value, bool):
            return names[0]
        if isinstance(value, int):
            for name in names:
                if (properties.get(name) or {}).get("type") == "integer":
                    return name
            return names[0]
        if isinstance(value, dict):
            if "position" in value and "chest" in names:
                return "chest"
            if "position" in names:
                return "position"
            if "chest" in names:
                return "chest"
        return names[0]

    @staticmethod
    def _parse_item_pairs(
        args: list[Any],
        default_count: int | str,
    ) -> list[Any]:
        items: list[Any] = []
        index = 0
        while index < len(args):
            item = args[index]
            if not isinstance(item, str) or not item:
                raise SimpleInstructionError("不定长 item 参数必须是字符串")
            if (
                index + 1 < len(args)
                and isinstance(args[index + 1], int)
                and not isinstance(args[index + 1], bool)
                and args[index + 1] >= 1
            ):
                count = args[index + 1]
                index += 2
                items.append({"name": item, "count": count})
            else:
                if default_count == "all":
                    items.append(item)
                else:
                    items.append({"name": item, "count": default_count})
                index += 1
        if not items:
            raise SimpleInstructionError("至少需要一个 item")
        return items


def _wrap_combat_target(value: str) -> dict[str, Any]:
    """Wrap a combat target string/int into an entity object.

    ``zombie_79`` (the observation label ``<kind>_<entity_id>``) becomes
    ``{"name": "zombie", "entity_id": 79}`` so the skill can resolve the exact
    entity; plain names such as ``zombie`` stay ``{"name": "zombie"}``; a bare
    numeric id such as ``109`` becomes ``{"entity_id": 109}``.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return {"entity_id": value}
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)_(\d+)", value)
    if match:
        return {"name": match.group(1), "entity_id": int(match.group(2))}
    compact_boss = re.fullmatch(r"(wither|ender_dragon)_.+", value, re.IGNORECASE)
    if compact_boss:
        return {"name": compact_boss.group(1).lower()}
    return {"name": value}


__all__ = [
    "DEFAULT_INSTRUCTIONS_PATH",
    "SimpleInstruction",
    "SimpleInstructionCatalog",
    "SimpleInstructionError",
    "SimpleInstructionRegistry",
    "parse_action_call",
    "render_action_space",
]
