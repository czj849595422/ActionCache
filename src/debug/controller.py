"""Runner 无关的交互式调试控制器。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable

from .providers import DebugAdapter, PromptProvider
from .recorder import DebugRecorder
from .schemas import (
    DebugOperationError,
    DebugResult,
    PromptSnapshot,
    to_jsonable,
)


class DebugController:
    """统一封装动作、RCON、观察、Prompt 和组件查询。"""

    def __init__(
        self,
        adapter: DebugAdapter,
        recorder: DebugRecorder,
        *,
        prompt_providers: Iterable[PromptProvider] = (),
    ) -> None:
        self.adapter = adapter
        self.recorder = recorder
        self._prompt_providers: dict[str, PromptProvider] = {}
        for provider in prompt_providers:
            if provider.name in self._prompt_providers:
                raise ValueError(f"duplicate prompt provider: {provider.name}")
            self._prompt_providers[provider.name] = provider
        self._last_result: DebugResult | None = None

    def execute_action(
        self,
        agent: str,
        operation: str,
        *,
        action: str | None = None,
        params: dict[str, Any] | None = None,
        action_id: str | None = None,
        reason: str | None = None,
    ) -> DebugResult:
        request = {
            "agent": agent,
            "action": action,
            "params": params or {},
            "action_id": action_id,
            "reason": reason,
        }

        def invoke() -> Any:
            raw = self.adapter.execute_action(
                agent,
                operation,
                action=action,
                params=params,
                action_id=action_id,
                reason=reason,
            )
            if hasattr(raw, "ok") and not bool(raw.ok):
                message = getattr(raw, "error", None) or "Agent action failed"
                raise DebugOperationError(
                    "action_failed",
                    str(message),
                    details=to_jsonable(raw),
                )
            return raw

        return self._invoke(
            f"action.{operation}",
            invoke,
            request=request,
            force_artifact=True,
        )

    def send_server_command(self, command: str) -> DebugResult:
        if not isinstance(command, str) or not command.strip():
            return self.error_result(
                "rcon",
                "invalid_command",
                "RCON command must not be empty",
                request={"command": command},
            )
        return self._invoke(
            "rcon",
            lambda: {
                "command": command,
                "response": self.adapter.send_server_command(command),
            },
            request={"command": command},
        )

    def observe_agent(
        self,
        agent: str,
        options: dict[str, Any] | None = None,
    ) -> DebugResult:
        normalized = dict(options or {})
        return self._invoke(
            "observe.agent",
            lambda: {
                "agent": agent,
                "options": normalized,
                "observation": self.adapter.observe_agent(agent, normalized),
            },
            request={"agent": agent, "options": normalized},
            force_artifact=True,
        )

    def observe_all(self, options: dict[str, Any] | None = None) -> DebugResult:
        normalized = dict(options or {})

        def invoke() -> dict[str, Any]:
            observations: dict[str, Any] = {}
            names = self.adapter.agent_names
            with ThreadPoolExecutor(max_workers=max(1, len(names))) as executor:
                futures = {
                    executor.submit(
                        self.adapter.observe_agent,
                        name,
                        normalized,
                    ): name
                    for name in names
                }
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        observations[name] = {
                            "ok": True,
                            "observation": future.result(),
                        }
                    except Exception as exc:  # noqa: BLE001
                        observations[name] = {
                            "ok": False,
                            "error": {
                                "code": "observation_failed",
                                "message": str(exc),
                            },
                        }
            return {
                "definition": "aggregated_agent_global_observations",
                "options": normalized,
                "agents": {
                    name: observations[name]
                    for name in names
                },
                "server": self.adapter.status().get("server"),
                "task_outcome": self.adapter.outcome(),
                "task_metrics": self.adapter.metrics(),
            }

        return self._invoke(
            "observe.all",
            invoke,
            request={"options": normalized},
            force_artifact=True,
        )

    def list_prompt_methods(self, agent: str | None = None) -> DebugResult:
        def invoke() -> dict[str, Any]:
            if agent is not None and agent not in self.adapter.agent_names:
                raise DebugOperationError("unknown_agent", f"Unknown agent: {agent}")
            methods: list[str] = []
            for name, provider in sorted(self._prompt_providers.items()):
                methods.extend(
                    f"{name}.{method}" for method in provider.list_methods(agent)
                )
            return {"agent": agent, "methods": methods}

        return self._invoke(
            "prompt.list",
            invoke,
            request={"agent": agent},
        )

    def build_prompt(self, agent: str, qualified_method: str) -> DebugResult:
        def invoke() -> PromptSnapshot:
            if agent not in self.adapter.agent_names:
                raise DebugOperationError("unknown_agent", f"Unknown agent: {agent}")
            provider_name, separator, method = qualified_method.partition(".")
            provider = self._prompt_providers.get(provider_name)
            if not separator or provider is None or method not in provider.list_methods(agent):
                raise DebugOperationError(
                    "prompt_method_unavailable",
                    f"Prompt method is not registered: {qualified_method}",
                )
            snapshot = provider.build_prompt(agent, method)
            if not isinstance(snapshot, PromptSnapshot):
                raise DebugOperationError(
                    "invalid_prompt_snapshot",
                    "Prompt provider must return PromptSnapshot",
                )
            return snapshot

        return self._invoke(
            "prompt.show",
            invoke,
            request={"agent": agent, "method": qualified_method},
            force_artifact=True,
        )

    def list_components(self) -> DebugResult:
        return self._invoke(
            "inspect.list",
            lambda: {
                "components": sorted(
                    {*self.adapter.component_providers(), "prompt_providers"}
                )
            },
        )

    def inspect_component(
        self,
        component: str,
        params: dict[str, Any] | None = None,
    ) -> DebugResult:
        normalized = dict(params or {})

        def invoke() -> Any:
            if component == "prompt_providers":
                return {
                    name: list(provider.list_methods(normalized.get("agent")))
                    for name, provider in sorted(self._prompt_providers.items())
                }
            provider = self.adapter.component_providers().get(component)
            if provider is None:
                raise DebugOperationError(
                    "component_unavailable",
                    f"Component is not registered: {component}",
                )
            return provider(normalized)

        return self._invoke(
            "inspect.show",
            invoke,
            request={"component": component, "params": normalized},
            force_artifact=True,
        )

    def status(self) -> DebugResult:
        return self._invoke("status", self.adapter.status)

    def metrics(self) -> DebugResult:
        return self._invoke("metrics", self.adapter.metrics)

    def outcome(self) -> DebugResult:
        return self._invoke("outcome", self.adapter.outcome)

    def history(self, count: int = 20) -> DebugResult:
        return self._invoke(
            "history",
            lambda: {"events": self.recorder.history(count)},
            request={"count": count},
        )

    def last_result(self) -> DebugResult | None:
        return self._last_result

    def error_result(
        self,
        operation: str,
        code: str,
        message: str,
        *,
        details: Any = None,
        request: Any = None,
    ) -> DebugResult:
        result = DebugResult.failure(
            operation,
            code,
            message,
            details=details,
        )
        return self._record(result, request=request)

    def _invoke(
        self,
        operation: str,
        callback: Callable[[], Any],
        *,
        request: Any = None,
        force_artifact: bool = False,
    ) -> DebugResult:
        try:
            result = DebugResult.success(operation, callback())
        except DebugOperationError as exc:
            result = DebugResult.failure(
                operation,
                exc.code,
                str(exc),
                details=exc.details,
            )
        except Exception as exc:  # noqa: BLE001
            result = DebugResult.failure(
                operation,
                "operation_failed",
                str(exc),
            )
        return self._record(
            result,
            request=request,
            force_artifact=force_artifact,
        )

    def _record(
        self,
        result: DebugResult,
        *,
        request: Any = None,
        force_artifact: bool = False,
    ) -> DebugResult:
        self.recorder.record_result(
            result,
            request=request,
            force_artifact=force_artifact,
        )
        self._last_result = result
        return result
