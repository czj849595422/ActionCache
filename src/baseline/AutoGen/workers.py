"""Workers for the AutoGen-style hierarchical A2A baseline."""

from __future__ import annotations

import time
from typing import Any, Callable, Sequence

from src.agents.action import SimpleInstructionError, parse_action_call
from src.baseline.COAP.adapters import build_action_unit, expand_action_name
from src.baseline.COAP.schemas import COAPProposal
from src.memory import TeamMemory, action_text
from src.runtime import LoopWorker, SingleSlotOverwriteBuffer
from src.runtime.liveness import EntityLivenessGate

from .a2a import A2AStore
from .prompts import build_commander_request, build_executor_request
from .protocol import (
    AutoGenProtocolError,
    parse_commander_response,
    parse_executor_response,
)
from .schemas import AutoGenConfig, ExecutorDecision, SubtaskReport


EventSink = Callable[..., None]


class AutoGenCommanderWorker(LoopWorker):
    """Assign short-horizon subtasks to currently available executors."""

    def __init__(
        self,
        *,
        llm_client: Any,
        memory: TeamMemory,
        a2a: A2AStore,
        config: AutoGenConfig,
        spec: Any,
        executors: Sequence[str],
        budget: Any | None = None,
        liveness_gate: EntityLivenessGate | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        super().__init__(
            name=f"autogen-commander-{config.commander_id}",
            loop_interval=config.commander_interval,
        )
        self.llm_client = llm_client
        self.memory = memory
        self.a2a = a2a
        self.config = config
        self.spec = spec
        self.executors = tuple(executors)
        self.budget = budget
        self.liveness_gate = liveness_gate
        self.event_sink = event_sink
        self.planned_count = 0
        self.assignment_count = 0
        self.last_plan_error: str | None = None
        self.planning_error_history: list[str] = []
        self.consecutive_errors = 0
        self._last_processed_a2a_version: int | None = None
        self._last_processed_public_message_count: int | None = None

    def iterate(self) -> None:
        if not self._observations_ready():
            return
        eligible = tuple(
            agent_id
            for agent_id in self.a2a.eligible_recipients()
            if self.liveness_gate is None or self.liveness_gate.is_live(agent_id)
        )
        if not eligible:
            return
        current_version = self.a2a.version
        public_message_count = self._public_message_count()
        if (
            self._last_processed_a2a_version == current_version
            and self._last_processed_public_message_count == public_message_count
            and self.planned_count > 0
        ):
            return
        self._plan_once(
            eligible=eligible,
            source_version=current_version,
            source_public_message_count=public_message_count,
        )

    def _plan_once(
        self,
        *,
        eligible: Sequence[str],
        source_version: int,
        source_public_message_count: int,
    ) -> None:
        if self.budget is not None and not self.budget.try_acquire(
            self.config.commander_id
        ):
            return
        try:
            request = build_commander_request(
                memory=self.memory,
                a2a=self.a2a,
                spec=self.spec,
                config=self.config,
                eligible_recipients=eligible,
                last_error=self.last_plan_error,
                error_history=tuple(self.planning_error_history),
            )
            response = self.llm_client.complete(request)
            parsed = parse_commander_response(
                response.content,
                eligible_recipients=eligible,
                default_max_rounds=self.config.default_subtask_max_rounds,
            )
            self.memory.append_history_event(
                agent_id=self.config.commander_id,
                source="self",
                event_type="planning",
                content={
                    "standard_plan": "分配任务",
                    "selected_action": "、".join(
                        assignment.subtask_id for assignment in parsed.assignments
                    ) or "无",
                    "reasoning": parsed.reasoning or None,
                    "communication": None,
                    "result_description": "",
                    "result_status": None,
                    "result_data": None,
                },
                metadata={"method": "autogen", "role": "commander"},
            )
            for assignment in parsed.assignments:
                self.a2a.assign(assignment)
                self.assignment_count += 1
                if self.event_sink is not None:
                    self.event_sink(
                        "autogen.assignment.created",
                        recipient=assignment.recipient,
                        subtask_id=assignment.subtask_id,
                    )
            self.planned_count += 1
            self.consecutive_errors = 0
            self.last_plan_error = None
            self.planning_error_history.clear()
            self._last_processed_a2a_version = self.a2a.version
            self._last_processed_public_message_count = source_public_message_count
        except Exception as exc:  # noqa: BLE001
            self.consecutive_errors += 1
            error = str(exc)
            self.last_plan_error = error
            self.planning_error_history.append(error)
            if self.event_sink is not None:
                self.event_sink(
                    "autogen.commander.failed",
                    error=error,
                    consecutive_errors=self.consecutive_errors,
                )
            if self.consecutive_errors >= self.config.max_consecutive_planning_errors:
                self.consecutive_errors = 0
                self.last_plan_error = None
                self.planning_error_history.clear()

    def _observations_ready(self) -> bool:
        observations = self.memory.observations()
        live_executors = tuple(
            agent_id
            for agent_id in self.executors
            if self.liveness_gate is None or self.liveness_gate.is_live(agent_id)
        )
        return bool(live_executors) and all(
            agent_id in observations for agent_id in live_executors
        )

    def _public_message_count(self) -> int:
        entries = self.memory.prompt_history(
            self.config.commander_id,
            team_limit=10_000,
            system_limit=10_000,
        )
        return sum(
            1
            for entry in entries
            if entry.get("event_type") in {"communication", "system"}
            or entry.get("content", {}).get("result_status") is not None
            or entry.get("content", {}).get("communication")
        )


class AutoGenSubtaskPlanningWorker(LoopWorker):
    """Executor-side subtask completion check and next-action planner."""

    def __init__(
        self,
        *,
        agent: Any,
        teammates: Sequence[str],
        llm_client: Any,
        buffer: SingleSlotOverwriteBuffer,
        memory: TeamMemory,
        a2a: A2AStore,
        config: AutoGenConfig,
        registry: Any,
        spec: Any,
        instructions: Any | None,
        acting_worker: Any,
        budget: Any | None = None,
        event_sink: EventSink | None = None,
        path_label: str = "autogen",
        liveness_gate: EntityLivenessGate | None = None,
        trade_recipe_history: Any | None = None,
    ) -> None:
        agent_id = str(agent.username)
        super().__init__(
            name=f"autogen-planning-{agent_id}",
            loop_interval=0.1,
        )
        self.agent = agent
        self.agent_id = agent_id
        self.teammates = tuple(teammates)
        self.llm_client = llm_client
        self.buffer = buffer
        self.memory = memory
        self.a2a = a2a
        self.config = config
        self.registry = registry
        self.spec = spec
        self.instructions = instructions
        self.acting_worker = acting_worker
        self.budget = budget
        self.event_sink = event_sink
        self.path_label = path_label
        self.liveness_gate = liveness_gate
        self.trade_recipe_history = trade_recipe_history
        self.planned_count = 0
        self.last_plan_error: str | None = None
        self.consecutive_errors = 0
        self.report_count = 0

    def iterate(self) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        if self.memory.get_observation(self.agent_id) is None:
            return
        if self.acting_worker.is_busy:
            return
        if self.buffer.has_new(self.acting_worker.seen_version):
            return
        assignment = self.a2a.active_assignment(self.agent_id)
        if assignment is None:
            return
        state = self.a2a.state(self.agent_id)
        if state.planning_round >= assignment.max_planning_rounds:
            self._send_report(
                SubtaskReport(
                    sender=self.agent_id,
                    subtask_id=assignment.subtask_id,
                    status="max_rounds",
                    report=(
                        "Maximum planning rounds reached before satisfying "
                        "the subtask success criteria."
                    ),
                    reason="max_planning_rounds",
                    metadata={"max_planning_rounds": assignment.max_planning_rounds},
                )
            )
            return
        self._plan_once(assignment)

    def _plan_once(self, assignment) -> None:
        if self.budget is not None and not self.budget.try_acquire(self.agent_id):
            return
        try:
            request = build_executor_request(
                memory=self.memory,
                a2a=self.a2a,
                spec=self.spec,
                registry=self.registry,
                instructions=self.instructions,
                config=self.config,
                agent_id=self.agent_id,
                teammates=self.teammates,
                assignment=assignment,
                last_error=self.last_plan_error,
                trade_recipe_history=self.trade_recipe_history,
            )
            started = time.time()
            response = self.llm_client.complete(request)
            finished = time.time()
            if self._stop_event.is_set():
                return
            if self.liveness_gate is not None and not self.liveness_gate.is_live(
                self.agent_id
            ):
                return
            decision = parse_executor_response(response.content)
            round_number = self.a2a.increment_planning_round(self.agent_id)
            self.planned_count += 1
            self.consecutive_errors = 0
            self.last_plan_error = None
            if decision.status == "continue":
                proposal = self._proposal_from_decision(
                    decision,
                    request_id=getattr(response, "request_id", None),
                    started=started,
                    finished=finished,
                )
                self._publish(proposal)
                self.a2a.mark_working(self.agent_id)
                return
            self.memory.append_history_event(
                agent_id=self.agent_id,
                source="self",
                event_type="communication",
                content={
                    "message": (
                        f"子任务 {assignment.subtask_id} 状态：{decision.status}"
                    ),
                    "status": decision.status,
                },
                metadata={
                    "method": "autogen",
                    "subtask_id": assignment.subtask_id,
                },
            )
            self._send_report(
                SubtaskReport(
                    sender=self.agent_id,
                    subtask_id=assignment.subtask_id,
                    status=decision.status,
                    report=decision.report,
                    reason=decision.reason,
                    metadata={"planning_round": round_number},
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.consecutive_errors += 1
            self.last_plan_error = str(exc)
            if self.event_sink is not None:
                self.event_sink(
                    "autogen.executor.failed",
                    agent_id=self.agent_id,
                    error=str(exc),
                    consecutive_errors=self.consecutive_errors,
                )
            if self.consecutive_errors >= self.config.max_consecutive_planning_errors:
                assignment = self.a2a.active_assignment(self.agent_id)
                if assignment is not None:
                    self._send_report(
                        SubtaskReport(
                            sender=self.agent_id,
                            subtask_id=assignment.subtask_id,
                            status="blocked",
                            report=(
                                "Executor planning failed repeatedly: "
                                f"{self.last_plan_error}"
                            ),
                            reason="planning_error_threshold",
                            metadata={
                                "consecutive_errors": self.consecutive_errors,
                            },
                        )
                    )
                self.consecutive_errors = 0

    def _proposal_from_decision(
        self,
        decision: ExecutorDecision,
        *,
        request_id: str | None,
        started: float,
        finished: float,
    ) -> COAPProposal:
        try:
            name, params, positional = parse_action_call(decision.action_text)
        except SimpleInstructionError as exc:
            raise AutoGenProtocolError(str(exc)) from exc
        try:
            canonical, filled, mode = expand_action_name(
                name,
                params,
                instructions=self.instructions,
                registry=self.registry,
                enable_chat_actions=self.config.enable_chat_actions,
                positional=positional,
            )
        except ValueError as exc:
            raise AutoGenProtocolError(str(exc)) from exc
        unit = build_action_unit(
            agent_id=self.agent_id,
            action=canonical,
            params=filled,
            mode=mode,
        )
        return COAPProposal(
            action_unit=unit,
            interrupt=False,
            communication="",
            path=self.path_label,
            request_id=request_id,
            raw_text=decision.raw_text,
            reasoning=decision.reason,
            input_params=dict(params),
            input_positional=list(positional),
            planning_started_at=started,
            planning_finished_at=finished,
        )

    def _publish(self, proposal: COAPProposal) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        entry = self.memory.append_history_event(
            agent_id=self.agent_id,
            source="self",
            event_type="planning",
            content={
                "standard_plan": action_text(
                    proposal.action_unit.action,
                    proposal.input_params,
                    instructions=self.instructions,
                    positional=proposal.input_positional,
                ),
                "selected_action": action_text(
                    proposal.action_unit.action,
                    proposal.input_params,
                    instructions=self.instructions,
                    positional=proposal.input_positional,
                ),
                "reasoning": proposal.reasoning or None,
                "communication": None,
                "result_description": "",
                "result_status": None,
                "result_data": None,
            },
            metadata={
                "plan_id": proposal.plan_id,
                "request_id": proposal.request_id,
                "path": self.path_label,
                "mode": proposal.action_unit.mode,
                "method": "autogen",
            },
        )
        proposal.memory_entry_id = entry.entry_id
        self.buffer.put(proposal)
        if self.event_sink is not None:
            self.event_sink(
                "autogen.proposal.created",
                agent_id=self.agent_id,
                plan_id=proposal.plan_id,
                request_id=proposal.request_id,
                action=proposal.action_unit.action,
            )

    def _send_report(self, report: SubtaskReport) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        self.a2a.submit_report(report)
        self.report_count += 1
        if self.event_sink is not None:
            self.event_sink(
                "autogen.subtask.reported",
                agent_id=report.sender,
                subtask_id=report.subtask_id,
                status=report.status,
            )


__all__ = ["AutoGenCommanderWorker", "AutoGenSubtaskPlanningWorker"]
