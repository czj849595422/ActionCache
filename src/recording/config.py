"""Configuration for project-wide run evidence recording."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
import warnings


DEFAULT_SENSITIVE_KEYS = (
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)


@dataclass(frozen=True)
class RecordingConfig:
    """Filesystem and redaction policy for recording artifacts."""

    root_dir: Path = Path("data")
    schema_version: str = "1"
    close_timeout: float = 5.0
    sensitive_keys: tuple[str, ...] = DEFAULT_SENSITIVE_KEYS
    redact_values: tuple[str, ...] = field(default_factory=tuple, repr=False)

    def __post_init__(self) -> None:
        if not self.schema_version.strip():
            raise ValueError("schema_version must not be empty")
        if self.close_timeout <= 0:
            raise ValueError("close_timeout must be positive")
        if not self.sensitive_keys:
            raise ValueError("sensitive_keys must not be empty")
        object.__setattr__(self, "root_dir", Path(self.root_dir))


def resolve_recording_config(
    output: Mapping[str, Any] | None = None,
    *,
    root_dir: str | Path | None = None,
    base_dir: str | Path | None = None,
) -> RecordingConfig:
    """Resolve explicit root, legacy task output, then the project default."""
    legacy = dict(output or {})
    if "dir" in legacy:
        warnings.warn(
            "TaskSpec.output.dir is deprecated; use the runner recording config",
            DeprecationWarning,
            stacklevel=2,
        )
    if "save_llm_calls" in legacy:
        warnings.warn(
            "TaskSpec.output.save_llm_calls is deprecated and ignored; "
            "LLM calls are always recorded",
            DeprecationWarning,
            stacklevel=2,
        )
    selected = root_dir if root_dir is not None else legacy.get("dir", Path("data"))
    path = Path(selected)
    if not path.is_absolute() and base_dir is not None:
        path = Path(base_dir) / path
    return RecordingConfig(root_dir=path)
