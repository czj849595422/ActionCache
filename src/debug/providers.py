"""Debug Controller 的同进程 adapter 与 provider 协议。"""

from __future__ import annotations

from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from .schemas import PromptSnapshot


@runtime_checkable
class PromptProvider(Protocol):
    """Runner 注册其实际使用的 Prompt builder。"""

    name: str

    def list_methods(self, agent: str | None = None) -> Sequence[str]:
        ...

    def build_prompt(self, agent: str, method: str) -> PromptSnapshot:
        ...


ComponentInspector = Callable[[dict[str, Any]], Any]


@runtime_checkable
class DebugAdapter(Protocol):
    """不同 Runner 接入 DebugController 的最小同进程接口。"""

    @property
    def agent_names(self) -> list[str]:
        ...

    def execute_action(
        self,
        agent: str,
        operation: str,
        *,
        action: str | None = None,
        params: dict[str, Any] | None = None,
        action_id: str | None = None,
        reason: str | None = None,
    ) -> Any:
        ...

    def send_server_command(self, command: str) -> Any:
        ...

    def observe_agent(self, agent: str, options: dict[str, Any]) -> Any:
        ...

    def status(self) -> dict[str, Any]:
        ...

    def metrics(self) -> dict[str, Any]:
        ...

    def outcome(self) -> dict[str, Any] | None:
        ...

    def component_providers(self) -> dict[str, ComponentInspector]:
        ...
