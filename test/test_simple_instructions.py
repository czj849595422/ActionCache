"""Tests for the simple instruction layer (alias -> canonical action)."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import (  # noqa: E402
    ActionMode,
    FunctionCallTextCodec,
    SimpleInstructionCodec,
    SkillRegistry,
    TextProtocolError,
    parse_action_call,
    render_action_space,
)
from src.agents.action.simple_instructions import (  # noqa: E402
    SimpleInstructionCatalog,
    SimpleInstructionError,
    SimpleInstructionRegistry,
)
def registry() -> SimpleInstructionRegistry:
    return SimpleInstructionRegistry()


class SimpleInstructionCatalogTests(unittest.TestCase):
    def test_real_catalog_loads_with_expected_entries(self) -> None:
        reg = registry()
        self.assertEqual(len(reg.catalog.instructions), 65)
        self.assertEqual(len(reg.catalog.by_action), 65)
        chat = reg.catalog.instructions["chat"]
        self.assertEqual(chat.action, "chat.say")
        self.assertEqual(chat.params, ("message",))
        self.assertEqual(chat.description, "通信发送message。")
        self.assertTrue(chat.description_en)
        self.assertEqual(chat.description_for("zh"), chat.description)
        self.assertEqual(chat.description_for("en"), chat.description_en)
        self.assertEqual(reg.catalog.instructions["use_food"].action, "combat.use_food")
        self.assertEqual(
            reg.catalog.instructions["use_food"].description,
            "使用背包内的食物或药水；治疗药水写作 potion[healing]。",
        )
        self.assertEqual(
            reg.catalog.instructions["fight"].description,
            "靠近target至进入攻击范围，并攻击 target（target 可写 <种类>_<id>，如 zombie_73，或对象 {name, entity_id}；武器可选，缺省使用当前手持或空手；战斗任务中 weapon 可使用装备注册标识，如 diamond_sword_1，与当前所持武器不同时自动切换；结果会说明“<target>已被消灭”或“<target>依然存活”）。",
        )
        observe = reg.catalog.instructions["observe"]
        self.assertEqual(observe.action, "combat.observe")
        self.assertEqual(observe.params, ())
        self.assertEqual(observe.description, "观察周围，寻找是否存在敌对实体与掉落物。")
        pick = reg.catalog.instructions["pick"]
        self.assertEqual(pick.action, "inventory.pick")
        self.assertEqual(pick.params, ("positions",))
        self.assertEqual(pick.description, "按指定位置拾取掉落物。")
        drop = reg.catalog.instructions["drop"]
        self.assertEqual(drop.action, "inventory.drop")
        self.assertEqual(drop.params, ("items",))
        self.assertEqual(drop.description, "丢弃items中的物品到当前位置地面。")
        give = reg.catalog.instructions["give"]
        self.assertEqual(give.action, "inventory.give")
        self.assertEqual(give.params, ("target", "items"))
        self.assertEqual(give.description, "靠近target并把items中的物品丢给对方拾取。")
        self.assertEqual(reg.catalog.instructions["eat"].action, "survival.use_food")
        self.assertEqual(
            reg.catalog.instructions["eat"].description,
            "使用食物或药水food；治疗药水写作 potion[healing]。",
        )
        self.assertEqual(
            reg.catalog.instructions["goto"].params,
            (("position", "entity_name"),),
        )
        take_from = reg.catalog.instructions["take_from"]
        self.assertEqual(take_from.action, "trade.take_from_container")
        self.assertEqual(take_from.variadic, ("container",))
        self.assertEqual(take_from.default_count, "all")
        store_to = reg.catalog.instructions["store_to"]
        self.assertEqual(store_to.action, "trade.store_to_container")
        self.assertEqual(reg.catalog.instructions["transfer"].default_count, "all")
        pick = reg.catalog.instructions["pick_from_chest"]
        self.assertEqual(pick.variadic, (("position", "chest", "chest_id"),))
        self.assertEqual(pick.default_count, 1)
        aliases = set(reg.catalog.instructions)
        self.assertNotIn("lock_nearest_hostile", aliases)
        self.assertNotIn("flee_nearest_hostile", aliases)
        self.assertNotIn("register_chests", aliases)

    def test_invalid_catalog_raises(self) -> None:
        base = {
            "version": 1,
            "instructions": [{"alias": "chat", "action": "chat.say", "params": ["message"]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "instructions.json"

            data = dict(base)
            data["version"] = 2
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SimpleInstructionError):
                SimpleInstructionRegistry(SimpleInstructionCatalog(path))

            data = dict(base)
            data["instructions"] = [
                {"alias": "chat", "action": "chat.say", "params": ["message"]},
                {"alias": "chat", "action": "chat.whisper", "params": ["message"]},
            ]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SimpleInstructionError):
                SimpleInstructionCatalog(path)

            data = dict(base)
            data["instructions"] = [
                {"alias": "chat.say", "action": "chat.say", "params": ["message"]}
            ]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SimpleInstructionError):
                SimpleInstructionCatalog(path)

            data = dict(base)
            data["instructions"] = [
                {"alias": "missing", "action": "missing.nope", "params": []}
            ]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SimpleInstructionError):
                SimpleInstructionRegistry(SimpleInstructionCatalog(path))

            data = dict(base)
            data["instructions"] = [
                {"alias": "bad", "action": "wait.wait", "params": ["nonexistent"]}
            ]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SimpleInstructionError):
                SimpleInstructionRegistry(SimpleInstructionCatalog(path))

            data = dict(base)
            data["instructions"] = [
                {
                    "alias": "badvar",
                    "action": "wait.wait",
                    "params": ["seconds"],
                    "variadic": {"prefix": ["nonexistent"]},
                }
            ]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SimpleInstructionError):
                SimpleInstructionRegistry(SimpleInstructionCatalog(path))

            data = dict(base)
            data["instructions"] = [
                {
                    "alias": "badcount",
                    "action": "wait.wait",
                    "params": ["seconds"],
                    "variadic": {"prefix": ["seconds"], "default_count": 0},
                }
            ]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SimpleInstructionError):
                SimpleInstructionRegistry(SimpleInstructionCatalog(path))


class SimpleInstructionExpandTests(unittest.TestCase):
    def test_expand_fills_defaults_and_chooses_mode(self) -> None:
        action, filled, mode = registry().expand(
            "break",
            {"position": {"x": 0, "y": 1, "z": 0}},
        )
        self.assertEqual(action, "block.break")
        self.assertEqual(filled["max_distance"], 45)
        self.assertEqual(filled["collect_drops"], True)
        self.assertEqual(mode, ActionMode.ONESHOT)

    def test_position_translates_for_schema_without_position(self) -> None:
        reg = registry()
        action, filled, _ = reg.expand(
            "goto",
            {"position": {"x": 1, "y": 2, "z": 3}},
        )
        self.assertEqual(action, "navigation.goto_position")
        self.assertEqual(filled["x"], 1)
        self.assertEqual(filled["y"], 2)
        self.assertEqual(filled["z"], 3)
        self.assertNotIn("position", filled)

        action, filled, _ = reg.expand(
            "follow",
            {"position": {"x": 4, "y": 5, "z": 6}},
        )
        self.assertEqual(action, "navigation.follow_position")
        self.assertEqual(filled["x"], 4)

    def test_goto_position_positional_formats(self) -> None:
        cases = (
            ("goto(0,0,0)", {"x": 0, "y": 0, "z": 0}),
            ("goto((0,0,0))", {"x": 0, "y": 0, "z": 0}),
            ("goto((-1.5,0.0,-11.5))", {"x": -1.5, "y": 0.0, "z": -11.5}),
            ("goto({x:-45,y:0,z:-25})", {"x": -45, "y": 0, "z": -25}),
            ("goto(position={'x':1,'y':0,'z':2})", {"x": 1, "y": 0, "z": 2}),
        )
        reg = registry()
        for call, expected in cases:
            with self.subTest(call=call):
                name, params, positional = parse_action_call(call)
                action, filled, _ = reg.expand(name, params, positional)
                self.assertEqual(action, "navigation.goto_position")
                self.assertEqual(filled, expected)

    def test_one_of_essential_group(self) -> None:
        reg = registry()
        _, filled, _ = reg.expand("goto", {"entity_name": "zombie"})
        self.assertEqual(filled["entity_name"], "zombie")
        with self.assertRaises(SimpleInstructionError):
            reg.expand("goto", {})

    def test_missing_essential_raises(self) -> None:
        with self.assertRaises(SimpleInstructionError):
            registry().expand("chat", {})
        with self.assertRaises(SimpleInstructionError):
            registry().expand("break", {})

    def test_invalid_position_shape_raises(self) -> None:
        with self.assertRaises(SimpleInstructionError):
            registry().expand("goto", {"position": "1,2,3"})

    def test_canonical_name_fallback(self) -> None:
        action, filled, mode = registry().expand(
            "combat.patrol",
            {},
        )
        self.assertEqual(action, "combat.patrol")
        self.assertEqual(mode, ActionMode.ONESHOT)
        observe_action, observe_filled, observe_mode = registry().expand("observe", {})
        self.assertEqual(observe_action, "combat.observe")
        self.assertEqual(observe_filled, {})
        self.assertEqual(observe_mode, ActionMode.ONESHOT)
        with self.assertRaises(KeyError):
            registry().expand("no_such_alias", {})

    def test_use_food_maps_to_combat(self) -> None:
        action, filled, _ = registry().expand("use_food", {"food": "apple"})
        self.assertEqual(action, "combat.use_food")
        self.assertEqual(filled["food"], "apple")

    def test_pick_maps_to_inventory_and_defaults_mode(self) -> None:
        action, filled, mode = registry().expand(
            "pick",
            {"positions": [{"x": 1, "y": 0, "z": 2}]},
        )
        self.assertEqual(action, "inventory.pick")
        self.assertEqual(filled["positions"], [{"x": 1, "y": 0, "z": 2}])
        self.assertEqual(filled["mode"], 1)
        self.assertEqual(mode, ActionMode.CONTINUOUS)

    def test_drop_give_map_to_inventory_actions(self) -> None:
        action, filled, mode = registry().expand(
            "drop",
            {"items": ["bread", {"name": "arrow", "count": 8}]},
        )
        self.assertEqual(action, "inventory.drop")
        self.assertEqual(
            filled["items"],
            ["bread", {"name": "arrow", "count": 8}],
        )
        self.assertEqual(mode, ActionMode.ONESHOT)

        action2, filled2, mode2 = registry().expand(
            "give",
            {},
            ["Bob", ["bread"]],
        )
        self.assertEqual(action2, "inventory.give")
        self.assertEqual(filled2["target"], "Bob")
        self.assertEqual(filled2["items"], ["bread"])
        self.assertEqual(mode2, ActionMode.CONTINUOUS)

    def test_fight_maps_target_first_and_weapon_optional(self) -> None:
        action, filled, mode = registry().expand(
            "fight",
            {},
            ["zombie_65", "iron_sword"],
        )
        self.assertEqual(action, "combat.fight")
        self.assertEqual(filled["target"], {"name": "zombie", "entity_id": 65})
        self.assertEqual(filled["weapon"], "iron_sword")
        self.assertEqual(mode, ActionMode.CONTINUOUS)

        action2, filled2, _ = registry().expand(
            "fight",
            {"target": {"name": "zombie"}},
        )
        self.assertEqual(action2, "combat.fight")
        self.assertNotIn("weapon", filled2)
        self.assertEqual(filled2["max_duration_ms"], 20000)

    def test_fight_accepts_compact_boss_fallback_identifier(self) -> None:
        action, filled, _ = registry().expand(
            "fight",
            {},
            ["wither_boss"],
        )

        self.assertEqual(action, "combat.fight")
        self.assertEqual(filled["target"], {"name": "wither"})

    def test_fight_normalizes_legacy_boss_uuid_identifier(self) -> None:
        action, filled, _ = registry().expand(
            "fight",
            {},
            ["wither_73479e72-1111-2222-3333-444444444444"],
        )

        self.assertEqual(action, "combat.fight")
        self.assertEqual(filled["target"], {"name": "wither"})

    def test_equip_unequip_map_to_batch_commands(self) -> None:
        action, filled, mode = registry().expand(
            "equip",
            {},
            ["diamond_sword"],
        )
        self.assertEqual(action, "equipment.equip_items")
        self.assertEqual(filled["items"], "diamond_sword")
        self.assertEqual(mode, ActionMode.ONESHOT)

        action2, filled2, _ = registry().expand(
            "equip",
            {"items": ["iron_sword", "bow"]},
        )
        self.assertEqual(action2, "equipment.equip_items")
        self.assertEqual(filled2["items"], ["iron_sword", "bow"])

        action3, filled3, _ = registry().expand("unequip", {}, ["head"])
        self.assertEqual(action3, "equipment.unequip_items")
        self.assertEqual(filled3["destinations"], "head")

        action4, filled4, _ = registry().expand(
            "unequip",
            {"destinations": ["head", "legs"]},
        )
        self.assertEqual(action4, "equipment.unequip_items")
        self.assertEqual(filled4["destinations"], ["head", "legs"])

    def test_variadic_expand_trade(self) -> None:
        reg = registry()
        action, filled, mode = reg.expand(
            "take_from",
            {},
            ["personal_box", "iron_ingot", 3, "coal"],
        )
        self.assertEqual(action, "trade.take_from_container")
        self.assertEqual(filled["container"], "personal_box")
        self.assertEqual(
            filled["items"],
            [{"name": "iron_ingot", "count": 3}, "coal"],
        )
        self.assertEqual(mode, ActionMode.ONESHOT)

        _, filled, _ = reg.expand("store_to", {}, ["shop", "diamond", 2])
        self.assertEqual(filled["container"], "shop")
        self.assertEqual(filled["items"], [{"name": "diamond", "count": 2}])

        _, filled, _ = reg.expand("transfer", {}, ["shop", "take", "a", 2])
        self.assertEqual(filled["direction"], "take")
        self.assertEqual(filled["items"], [{"name": "a", "count": 2}])

    def test_variadic_consecutive_bare_items(self) -> None:
        _, filled, _ = registry().expand(
            "take_from",
            {},
            ["shop", "a", "b", 3],
        )
        self.assertEqual(
            filled["items"],
            ["a", {"name": "b", "count": 3}],
        )

    def test_variadic_container_default_one(self) -> None:
        reg = registry()
        _, filled, _ = reg.expand(
            "pick_from_chest",
            {},
            [{"x": 1, "y": 2, "z": 3}, "iron_ingot", "coal", 2],
        )
        self.assertEqual(filled["position"], {"x": 1, "y": 2, "z": 3})
        self.assertEqual(
            filled["items"],
            [
                {"name": "iron_ingot", "count": 1},
                {"name": "coal", "count": 2},
            ],
        )

        _, filled, _ = reg.expand(
            "pick_from_chest",
            {},
            [5, "coal"],
        )
        self.assertEqual(filled["chest_id"], 5)
        self.assertEqual(filled["items"], [{"name": "coal", "count": 1}])

    def test_variadic_requires_prefix_and_item(self) -> None:
        reg = registry()
        with self.assertRaises(SimpleInstructionError):
            reg.expand("take_from", {}, [])
        with self.assertRaises(SimpleInstructionError):
            reg.expand("take_from", {}, ["shop"])

    def test_non_variadic_binds_positional_by_param_order(self) -> None:
        action, filled, mode = registry().expand("chat", {}, ["hi"])
        self.assertEqual(action, "chat.say")
        self.assertEqual(filled, {"message": "hi"})
        self.assertEqual(mode, ActionMode.ONESHOT)

    def test_positional_too_many_raises(self) -> None:
        with self.assertRaises(SimpleInstructionError):
            registry().expand("auto_equip", {}, ["iron_sword"])


class SimpleInstructionCodecTests(unittest.TestCase):
    def decision(self, action: str, *, interrupt: str = "NO", message: str = "-") -> str:
        return (
            f"ACTION: {action}\n"
            f"INTERRUPT: {interrupt}\n"
            "REASON: test reason\n"
            f"MESSAGE: {message}"
        )

    def test_parses_alias_and_passthrough_fields(self) -> None:
        codec = SimpleInstructionCodec()
        decision = codec.parse(
            self.decision("chat(message='hi')", interrupt="YES", message="team"),
            agent_id="a0",
            registry=SkillRegistry(allowed_actions={"chat.say"}),
        )
        self.assertEqual(decision.action, "chat.say")
        self.assertEqual(decision.mode, ActionMode.ONESHOT)
        self.assertEqual(decision.params, {"message": "hi"})
        self.assertEqual(decision.input_params, {"message": "hi"})
        self.assertEqual(decision.input_positional, [])
        self.assertTrue(decision.interrupt)
        self.assertEqual(decision.passive_message, "team")
        self.assertEqual(decision.reason, "test reason")

    def test_parses_canonical_name(self) -> None:
        decision = SimpleInstructionCodec().parse(
            self.decision("wait.wait(seconds=0)"),
            agent_id="a0",
            registry=SkillRegistry(allowed_actions={"wait.wait"}),
        )
        self.assertEqual(decision.action, "wait.wait")
        self.assertEqual(decision.mode, ActionMode.CONTINUOUS)

    def test_disallowed_alias_raises(self) -> None:
        codec = SimpleInstructionCodec()
        with self.assertRaises(TextProtocolError):
            codec.parse(
                self.decision("attack(target={'id': 1})"),
                agent_id="a0",
                registry=SkillRegistry(allowed_actions={"chat.say"}),
            )

    def test_rejects_markdown_and_bad_lines(self) -> None:
        codec = SimpleInstructionCodec()
        with self.assertRaises(TextProtocolError):
            codec.parse(
                "```\n" + self.decision("chat(message='hi')") + "\n```",
                agent_id="a0",
                registry=SkillRegistry(allowed_actions={"chat.say"}),
            )
        with self.assertRaises(TextProtocolError):
            codec.parse(
                "ACTION: chat(message='hi')\nINTERRUPT: NO\nREASON: x",
                agent_id="a0",
                registry=SkillRegistry(allowed_actions={"chat.say"}),
            )

    def test_parses_variadic_action_line(self) -> None:
        codec = SimpleInstructionCodec()
        decision = codec.parse(
            "ACTION: take_from('personal_box','iron_ingot',3)\n"
            "INTERRUPT: NO\nREASON: r\nMESSAGE: -",
            agent_id="a0",
            registry=SkillRegistry(allowed_actions={"trade.take_from_container"}),
        )
        self.assertEqual(decision.action, "trade.take_from_container")
        self.assertEqual(decision.params["container"], "personal_box")
        self.assertEqual(
            decision.params["items"],
            [{"name": "iron_ingot", "count": 3}],
        )
        self.assertEqual(decision.input_params, {})
        self.assertEqual(
            decision.input_positional,
            ["personal_box", "iron_ingot", 3],
        )

    def test_variadic_rejects_keywords(self) -> None:
        codec = SimpleInstructionCodec()
        with self.assertRaises(TextProtocolError):
            codec.parse(
                "ACTION: take_from(container='personal_box')\n"
                "INTERRUPT: NO\nREASON: r\nMESSAGE: -",
                agent_id="a0",
                registry=SkillRegistry(allowed_actions={"trade.take_from_container"}),
            )

    def test_legacy_codec_still_rejects_positional(self) -> None:
        with self.assertRaises(TextProtocolError):
            FunctionCallTextCodec().parse(
                "ACTION: chat.say('hi')\n"
                "INTERRUPT: NO\nREASON: r\nMESSAGE: -",
                agent_id="a0",
                registry=SkillRegistry(allowed_actions={"chat.say"}),
            )

    def test_bare_identifier_positional_treated_as_string(self) -> None:
        decision = SimpleInstructionCodec().parse(
            self.decision("fight_melee(iron_sword, zombie_65)"),
            agent_id="a0",
            registry=SkillRegistry(allowed_actions={"combat.fight_using_weapon"}),
        )
        self.assertEqual(decision.action, "combat.fight_using_weapon")
        self.assertEqual(decision.params["weapon"], "iron_sword")
        self.assertEqual(
            decision.params["target"],
            {"name": "zombie", "entity_id": 65},
        )

    def test_combat_target_plain_name_stays_name(self) -> None:
        decision = SimpleInstructionCodec().parse(
            self.decision("fight_melee('iron_sword', 'zombie')"),
            agent_id="a0",
            registry=SkillRegistry(allowed_actions={"combat.fight_using_weapon"}),
        )
        self.assertEqual(decision.params["target"], {"name": "zombie"})

    def test_combat_target_numeric_id_wraps_entity_id(self) -> None:
        decision = SimpleInstructionCodec().parse(
            self.decision("attack(109)"),
            agent_id="a0",
            registry=SkillRegistry(allowed_actions={"combat.attack"}),
        )
        self.assertEqual(decision.action, "combat.attack")
        self.assertEqual(decision.params["target"], {"entity_id": 109})


class SharedSimpleActionApiTests(unittest.TestCase):
    def test_parse_action_call(self) -> None:
        name, params, positional = parse_action_call(
            "take_from('personal_box','iron_ingot',3)"
        )
        self.assertEqual(name, "take_from")
        self.assertEqual(params, {})
        self.assertEqual(positional, ["personal_box", "iron_ingot", 3])

        name, params, positional = parse_action_call("chat(message='hi')")
        self.assertEqual(name, "chat")
        self.assertEqual(params, {"message": "hi"})
        self.assertEqual(positional, [])

        with self.assertRaises(SimpleInstructionError):
            parse_action_call("chat(**{'message': 'hi'})")
        with self.assertRaises(SimpleInstructionError):
            parse_action_call("chat(")

    def test_parse_action_call_escapes_python_keywords(self) -> None:
        name, params, positional = parse_action_call(
            "break(position={'x':0,'y':1,'z':0})"
        )
        self.assertEqual(name, "break")
        self.assertEqual(params, {"position": {"x": 0, "y": 1, "z": 0}})
        self.assertEqual(positional, [])

        name, params, positional = parse_action_call(
            "block.break(position={'x':0})"
        )
        self.assertEqual(name, "block.break")
        self.assertEqual(params, {"position": {"x": 0}})

    def test_parse_action_call_accepts_bare_identifier_dict_keys(self) -> None:
        name, params, positional = parse_action_call(
            "goto({x:-45, y:0, z:-25})"
        )
        self.assertEqual(name, "goto")
        self.assertEqual(params, {})
        self.assertEqual(positional, [{"x": -45, "y": 0, "z": -25}])

        name, params, positional = parse_action_call(
            "pick([{x:-2,y:0,z:-3}])"
        )
        self.assertEqual(name, "pick")
        self.assertEqual(positional, [[{"x": -2, "y": 0, "z": -3}]])

    def test_parse_action_call_recovers_hyphenated_entity_names(self) -> None:
        name, params, positional = parse_action_call(
            "fight_bow(bow='bow', target=Bob)"
        )
        self.assertEqual(name, "fight_bow")
        self.assertEqual(params, {"bow": "bow", "target": "Bob"})
        self.assertEqual(positional, [])

    def test_codec_parses_break_alias(self) -> None:
        decision = SimpleInstructionCodec().parse(
            "ACTION: break(position={'x':0,'y':1,'z':0})\n"
            "INTERRUPT: NO\nREASON: r\nMESSAGE: -",
            agent_id="a0",
            registry=SkillRegistry(allowed_actions={"block.break"}),
        )
        self.assertEqual(decision.action, "block.break")
        self.assertEqual(
            decision.params["position"],
            {"x": 0, "y": 1, "z": 0},
        )

    def test_render_action_space_bilingual_and_variadic(self) -> None:
        instructions = registry()
        allowed = SkillRegistry(
            allowed_actions={
                "chat.say",
                "trade.take_from_container",
            }
        )
        zh = render_action_space(allowed, instructions, "zh")
        self.assertIn(
            "- chat(message) : #通信发送message。",
            zh,
        )
        self.assertIn(
            "- take_from(container, item, num, ...) : #从container中拿取num_X个item_X。",
            zh,
        )
        en = render_action_space(allowed, instructions, "en")
        self.assertIn(
            "- chat(message) : #Send message to communicate.",
            en,
        )

    def test_render_action_space_uses_zh_manifest_fallback(self) -> None:
        instructions = registry()
        allowed = SkillRegistry(allowed_actions={"equipment.equip_item"})
        zh = render_action_space(allowed, instructions, "zh")
        self.assertIn("equipment.equip_item(item, name, slot, destination)", zh)
        self.assertIn("可使用物品名，或使用背包 slot 槽位编号精确选择同名物品。", zh)
        self.assertNotIn("Equip a named item", zh)

        en = render_action_space(allowed, instructions, "en")
        self.assertIn("Equip an item selected by name or inventory slot", en)


if __name__ == "__main__":
    unittest.main()
