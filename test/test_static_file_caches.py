"""Tests for static prompt and action-result file caches."""

from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import unittest
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import result_postprocessor  # noqa: E402
from src.agents.action.result_postprocessor import (  # noqa: E402
    postprocess_action_result,
)
from src.tasks.spec import TaskSpec  # noqa: E402


class StaticFileCacheTests(unittest.TestCase):
    def test_task_description_is_read_once_per_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            config_dir.mkdir()
            description = root / "description.txt"
            description.write_text("静态任务描述\n", encoding="utf-8")
            spec = TaskSpec.from_dict(
                {
                    "task_id": "cache.test",
                    "category": "mining",
                    "name": "Cache Test",
                    "agents": {"count": 1, "names": ["a0"]},
                    "completion": {
                        "timeout_seconds": 10,
                        "success": {
                            "type": "survive_until",
                            "duration_seconds": 1,
                        },
                    },
                }
            )
            spec._source_path = config_dir / "1.yaml"

            original_read_text = Path.read_text
            read_count = 0
            read_count_lock = threading.Lock()

            def read_text(path: Path, *args, **kwargs):
                nonlocal read_count
                if path == description:
                    with read_count_lock:
                        read_count += 1
                    time.sleep(0.01)
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", new=read_text):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(lambda _: spec.load_description_text(), range(8)))

            self.assertEqual(results, ["静态任务描述"] * 8)
            self.assertEqual(read_count, 1)
            description.write_text("修改后的描述\n", encoding="utf-8")
            self.assertEqual(spec.load_description_text(), "静态任务描述")
            self.assertNotIn("_description_text_cache", spec.to_dict())

    def test_skill_postprocessor_index_and_module_are_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill_dir = root / "demo"
            skill_dir.mkdir()
            (skill_dir / "skill.json").write_text(
                '{"name": "demo", "commands": {}}',
                encoding="utf-8",
            )
            (skill_dir / "postprocess.py").write_text(
                "def describe_result(command, data):\n"
                "    return command\n",
                encoding="utf-8",
            )

            with patch.object(
                result_postprocessor.importlib.util,
                "spec_from_file_location",
                wraps=result_postprocessor.importlib.util.spec_from_file_location,
            ) as spec_from_file_location:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(
                        pool.map(
                            lambda _: postprocess_action_result(
                                {"action": "demo.inspect", "ok": True},
                                skills_dir=root,
                            ),
                            range(8),
                        )
                    )

            self.assertEqual([result["description"] for result in results], ["inspect"] * 8)
            self.assertEqual(spec_from_file_location.call_count, 1)


if __name__ == "__main__":
    unittest.main()
