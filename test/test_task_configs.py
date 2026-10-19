"""任务 config/ 编号初始化文件发现与展开测试。"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tasks import (  # noqa: E402
    SPECS_DIR,
    TaskConfigError,
    expand_task_args,
    list_seed_preset_ids,
    list_task_configs,
    load_spec,
    parse_seeded_task_arg,
    seeded_task_spec,
    task_description_text,
    task_config_dir,
)


def make_config_dir(tmp: Path, *names: str) -> Path:
    folder = tmp / "task"
    config = folder / "config"
    config.mkdir(parents=True)
    for name in names:
        (config / name).write_text(
            "task_id: demo.task\ncategory: debug\nname: Demo\n",
            encoding="utf-8",
        )
    return folder


class TaskConfigDiscoveryTests(unittest.TestCase):
    def test_config_files_sorted_by_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = make_config_dir(Path(tmp), "10.yaml", "2.yaml", "1.yaml", "a.yaml")
            files = list_task_configs(folder)
            self.assertEqual(
                [path.name for path in files],
                ["1.yaml", "2.yaml", "10.yaml", "a.yaml"],
            )

    def test_missing_or_empty_config_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            empty_folder = base / "empty"
            empty_folder.mkdir()
            (empty_folder / "config").mkdir()
            with self.assertRaises(TaskConfigError):
                list_task_configs(empty_folder)
            bare = base / "bare"
            bare.mkdir()
            with self.assertRaises(TaskConfigError):
                list_task_configs(bare)

    def test_task_config_dir_resolves_task_id(self) -> None:
        self.assertEqual(
            task_config_dir("debug"),
            (SPECS_DIR / "debug").resolve(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "custom"
            folder.mkdir()
            self.assertEqual(
                task_config_dir(str(folder)),
                folder.resolve(),
            )
            with self.assertRaises(TaskConfigError):
                task_config_dir("no.such.task")

    def test_expand_runtime_seeded_tasks_default_to_seed_zero(self) -> None:
        for task_id, file_keys in (
            ("mining", ("schedule_file", "command_file")),
            ("monster_clearance", ("schedule_file", "command_file")),
            ("trade", ("trade_config_file", "command_file")),
            ("boss_combat", ()),
            ("pvp", ()),
        ):
            with self.subTest(task_id=task_id):
                specs = expand_task_args([task_id])
                self.assertEqual(len(specs), 1)
                spec = specs[0]
                self.assertEqual(spec.task_id, task_id)
                self.assertEqual((spec.params or {}).get("generation_seed"), 0)
                self.assertEqual((spec.metadata or {}).get("generation_seed"), 0)
                self.assertTrue((spec.metadata or {}).get("runtime_seed_generation"))
                self.assertEqual(spec.source_path.name, "seed-0.yaml")
                self.assertTrue(task_description_text(spec))
                for key in file_keys:
                    self.assertNotIn(key, spec.params)

    def test_expand_legacy_task_loads_numbered_configs(self) -> None:
        specs = expand_task_args(["debug"])
        self.assertTrue(specs)
        self.assertEqual({spec.task_id for spec in specs}, {"debug"})
        self.assertEqual(specs[0].description, "")
        self.assertTrue(task_description_text(specs[0]))

    def test_description_loaded_from_task_folder_not_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "demo"
            config = folder / "config"
            config.mkdir(parents=True)
            (folder / "demo.description.txt").write_text(
                "folder task description",
                encoding="utf-8",
            )
            task = config / "1.yaml"
            task.write_text(
                "\n".join(
                    [
                        "task_id: demo",
                        "category: debug",
                        "name: Demo",
                        "description: yaml description must not be used",
                        "agents:",
                        "  count: 1",
                        "  names: [Alice]",
                        "completion:",
                        "  timeout_seconds: 10",
                        "  success:",
                        "    type: survive_until",
                        "    duration_seconds: 1",
                    ]
                ),
                encoding="utf-8",
            )

            spec = load_spec(task)

            self.assertEqual(spec.description, "")
            self.assertEqual(
                spec.description_file.resolve(),
                (folder / "demo.description.txt").resolve(),
            )
            self.assertEqual(task_description_text(spec), "folder task description")

    def test_explicit_yaml_path_keeps_file_mode(self) -> None:
        spec_path = ROOT / "src" / "tasks" / "monster_clearance" / "config" / "0.yaml"
        spec = expand_task_args([str(spec_path)])[0]
        self.assertEqual(spec.source_path, spec_path.resolve())
        self.assertIn("schedule_file", spec.params)
        self.assertIn("command_file", spec.params)
        self.assertEqual(spec.params["generation_seed"], 0)

    def test_expand_mixes_explicit_yaml_and_task_name(self) -> None:
        explicit = SPECS_DIR / "debug" / "config" / "1.yaml"
        specs = expand_task_args([str(explicit), "debug"])
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0].task_id, "debug")
        self.assertEqual(specs[1].task_id, "debug")

    def test_expand_rejects_unknown_task(self) -> None:
        with self.assertRaises(TaskConfigError):
            expand_task_args(["no.such.task"])


class SeedPresetExpansionTests(unittest.TestCase):
    def test_seed_preset_ids_are_limited_to_seedable_tasks(self) -> None:
        self.assertEqual(
            list_seed_preset_ids(),
            ("debug", "mining", "pvp"),
        )

    def test_parse_task_at_seed(self) -> None:
        self.assertEqual(parse_seeded_task_arg("mining@42"), ("mining", 42))
        self.assertEqual(parse_seeded_task_arg("pvp@-1"), ("pvp", -1))
        self.assertIsNone(parse_seeded_task_arg("mining"))

    def test_seeded_mining_expands_to_complete_task_spec(self) -> None:
        spec = seeded_task_spec("mining", 42)
        self.assertEqual(spec.task_id, "mining")
        self.assertEqual(spec.category, "mining")
        self.assertEqual(spec.params["generation_seed"], 42)
        self.assertEqual(spec.environment["seed"], 42)
        self.assertEqual(spec.metadata["generation_seed"], 42)
        self.assertNotIn("schedule_file", spec.params)
        self.assertTrue(task_description_text(spec))
        self.assertEqual(spec.source_path.name, "seed-42.yaml")

    def test_expand_task_args_accepts_seed_option(self) -> None:
        specs = expand_task_args(["mining"], seed=20260810)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].params["generation_seed"], 20260810)
        self.assertEqual(specs[0].name, "Dynamic Mineral Collection Quick (seed 20260810)")

    def test_seed_option_uses_episode_window_for_resume(self) -> None:
        specs = expand_task_args(
            ["mining"],
            seed=10,
            start_episode=3,
            episode_count=2,
        )
        self.assertEqual(
            [spec.params["generation_seed"] for spec in specs],
            [10, 11, 12, 13],
        )
        selected = specs[2:4]
        self.assertEqual(
            [spec.params["generation_seed"] for spec in selected],
            [12, 13],
        )

    def test_expand_task_args_accepts_task_at_seed(self) -> None:
        specs = expand_task_args(["pvp@7"])
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].task_id, "pvp")
        self.assertEqual(specs[0].params["generation_seed"], 7)
        self.assertEqual(specs[0].agents["count"], 8)

    def test_task_at_seed_can_expand_multiple_episodes(self) -> None:
        specs = expand_task_args(["pvp@7"], episode_count=3)
        self.assertEqual(
            [spec.params["generation_seed"] for spec in specs],
            [7, 8, 9],
        )

    def test_multiple_explicit_seeded_tasks_stay_explicit(self) -> None:
        specs = expand_task_args(["mining@3", "mining@9"], episode_count=1)
        self.assertEqual(
            [spec.params["generation_seed"] for spec in specs],
            [3, 9],
        )

    def test_seed_option_does_not_change_old_task_config_flow(self) -> None:
        old = expand_task_args(["mining"])[0]
        seeded = expand_task_args(["mining"], seed=99)[0]
        self.assertEqual(old.params["generation_seed"], 0)
        self.assertEqual(seeded.params["generation_seed"], 99)

    def test_seed_option_supports_runtime_seeded_tasks(self) -> None:
        trade = expand_task_args(["trade"], seed=1)[0]
        monster = expand_task_args(["monster_clearance"], seed=1)[0]
        self.assertEqual(trade.params["generation_seed"], 1)
        self.assertEqual(monster.params["generation_seed"], 1)
        self.assertNotIn("trade_config_file", trade.params)
        self.assertNotIn("schedule_file", monster.params)

    def test_seed_option_rejects_ambiguous_seed_sources(self) -> None:
        with self.assertRaises(TaskConfigError):
            expand_task_args(["mining", "debug"], seed=1)
        with self.assertRaises(TaskConfigError):
            expand_task_args(["mining@1"], seed=2)


if __name__ == "__main__":
    unittest.main()
