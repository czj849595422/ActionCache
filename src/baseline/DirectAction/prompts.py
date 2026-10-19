"""Prompt construction for the DirectAction baseline."""

from __future__ import annotations

from pathlib import Path

from src.baseline.BasicAsync.prompts import BasicAsyncPromptBuilder


DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "prompts"


class DirectActionPromptBuilder(BasicAsyncPromptBuilder):
    """Build direct-action prompts from DirectAction-owned templates."""

    default_template_dir = DEFAULT_TEMPLATE_DIR


__all__ = ["DEFAULT_TEMPLATE_DIR", "DirectActionPromptBuilder"]
