#!/usr/bin/env python3
"""Paper-style JSON decision protocol tests for the PPA baseline."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import ActionMode, SkillRegistry  # noqa: E402
from src.baseline.Parallelized import JsonDecisionCodec, PPAProtocolError  # noqa: E402


class JsonDecisionCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        skill_dir = Path(self.temp.name) / "demo"
        skill_dir.mkdir()
        manifest = {
            "name": "demo",
            "version": "1.0",
            "runtime": "javascript",
            "entry": "skill.js",
            "commands": {
                "act": {
                    "mode": ["oneshot", "continuous"],
                    "params_schema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "minLength": 2},
                            "count": {"type": "integer", "minimum": 1},
                            "kind": {"type": "string", "enum": ["a", "b"]},
                            "items": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "number", "minimum": 0},
                            },
                            "options": {
                                "type": "object",
                                "properties": {"enabled": {"type": "boolean"}},
                                "required": ["enabled"],
                                "additionalProperties": False,
                            },
                            "choice": {
                                "anyOf": [
                                    {"type": "string", "minLength": 1},
                                    {"type": "integer", "minimum": 1},
                                ]
                            },
                            "exclusive": {
                                "oneOf": [
                                    {"type": "string"},
                                    {"type": "integer"},
                                ]
                            },
                            "optional": {"type": "string", "default": "unused"},
                        },
                        "required": [
                            "name",
                            "count",
                            "kind",
                            "items",
                            "options",
                            "choice",
                            "exclusive",
                        ],
                        "additionalProperties": False,
                    },
                },
                "once": {
                    "mode": ["oneshot"],
                    "params_schema": {
                        "type": "object",
                        "additionalProperties": False,
                    },
                },
            },
        }
        (skill_dir / "skill.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        self.registry = SkillRegistry(self.temp.name)
        self.codec = JsonDecisionCodec()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def decision(
        self,
        action: str,
        *,
        interrupt: bool = False,
        reason: str = "test reason",
        extra: dict | None = None,
    ) -> str:
        payload = {"skill": action, "interrupt": interrupt, "reason": reason}
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    def test_parses_nested_literals_and_prefers_continuous(self) -> None:
        proposal = self.codec.parse(
            self.decision(
                "demo.act(name='ok', count=2, kind='a', items=[0, 1.5], "
                "options={'enabled': True}, choice=3, exclusive='x')",
                interrupt=True,
            ),
            agent_id="Alice",
            registry=self.registry,
        )
        self.assertEqual(proposal.action_unit.mode, ActionMode.CONTINUOUS)
        self.assertEqual(proposal.action_unit.params["items"], [0, 1.5])
        self.assertEqual(proposal.action_unit.params["options"], {"enabled": True})
        self.assertTrue(proposal.interrupt)
        self.assertEqual(proposal.reason, "test reason")
        self.assertIsNone(proposal.passive_message)
        self.assertNotIn("optional", proposal.action_unit.params)
        self.assertEqual(proposal.input_params["items"], [0, 1.5])
        self.assertEqual(proposal.input_positional, [])

    def test_accepts_json_code_fence_and_ignores_extra_fields(self) -> None:
        text = "```json\n" + self.decision(
            "demo.once()",
            reason="fenced",
            extra={"message": "future passive message"},
        ) + "\n```"
        proposal = self.codec.parse(
            text,
            agent_id="Alice",
            registry=self.registry,
        )
        self.assertEqual(proposal.action_unit.action, "demo.once")
        self.assertEqual(proposal.action_unit.mode, ActionMode.ONESHOT)
        self.assertEqual(proposal.reason, "fenced")
        self.assertIn('"message"', proposal.raw_text)

    def test_oneshot_is_selected_when_continuous_is_unavailable(self) -> None:
        proposal = self.codec.parse(
            self.decision("demo.once()"),
            agent_id="Alice",
            registry=self.registry,
        )
        self.assertEqual(proposal.action_unit.mode, ActionMode.ONESHOT)

    def test_schema_constraints_are_enforced(self) -> None:
        invalid_calls = {
            "required": "demo.act()",
            "object": (
                "demo.act(name='ok', count=1, kind='a', items=[1], options=[], "
                "choice=1, exclusive='x')"
            ),
            "array": (
                "demo.act(name='ok', count=1, kind='a', items='x', "
                "options={'enabled': True}, choice=1, exclusive='x')"
            ),
            "basic type": (
                "demo.act(name='ok', count=True, kind='a', items=[1], "
                "options={'enabled': True}, choice=1, exclusive='x')"
            ),
            "additionalProperties": (
                "demo.act(name='ok', count=1, kind='a', items=[1], "
                "options={'enabled': True, 'extra': 1}, choice=1, exclusive='x')"
            ),
            "enum": (
                "demo.act(name='ok', count=1, kind='c', items=[1], "
                "options={'enabled': True}, choice=1, exclusive='x')"
            ),
            "minimum": (
                "demo.act(name='ok', count=0, kind='a', items=[1], "
                "options={'enabled': True}, choice=1, exclusive='x')"
            ),
            "minLength": (
                "demo.act(name='x', count=1, kind='a', items=[1], "
                "options={'enabled': True}, choice=1, exclusive='x')"
            ),
            "minItems": (
                "demo.act(name='ok', count=1, kind='a', items=[], "
                "options={'enabled': True}, choice=1, exclusive='x')"
            ),
            "items": (
                "demo.act(name='ok', count=1, kind='a', items=[-1], "
                "options={'enabled': True}, choice=1, exclusive='x')"
            ),
            "anyOf": (
                "demo.act(name='ok', count=1, kind='a', items=[1], "
                "options={'enabled': True}, choice=[], exclusive='x')"
            ),
            "oneOf": (
                "demo.act(name='ok', count=1, kind='a', items=[1], "
                "options={'enabled': True}, choice=1, exclusive=True)"
            ),
        }
        for label, action in invalid_calls.items():
            with self.subTest(label=label), self.assertRaises(PPAProtocolError):
                self.codec.parse(
                    self.decision(action),
                    agent_id="Alice",
                    registry=self.registry,
                )

    def test_rejects_code_positional_args_and_unpacking(self) -> None:
        actions = (
            "demo.once(__import__('os').getcwd())",
            "demo.once('position')",
            "demo.once(**{'x': 1})",
            "demo.once(*[])",
        )
        for action in actions:
            with self.subTest(action=action), self.assertRaises(PPAProtocolError):
                self.codec.parse(
                    self.decision(action),
                    agent_id="Alice",
                    registry=self.registry,
                )

    def test_rejects_unknown_disabled_and_bad_json(self) -> None:
        disabled = SkillRegistry(self.temp.name, allowed_actions={"demo.once"})
        cases = (
            (self.decision("missing.act()"), self.registry),
            (self.decision("demo.act()"), disabled),
            ("not json at all", self.registry),
            ("```\n" + self.decision("demo.once()"), self.registry),
            ("```json\n" + self.decision("demo.once()") + "\n```\nextra", self.registry),
        )
        for text, registry in cases:
            with self.subTest(text=text[:40]), self.assertRaises(PPAProtocolError):
                self.codec.parse(text, agent_id="Alice", registry=registry)

    def test_requires_boolean_interrupt_and_reason(self) -> None:
        cases = (
            self.decision("demo.once()", interrupt="true"),  # type: ignore[arg-type]
            self.decision("demo.once()", interrupt=1),  # type: ignore[arg-type]
            self.decision("demo.once()", reason="  "),
            '{"interrupt": false, "reason": "x"}',
            '{"skill": "demo.once()", "reason": "x"}',
            '{"skill": "demo.once()", "interrupt": false}',
            "[]",
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(PPAProtocolError):
                self.codec.parse(text, agent_id="Alice", registry=self.registry)


class JsonDecisionSimpleInstructionTests(unittest.TestCase):
    def test_expands_simple_aliases_and_variadic_calls(self) -> None:
        from src.agents.action import SimpleInstructionRegistry

        codec = JsonDecisionCodec(instructions=SimpleInstructionRegistry())
        proposal = codec.parse(
            json.dumps(
                {
                    "skill": "take_from('personal_box','iron_ingot',3)",
                    "interrupt": True,
                    "reason": "grab",
                },
                ensure_ascii=False,
            ),
            agent_id="a0",
            registry=SkillRegistry(allowed_actions={"trade.take_from_container"}),
        )
        self.assertEqual(proposal.action_unit.action, "trade.take_from_container")
        self.assertEqual(proposal.action_unit.params["container"], "personal_box")
        self.assertEqual(
            proposal.action_unit.params["items"],
            [{"name": "iron_ingot", "count": 3}],
        )
        self.assertEqual(proposal.input_params, {})
        self.assertEqual(
            proposal.input_positional,
            ["personal_box", "iron_ingot", 3],
        )
        self.assertTrue(proposal.interrupt)

    def test_rejects_variadic_keywords_in_json(self) -> None:
        from src.agents.action import SimpleInstructionRegistry

        codec = JsonDecisionCodec(instructions=SimpleInstructionRegistry())
        with self.assertRaises(PPAProtocolError):
            codec.parse(
                json.dumps(
                    {
                        "skill": "take_from(container='personal_box')",
                        "interrupt": False,
                        "reason": "r",
                    }
                ),
                agent_id="a0",
                registry=SkillRegistry(allowed_actions={"trade.take_from_container"}),
            )

    def test_binds_positional_and_bare_identifiers(self) -> None:
        from src.agents.action import SimpleInstructionRegistry

        codec = JsonDecisionCodec(instructions=SimpleInstructionRegistry())
        proposal = codec.parse(
            json.dumps(
                {
                    "skill": "fight_melee(iron_sword, zombie_65)",
                    "interrupt": True,
                    "reason": "close target",
                },
                ensure_ascii=False,
            ),
            agent_id="a0",
            registry=SkillRegistry(allowed_actions={"combat.fight_using_weapon"}),
        )
        self.assertEqual(proposal.action_unit.action, "combat.fight_using_weapon")
        self.assertEqual(proposal.action_unit.params["weapon"], "iron_sword")
        self.assertEqual(
            proposal.action_unit.params["target"],
            {"name": "zombie", "entity_id": 65},
        )
        self.assertEqual(
            proposal.input_positional,
            ["iron_sword", "zombie_65"],
        )


if __name__ == "__main__":
    unittest.main()
