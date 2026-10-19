"""COAP demo CLI cache boot 参数测试（不启动环境）。"""

from __future__ import annotations

from contextlib import redirect_stderr
import io
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from script import demo_coap  # noqa: E402
from src.baseline.COAP import CacheBootMode  # noqa: E402
from src.recording import RecordingConfig  # noqa: E402


class COAPDemoCliTests(unittest.TestCase):
    def parse(self, *args: str):
        return demo_coap.build_parser().parse_args(["--task", "dummy", *args])

    def test_default_cache_boot_mode_is_llm_init(self) -> None:
        parser = demo_coap.build_parser()
        config = demo_coap._build_coap_config(self.parse(), parser)
        self.assertEqual(config.cache_boot.mode, CacheBootMode.LLM_INIT)
        self.assertEqual(config.cache_boot.resolved_mode, CacheBootMode.LLM_INIT)
        self.assertTrue(config.timed_planning_enabled)
        self.assertTrue(config.standard_verification_enabled)

    def test_disable_timed_planning_only(self) -> None:
        parser = demo_coap.build_parser()
        config = demo_coap._build_coap_config(
            self.parse("--disable-timed-planning"),
            parser,
        )
        self.assertFalse(config.timed_planning_enabled)
        self.assertTrue(config.standard_verification_enabled)

    def test_disable_standard_verification_also_disables_timed_planning(self) -> None:
        parser = demo_coap.build_parser()
        config = demo_coap._build_coap_config(
            self.parse("--disable-standard-verification"),
            parser,
        )
        self.assertFalse(config.standard_verification_enabled)
        self.assertFalse(config.timed_planning_enabled)

    def test_explicit_cache_boot_modes(self) -> None:
        parser = demo_coap.build_parser()
        for mode in (
            CacheBootMode.LLM_INIT,
            CacheBootMode.REUSE_PREVIOUS,
            CacheBootMode.EMPTY,
        ):
            with self.subTest(mode=mode.value):
                config = demo_coap._build_coap_config(
                    self.parse("--cache-boot-mode", mode.value),
                    parser,
                )
                self.assertEqual(config.cache_boot.mode, mode)
                self.assertEqual(config.cache_boot.resolved_mode, mode)

    def test_cache_source_is_rejected(self) -> None:
        parser = demo_coap.build_parser()
        args = self.parse("--cache-source", "old.json")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                demo_coap._build_cache_boot(args, parser)
        self.assertEqual(raised.exception.code, 2)

    def test_help_lists_only_user_cache_modes(self) -> None:
        help_text = demo_coap.build_parser().format_help()
        self.assertIn("llm_init", help_text)
        self.assertIn("reuse_previous", help_text)
        self.assertIn("empty", help_text)
        self.assertNotIn("reuse_old", help_text)

    def test_ablation_suffixes_are_stable_and_accumulate(self) -> None:
        parser = demo_coap.build_parser()
        args = self.parse(
            "--disable-standard-verification",
            "--disable-timed-planning",
            "--disable-cache-reasoning-optimization",
            "--disable-score-update",
            "--cache-boot-mode",
            "reuse_previous",
        )
        config = demo_coap._build_coap_config(args, parser)
        self.assertEqual(
            demo_coap._coap_ablation_suffix(args, config),
            "_woSP_woPP_woRO_woSU_reuse",
        )

    def test_cache_and_standard_ablations_are_rejected(self) -> None:
        parser = demo_coap.build_parser()
        with self.assertRaises(ValueError):
            demo_coap._build_coap_config(
                self.parse(
                    "--disable-cache-planning",
                    "--disable-standard-verification",
                ),
                parser,
            )

    def test_disabling_standard_verification_only_marks_standard_ablation(self) -> None:
        parser = demo_coap.build_parser()
        args = self.parse("--disable-standard-verification")
        config = demo_coap._build_coap_config(args, parser)
        self.assertEqual(demo_coap._coap_ablation_suffix(args, config), "_woSP")

    def test_empty_cache_boot_uses_no_init_suffix(self) -> None:
        parser = demo_coap.build_parser()
        args = self.parse("--cache-boot-mode", "empty")
        config = demo_coap._build_coap_config(args, parser)
        self.assertEqual(demo_coap._coap_ablation_suffix(args, config), "_woInit")

    def test_suffix_is_added_to_recording_root_name(self) -> None:
        parser = demo_coap.build_parser()
        args = self.parse("--disable-timed-planning")
        config = demo_coap._build_coap_config(args, parser)
        recording_config, suffix = demo_coap._with_coap_ablation_suffix(
            RecordingConfig(root_dir=Path("main_result")),
            args,
            config,
        )
        self.assertEqual(suffix, "_woPP")
        self.assertEqual(recording_config.root_dir, Path("main_result_woPP"))


if __name__ == "__main__":
    unittest.main()
