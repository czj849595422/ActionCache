"""LLM 配置：环境变量读取，缺省回退到 MCAgent 项目 .env。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schemas import LLMConfigurationError

MCAgent_ENV_PATH = Path(r"D:\MC\MCAgent\.env")
_DEFAULT_THINKING_DISABLED_BODY = '{"thinking":{"type":"disabled"}}'


def _read_env_file(path: str | Path | None) -> dict[str, str]:
    """读取 .env 文件为字典；文件不存在或为空时返回 {}。"""
    if path is None:
        return {}
    env_path = Path(path)
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def _as_bool(value: str, *, default: bool = False) -> bool:
    if not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_base_url(url: str) -> str:
    normalized = url.strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/chat/completions"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)].rstrip("/")
    return normalized


def _parse_json_env(value: str, name: str, *, default: Any) -> Any:
    if not value.strip():
        return default
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LLMConfigurationError(f"Invalid {name} JSON") from exc
    return parsed


@dataclass
class LLMConfig:
    """OpenAI 兼容 chat completions 配置。"""

    api_url: str
    api_key: str = field(repr=False)
    model: str
    timeout: float = 60.0
    max_retries: int = 2
    temperature: float | None = None
    max_tokens: int | None = None
    thinking_enabled: bool = False
    thinking_disabled_body: dict[str, Any] = field(
        default_factory=lambda: {"thinking": {"type": "disabled"}}
    )
    reject_reasoning_when_disabled: bool = True
    disable_thinking_param: str = ""
    stream_include_usage: bool = False
    retain_raw_chunks: bool = True
    maximum_buffer_characters: int = 1_000_000
    extra_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.api_url = _normalize_base_url(self.api_url)
        if not self.api_url:
            raise LLMConfigurationError("api_url must not be empty")
        if not self.api_key:
            raise LLMConfigurationError("api_key must not be empty")
        if not self.model:
            raise LLMConfigurationError("model must not be empty")
        if self.timeout <= 0:
            raise LLMConfigurationError("timeout must be greater than zero")
        if self.max_retries < 0:
            raise LLMConfigurationError("max_retries must not be negative")
        if self.maximum_buffer_characters <= 0:
            raise LLMConfigurationError(
                "maximum_buffer_characters must be greater than zero"
            )
        if not isinstance(self.thinking_disabled_body, dict):
            raise LLMConfigurationError("thinking_disabled_body must be a JSON object")
        if not isinstance(self.reject_reasoning_when_disabled, bool):
            raise LLMConfigurationError(
                "reject_reasoning_when_disabled must be a boolean"
            )

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> "LLMConfig":
        """从环境变量构建配置。

        优先级：系统环境变量 > 显式 env_path 文件 > 缺省回退 MCAgent .env。
        """
        file_values = _read_env_file(env_path) if env_path is not None else {}
        if env_path is None and not cls._base_vars_present():
            file_values = _read_env_file(MCAgent_ENV_PATH)

        def value(name: str, default: str = "") -> str:
            key = f"LLM_{name}"
            return os.getenv(key, file_values.get(key, default)).strip()

        api_url = value("API_URL") or value("BASE_URL")
        api_key = value("API_KEY")
        model = value("MODEL")
        missing = [
            f"LLM_{name}"
            for name, current in (
                ("API_URL", api_url),
                ("API_KEY", api_key),
                ("MODEL", model),
            )
            if not current
        ]
        if missing:
            raise LLMConfigurationError(
                "Missing LLM environment variables: " + ", ".join(missing)
            )

        timeout_text = value("TIMEOUT", "60")
        max_retries_text = value("MAX_RETRIES", "2")
        temperature_text = value("TEMPERATURE")
        max_tokens_text = value("MAX_TOKENS")
        return cls(
            api_url=api_url,
            api_key=api_key,
            model=model,
            timeout=float(timeout_text),
            max_retries=int(max_retries_text),
            temperature=float(temperature_text) if temperature_text else None,
            max_tokens=int(max_tokens_text) if max_tokens_text else None,
            thinking_enabled=_as_bool(value("THINKING_ENABLED"), default=False),
            thinking_disabled_body=_parse_json_env(
                value("THINKING_DISABLED_BODY", _DEFAULT_THINKING_DISABLED_BODY),
                "LLM_THINKING_DISABLED_BODY",
                default={"thinking": {"type": "disabled"}},
            ),
            reject_reasoning_when_disabled=_as_bool(
                value("REJECT_REASONING_WHEN_DISABLED"), default=True
            ),
            disable_thinking_param=value("DISABLE_THINKING_PARAM"),
            stream_include_usage=_as_bool(
                value("STREAM_INCLUDE_USAGE"), default=False
            ),
            retain_raw_chunks=_as_bool(value("RETAIN_RAW_CHUNKS"), default=True),
            maximum_buffer_characters=int(
                value("MAXIMUM_BUFFER_CHARACTERS", "1000000")
            ),
            extra_headers=_parse_json_env(
                value("EXTRA_HEADERS"), "LLM_EXTRA_HEADERS", default={}
            ),
            extra_body=_parse_json_env(
                value("EXTRA_BODY"), "LLM_EXTRA_BODY", default={}
            ),
        )

    @classmethod
    def _base_vars_present(cls) -> bool:
        has_url = bool(
            os.getenv("LLM_API_URL", "").strip()
            or os.getenv("LLM_BASE_URL", "").strip()
        )
        return (
            has_url
            and bool(os.getenv("LLM_API_KEY", "").strip())
            and bool(os.getenv("LLM_MODEL", "").strip())
        )

    def safe_dict(self) -> dict[str, Any]:
        """脱敏后的配置视图（api_key 替换为 ***）。"""
        return {
            "api_url": self.api_url,
            "api_key": "***",
            "model": self.model,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "thinking_enabled": self.thinking_enabled,
            "thinking_disabled_body": dict(self.thinking_disabled_body),
            "reject_reasoning_when_disabled": self.reject_reasoning_when_disabled,
            "disable_thinking_param": self.disable_thinking_param,
            "stream_include_usage": self.stream_include_usage,
            "retain_raw_chunks": self.retain_raw_chunks,
            "maximum_buffer_characters": self.maximum_buffer_characters,
            "extra_headers": dict(self.extra_headers),
            "extra_body": dict(self.extra_body),
        }
