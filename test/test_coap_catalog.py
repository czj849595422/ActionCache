#!/usr/bin/env python3
"""COAP static action catalog cache tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.action import SkillRegistry  # noqa: E402
from src.baseline.COAP import adapters  # noqa: E402


class CoapCatalogCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        with adapters._CATALOG_CACHE_LOCK:
            adapters._CATALOG_CACHE.clear()

    def tearDown(self) -> None:
        with adapters._CATALOG_CACHE_LOCK:
            adapters._CATALOG_CACHE.clear()

    def test_concurrent_requests_share_one_catalog_render(self) -> None:
        registry = SkillRegistry()
        original = adapters.build_chat_filtered_registry
        calls = 0

        def build_filtered(value: SkillRegistry) -> SkillRegistry:
            nonlocal calls
            calls += 1
            return original(value)

        with patch.object(
            adapters,
            "build_chat_filtered_registry",
            side_effect=build_filtered,
        ):
            with ThreadPoolExecutor(max_workers=8) as pool:
                catalogs = list(
                    pool.map(
                        lambda _: adapters.render_catalog(
                            registry,
                            None,
                            "zh",
                        ),
                        range(8),
                    )
                )

        self.assertEqual(calls, 1)
        self.assertTrue(catalogs[0].strip())
        self.assertTrue(all(catalog == catalogs[0] for catalog in catalogs))

    def test_catalog_cache_key_changes_with_allowlist(self) -> None:
        registry = SkillRegistry()
        actions = sorted(
            f"{skill}.{command}"
            for skill, manifest in registry.manifests.items()
            for command in manifest.commands
            if not f"{skill}.{command}".startswith("chat.")
        )
        self.assertGreaterEqual(len(actions), 2)

        registry.allowed_actions = frozenset({actions[0]})
        first = adapters.render_catalog(registry, None, "zh")
        registry.allowed_actions = frozenset({actions[1]})
        second = adapters.render_catalog(registry, None, "zh")

        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
