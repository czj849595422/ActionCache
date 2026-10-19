"""COAP 规划输出协议编解码：ParsedActionResponse/Head → COAPProposal。"""

from __future__ import annotations

from typing import Any, Literal, Sequence

from src.agents.action import (
    SimpleInstructionError,
    SimpleInstructionRegistry,
    SkillRegistry,
    parse_action_call,
)

from .adapters import build_action_unit, expand_action_name
from .config import CacheActionCandidatePolicy, DEFAULT_ACTION_READY_KEYWORD
from .protocol import (
    ActionHeadResult,
    ParsedActionResponse,
    parse_action_head,
    parse_action_response,
)
from .schemas import COAPProposal


ActionOperation = Literal[
    "timed_standard_action",
    "standard_action",
    "cache_action",
    "direct_action",
]


class COAPCodecError(ValueError):
    pass


def should_publish_to_buffer(operation: ActionOperation, interrupt: bool) -> bool:
    """返回规划结果是否应进入动作缓冲。

    周期性普通规划只有在显式要求中断当前动作时才会进入缓冲，其余
    情况只做记录，不抢占后续执行权。
    """

    return operation != "timed_standard_action" or interrupt


class COAPDecisionCodec:
    """把 ARC 两段式协议输出转换为可执行的 COAPProposal。"""

    def __init__(
        self,
        registry: SkillRegistry,
        instructions: SimpleInstructionRegistry | None = None,
        *,
        enable_chat_actions: bool = False,
        action_ready_keyword: str = DEFAULT_ACTION_READY_KEYWORD,
        candidate_policy: CacheActionCandidatePolicy = (
            CacheActionCandidatePolicy.ADVISORY
        ),
    ) -> None:
        self.registry = registry
        self.instructions = instructions
        self.enable_chat_actions = enable_chat_actions
        self.action_ready_keyword = action_ready_keyword
        self.candidate_policy = CacheActionCandidatePolicy(candidate_policy)

    def parse_complete(
        self,
        text: str,
        *,
        agent_id: str,
        operation: ActionOperation,
        cache_candidates: Sequence[Any] = (),
    ) -> COAPProposal:
        parsed = parse_action_response(
            text,
            operation=operation,
        )
        return self._to_proposal(
            parsed,
            agent_id=agent_id,
            operation=operation,
            communication=parsed.communication,
            cache_candidates=cache_candidates,
        )

    def parse_streamed_action(
        self,
        segment: str,
        *,
        agent_id: str,
        operation: ActionOperation,
        cache_candidates: Sequence[Any] = (),
    ) -> COAPProposal:
        """解析流式 KEYWORD_REACHED 的动作段（通信稍后补充）。"""
        head = parse_action_head(
            segment,
            operation=operation,
        )
        return self._to_proposal(
            head,
            agent_id=agent_id,
            operation=operation,
            communication="",
            cache_candidates=cache_candidates,
        )

    def _to_proposal(
        self,
        head: ActionHeadResult | ParsedActionResponse,
        *,
        agent_id: str,
        operation: ActionOperation,
        communication: str,
        cache_candidates: Sequence[Any] = (),
    ) -> COAPProposal:
        try:
            name, params, positional = parse_action_call(head.action_text)
        except SimpleInstructionError as exc:
            raise COAPCodecError(str(exc)) from exc
        try:
            canonical, filled, mode = expand_action_name(
                name,
                params,
                instructions=self.instructions,
                registry=self.registry,
                enable_chat_actions=self.enable_chat_actions,
                positional=positional,
            )
        except ValueError as exc:
            raise COAPCodecError(str(exc)) from exc
        unit = build_action_unit(
            agent_id=agent_id,
            action=canonical,
            params=filled,
            mode=mode,
        )
        return COAPProposal(
            action_unit=unit,
            interrupt=head.interrupt,
            communication=communication,
            path=(
                "direct"
                if operation == "direct_action"
                else "cache"
                if operation == "cache_action"
                else "standard"
            ),
            publish_to_buffer=should_publish_to_buffer(operation, head.interrupt),
            request_id=getattr(head, "request_id", None),
            raw_text=head.raw_text if hasattr(head, "raw_text") else "",
            reasoning=head.reasoning,
            input_params=dict(params),
            input_positional=list(positional),
        )


__all__ = [
    "COAPCodecError",
    "COAPDecisionCodec",
    "should_publish_to_buffer",
]
