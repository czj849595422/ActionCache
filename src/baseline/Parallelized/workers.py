"""Observation, planning, and acting workers for one PPA team."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from src.agents.action import ActionMode, ActionStatus
from src.agents.action import SimpleInstructionRegistry
from src.agents.action.result_postprocessor import postprocess_action_result
from src.memory import TeamMemory, action_text
from src.perception.attack_detection import (
    is_successful_execution,
    tracker_for,
    with_detection_context,
)
from src.runtime import LoopWorker, SingleSlotOverwriteBuffer
from src.runtime.liveness import EntityLivenessGate

from .budget import RunBudget
from .prompts import PPAPromptBuilder
from .schemas import PPAConfig, PPAProposal
from .text_protocol import DecisionCodec, render_action_call
from src.recording.table import ActionExecutionTable
from src.tasks.trade import publish_trade_purchase_broadcast


RuleFactory = Callable[[], Any]
EventSink = Callable[..., None]


class ObservationWorker(LoopWorker):
    """Poll, filter, and render one agent observation into team memory."""

    def __init__(
        self,
        *,
        agent_id: str,
        perception: Any,
        verbalizer: Any,
        memory: TeamMemory,
        config: PPAConfig,
        rule_factory: RuleFactory | None = None,
        liveness_gate: EntityLivenessGate | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        super().__init__(
            name=f"ppa-observation-{agent_id}",
            loop_interval=config.observation_interval,
        )
        self.agent_id = agent_id
        self.perception = perception
        self.verbalizer = verbalizer
        self.memory = memory
        self.config = config
        self.rule_factory = rule_factory
        self.liveness_gate = liveness_gate
        self.event_sink = event_sink
        self.first_observation = threading.Event()
        self.consecutive_errors = 0
        self.last_transient_error: str | None = None
        self.attack_detection = tracker_for(getattr(perception, "agent", None))

    def iterate(self) -> None:
        try:
            if self.liveness_gate is not None and not self.liveness_gate.is_live(
                self.agent_id
            ):
                return
            if self.rule_factory is not None:
                self.perception.rule = self.rule_factory()
            observation = self.perception.perceive()
            if (
                self.liveness_gate is not None
                and not self.liveness_gate.update_from_observation(
                    self.agent_id,
                    observation,
                )
            ):
                return
            detection = self.attack_detection.observe(observation)
            text = self.verbalizer.render(
                with_detection_context(observation, detection)
            )
            self.memory.update_observation(
                self.agent_id,
                text or "本轮没有可报告的观测。",
                metadata={"observed_at": time.time(), "format": "text"},
            )
            self.consecutive_errors = 0
            self.last_transient_error = None
            first = not self.first_observation.is_set()
            self.first_observation.set()
            if self.event_sink is not None:
                self.event_sink(
                    "observation.updated",
                    agent_id=self.agent_id,
                    first=first,
                    text_length=len(text or ""),
                )
        except Exception as exc:  # noqa: BLE001
            self.consecutive_errors += 1
            self.last_transient_error = str(exc)
            if self.event_sink is not None:
                self.event_sink(
                    "observation.failed",
                    agent_id=self.agent_id,
                    consecutive_errors=self.consecutive_errors,
                    error=str(exc),
                )
            if self.consecutive_errors >= self.config.max_consecutive_errors:
                raise RuntimeError(
                    f"observation failed {self.consecutive_errors} times: {exc}"
                ) from exc


class PlanningWorker(LoopWorker):
    """Build prompts, call the LLM, and publish validated proposals."""

    def __init__(
        self,
        *,
        agent: Any,
        teammates: Sequence[str],
        llm_client: Any,
        prompt_builder: PPAPromptBuilder,
        codec: DecisionCodec,
        buffer: SingleSlotOverwriteBuffer,
        interrupt_event: threading.Event,
        memory: TeamMemory,
        config: PPAConfig,
        budget: RunBudget | None = None,
        liveness_gate: EntityLivenessGate | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        agent_id = str(agent.username)
        super().__init__(
            name=f"ppa-planning-{agent_id}",
            loop_interval=config.planning_interval,
        )
        self.agent = agent
        self.agent_id = agent_id
        self.teammates = tuple(teammates)
        self.llm_client = llm_client
        self.prompt_builder = prompt_builder
        self.codec = codec
        self.buffer = buffer
        self.interrupt_event = interrupt_event
        self.memory = memory
        self.config = config
        self.budget = budget
        self.liveness_gate = liveness_gate
        self.event_sink = event_sink
        self.registry = agent.action_planner.registry
        self.instructions = getattr(prompt_builder, "instructions", None)
        self.consecutive_errors = 0
        self.last_prompt_error: str | None = None
        self.planning_error_history: list[str] = []
        self.planned_count = 0
        self._last_passive_result_id: str | None = None
        self.attack_detection = tracker_for(agent)

    def iterate(self) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        if self.memory.get_observation(self.agent_id) is None:
            return
        self._maybe_passive_communicate()
        if self.budget is not None and not self.budget.try_acquire(self.agent_id):
            return
        try:
            request = self.prompt_builder.build(
                agent_id=self.agent_id,
                teammates=self.teammates,
                registry=self.registry,
                last_error=self.last_prompt_error,
                error_history=tuple(self.planning_error_history),
            )
            planning_started_at = time.time()
            response = self.llm_client.complete(request)
            planning_finished_at = time.time()
            if self._stop_event.is_set():
                return
            if self.liveness_gate is not None and not self.liveness_gate.is_live(
                self.agent_id
            ):
                return
            proposal = self.codec.parse(
                response.content,
                agent_id=self.agent_id,
                registry=self.registry,
            )
            proposal.request_id = getattr(response, "request_id", None)
            proposal.planning_started_at = planning_started_at
            proposal.planning_finished_at = planning_finished_at
            if self._stop_event.is_set():
                return
            if self.liveness_gate is not None and not self.liveness_gate.is_live(
                self.agent_id
            ):
                return
            proposal.action_unit.metadata["plan_id"] = proposal.plan_id
            previous = self.buffer.peek()
            if previous is not None and isinstance(previous[1], PPAProposal):
                self._mark_superseded(previous[1])
            entry = self.memory.append_history_event(
                agent_id=self.agent_id,
                source="self",
                event_type="planning",
                content={
                    "standard_plan": action_text(
                        proposal.action_unit.action,
                        proposal.input_params,
                        instructions=(
                            self.instructions
                            if getattr(
                                getattr(self.prompt_builder, "spec", None),
                                "category",
                                "",
                            ) == "trade"
                            else None
                        ),
                        positional=proposal.input_positional,
                    ),
                    "selected_action": action_text(
                        proposal.action_unit.action,
                        proposal.input_params,
                        instructions=(
                            self.instructions
                            if getattr(
                                getattr(self.prompt_builder, "spec", None),
                                "category",
                                "",
                            ) == "trade"
                            else None
                        ),
                        positional=proposal.input_positional,
                    ),
                    "reasoning": proposal.reason or None,
                    "communication": proposal.passive_message or None,
                    "result_description": "",
                    "result_status": None,
                    "result_data": None,
                },
                metadata={
                    "status": "planned",
                    "plan_id": proposal.plan_id,
                    "request_id": proposal.request_id,
                    "mode": proposal.action_unit.mode,
                    "raw_text": proposal.raw_text,
                },
            )
            proposal.memory_entry_id = entry.entry_id
            buffer_version = self.buffer.put(proposal)
            if proposal.interrupt:
                self.interrupt_event.set()
            if proposal.passive_message:
                self.memory.update_history_event(
                    proposal.memory_entry_id,
                    content={"communication": proposal.passive_message},
                )
                if self.event_sink is not None:
                    self.event_sink(
                        "chat.passive",
                        agent_id=self.agent_id,
                        plan_id=proposal.plan_id,
                        request_id=proposal.request_id,
                    )
            self.planned_count += 1
            if self.event_sink is not None:
                self.event_sink(
                    "proposal.created",
                    agent_id=self.agent_id,
                    plan_id=proposal.plan_id,
                    request_id=proposal.request_id,
                    buffer_version=buffer_version,
                    action=proposal.action_unit.action,
                    mode=proposal.action_unit.mode,
                    interrupt=proposal.interrupt,
                )
            self.consecutive_errors = 0
            self.last_prompt_error = None
            self.planning_error_history.clear()
        except Exception as exc:  # noqa: BLE001
            self.consecutive_errors += 1
            error = str(exc)
            self.last_prompt_error = error
            self.planning_error_history.append(error)
            if self.event_sink is not None:
                self.event_sink(
                    "planning.failed",
                    agent_id=self.agent_id,
                    consecutive_errors=self.consecutive_errors,
                    error=error,
                )
            if self.consecutive_errors >= self.config.max_consecutive_errors:
                self.consecutive_errors = 0
                self.last_prompt_error = None
                self.planning_error_history.clear()

    def _maybe_passive_communicate(self) -> None:
        """Send one passive-communication message per new action-result entry.

        Runs before the planning call in the same planning poll, using the
        previously recorded action-result feedback (``result`` channel) as the
        prompt input.  The plain-text reply is stored in the ``passive``
        channel.  Failures never block planning.
        """
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        latest = self._latest_result_entry()
        if latest is None or latest.get("entry_id") == self._last_passive_result_id:
            return
        if self.budget is not None and not self.budget.try_acquire(self.agent_id):
            return
        content = latest.get("content")
        feedback = (
            str(content.get("result_description") or "")
            if isinstance(content, dict)
            else str(content or "")
        )
        try:
            request = self.prompt_builder.build_passive(
                agent_id=self.agent_id,
                teammates=self.teammates,
                last_action_result=feedback,
            )
            response = self.llm_client.complete(request)
            if self._stop_event.is_set():
                return
            if self.liveness_gate is not None and not self.liveness_gate.is_live(
                self.agent_id
            ):
                return
            text = (getattr(response, "content", None) or "").strip()
            request_id = getattr(response, "request_id", None)
            if text and text != "-":
                self.memory.append_history_event(
                    agent_id=self.agent_id,
                    source="self",
                    event_type="communication",
                    content={"message": text},
                    metadata={"request_id": request_id, "source": "action_result"},
                )
                if self.event_sink is not None:
                    self.event_sink(
                        "chat.passive",
                        agent_id=self.agent_id,
                        request_id=request_id,
                        source="action_result",
                    )
            self._last_passive_result_id = latest.get("entry_id")
        except Exception as exc:  # noqa: BLE001
            if self.event_sink is not None:
                self.event_sink(
                    "passive_communication.failed",
                    agent_id=self.agent_id,
                    error=str(exc),
                )

    def _latest_result_entry(self) -> Any | None:
        """Most recent ``result``-channel chat entry produced by this agent."""
        for entry in reversed(
            self.memory.prompt_history(self.agent_id, team_limit=50, system_limit=1)
        ):
            if entry.get("source") == "self" and entry.get("content", {}).get("result_status"):
                return entry
        return None

    def _mark_superseded(self, proposal: PPAProposal) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        entry_id = proposal.memory_entry_id
        if not entry_id:
            return
        entry = self.memory.get(entry_id)
        if entry is None or entry.metadata.get("status") != "planned":
            return
        metadata = dict(entry.metadata)
        metadata.update({"status": "superseded", "completed_at": time.time()})
        self.memory.update(entry_id, metadata=metadata)
        if self.event_sink is not None:
            self.event_sink(
                "proposal.superseded",
                agent_id=self.agent_id,
                plan_id=proposal.plan_id,
                request_id=proposal.request_id,
            )


class ActingWorker(LoopWorker):
    """Own action dispatch and enforce latest-buffer interrupt semantics."""

    def __init__(
        self,
        *,
        agent: Any,
        buffer: SingleSlotOverwriteBuffer,
        interrupt_event: threading.Event,
        memory: TeamMemory,
        config: PPAConfig,
        instructions: SimpleInstructionRegistry | None = None,
        broadcast: Callable[[str], None] | None = None,
        broadcast_results: bool = True,
        liveness_gate: EntityLivenessGate | None = None,
        event_sink: EventSink | None = None,
        execution_table: ActionExecutionTable | None = None,
        call_file_resolver: Callable[[str | None], str | None] | None = None,
        task_category: str | None = None,
        trade_action_observer: Callable[..., Any] | None = None,
    ) -> None:
        agent_id = str(agent.username)
        super().__init__(
            name=f"ppa-acting-{agent_id}",
            loop_interval=config.acting_poll_interval,
        )
        self.agent = agent
        self.agent_id = agent_id
        self.planner = agent.action_planner
        self.buffer = buffer
        self.interrupt_event = interrupt_event
        self.memory = memory
        self.config = config
        self.instructions = instructions
        self.broadcast = broadcast
        self.broadcast_results = broadcast_results
        self.liveness_gate = liveness_gate
        self.event_sink = event_sink
        self.execution_table = execution_table
        self.call_file_resolver = call_file_resolver
        self.task_category = task_category
        self.trade_action_observer = trade_action_observer
        self.attack_detection = tracker_for(agent)
        self.seen_version = 0
        self.active_proposal: PPAProposal | None = None
        self.active_action_id: str | None = None
        self.active_buffer_version: int | None = None
        self.executed_count = 0
        self.deferred_interrupts = 0
        self.teardown_error: str | None = None
        self._interrupted_previous_flag = False
        self.execution_started_at: float | None = None
        self.interrupted_previous = False
        self.status_poll_errors = 0

    def iterate(self) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            if self.active_proposal is not None:
                self._abort_active_due_to_liveness()
            return
        if self.active_proposal is not None:
            self._advance_active()
            return
        latest = self.buffer.peek()
        if latest is None or latest[0] <= self.seen_version:
            return
        version, proposal = latest
        if not isinstance(proposal, PPAProposal):
            raise TypeError("PPA action buffer contains an invalid item")
        self.seen_version = version
        self.interrupt_event.clear()
        self.active_buffer_version = version
        self._dispatch(proposal)

    def teardown(self) -> None:
        if self.active_proposal is None:
            return
        if (
            self.active_proposal.action_unit.mode == ActionMode.CONTINUOUS
            and self.active_action_id
        ):
            try:
                result = self.planner.stop(
                    self.active_action_id,
                    reason="runner_stopped",
                )
                if not result.ok:
                    self.teardown_error = str(
                        result.error or "continuous action stop failed"
                    )
                self._finish_active(result, broadcast=False)
            except Exception as exc:  # noqa: BLE001
                self.teardown_error = str(exc)
                self._finish_active(
                    None,
                    status="failed",
                    error=str(exc),
                    broadcast=False,
                )

    def _advance_active(self) -> None:
        proposal = self.active_proposal
        if proposal is None:
            return
        if proposal.action_unit.mode != ActionMode.CONTINUOUS:
            return
        if self.interrupt_event.is_set():
            if not self.active_action_id:
                raise RuntimeError("continuous action has no action id")
            result = self.planner.stop(
                self.active_action_id,
                reason="planning_interrupt",
            )
            if not result.ok:
                if self.event_sink is not None:
                    self.event_sink(
                        "action.interrupt_failed",
                        agent_id=self.agent_id,
                        plan_id=proposal.plan_id,
                        request_id=proposal.request_id,
                        action_id=self.active_action_id,
                        action=proposal.action_unit.action,
                        error=str(result.error),
                    )
                raise RuntimeError(
                    f"failed to interrupt {proposal.action_unit.action}: {result.error}"
                )
            interrupted_action_id = self.active_action_id
            self._interrupted_previous_flag = True
            self._finish_active(result, status="interrupted")
            if self.event_sink is not None:
                self.event_sink(
                    "action.interrupted",
                    agent_id=self.agent_id,
                    plan_id=proposal.plan_id,
                    request_id=proposal.request_id,
                    action_id=interrupted_action_id,
                    action=proposal.action_unit.action,
                )
            self.interrupt_event.clear()
            return
        if not self.active_action_id:
            raise RuntimeError("continuous action has no action id")
        try:
            result = self.planner.status(self.active_action_id)
            self.status_poll_errors = 0
        except Exception as exc:  # noqa: BLE001
            self.status_poll_errors += 1
            if self.event_sink is not None:
                self.event_sink(
                    "action.status_failed",
                    agent_id=self.agent_id,
                    plan_id=proposal.plan_id,
                    request_id=proposal.request_id,
                    action_id=self.active_action_id,
                    action=proposal.action_unit.action,
                    consecutive_errors=self.status_poll_errors,
                    error=str(exc),
                )
            if self.status_poll_errors < self.config.max_consecutive_errors:
                return
            action_id = self.active_action_id
            stop_error = None
            try:
                stop_result = self.planner.stop(
                    action_id,
                    reason="status_poll_failed",
                    timeout=self.config.worker_stop_timeout,
                )
            except Exception as stop_exc:  # noqa: BLE001
                stop_result = None
                stop_error = str(stop_exc)
            message = (
                f"action status failed {self.status_poll_errors} times: {exc}"
            )
            if stop_error:
                message += f"; stop failed: {stop_error}"
            self._finish_active(stop_result, status="failed", error=message)
            return
        if self.event_sink is not None:
            self.event_sink(
                "action.status",
                agent_id=self.agent_id,
                plan_id=proposal.plan_id,
                request_id=proposal.request_id,
                action_id=self.active_action_id,
                action=proposal.action_unit.action,
                status=str(getattr(result, "status", "")),
                error=(
                    str(result.error)
                    if getattr(result, "error", None) is not None
                    else None
                ),
            )
        status = getattr(result, "status", None)
        if status in ActionStatus.TERMINAL:
            self._finish_active(result)

    def _dispatch(self, proposal: PPAProposal) -> None:
        self.active_proposal = proposal
        self.execution_started_at = time.time()
        self.status_poll_errors = 0
        self.interrupted_previous = self._interrupted_previous_flag
        self._interrupted_previous_flag = False
        self._set_entry_metadata(
            proposal,
            status="running",
            started_at=time.time(),
        )
        self.executed_count += 1
        if self.event_sink is not None:
            self.event_sink(
                "action.started",
                agent_id=self.agent_id,
                plan_id=proposal.plan_id,
                request_id=proposal.request_id,
                buffer_version=self.active_buffer_version,
                action=proposal.action_unit.action,
                mode=proposal.action_unit.mode,
            )
        if proposal.action_unit.mode == ActionMode.CONTINUOUS:
            result = self.planner.start_unit(proposal.action_unit)
            self.active_action_id = result.action_id
            if not result.ok or result.status in ActionStatus.TERMINAL:
                self._finish_active(result)
            elif not self.active_action_id:
                self._finish_active(
                    result,
                    status="failed",
                    error="continuous action started without action_id",
                )
                raise RuntimeError("continuous action started without action_id")
            return

        result = self.planner.execute_unit(proposal.action_unit)
        deferred = self.interrupt_event.is_set()
        self._finish_active(result, deferred=deferred)
        if deferred:
            self.deferred_interrupts += 1
            latest = self.buffer.peek()
            if latest is not None and isinstance(latest[1], PPAProposal):
                self._set_entry_metadata(latest[1], interrupt_state="deferred")
                if self.event_sink is not None:
                    self.event_sink(
                        "action.interrupt_deferred",
                        agent_id=self.agent_id,
                        plan_id=latest[1].plan_id,
                        request_id=latest[1].request_id,
                    )

    def _finish_active(
        self,
        result: Any,
        *,
        status: str | None = None,
        error: str | None = None,
        broadcast: bool = True,
        deferred: bool = False,
    ) -> None:
        proposal = self.active_proposal
        if proposal is None:
            return
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            self.active_proposal = None
            self.active_action_id = None
            self.active_buffer_version = None
            self.status_poll_errors = 0
            return
        processed = postprocess_action_result(result) if result is not None else {}
        result_status = status or processed.get("status")
        if not result_status:
            result_status = "completed" if processed.get("ok") else "failed"
        result_error = error or processed.get("error")
        if is_successful_execution(str(result_status), processed):
            self.attack_detection.mark_success()
        if self.trade_action_observer is not None:
            self.trade_action_observer(
                self.agent_id,
                proposal.action_unit.action,
                proposal.input_params,
                processed,
                str(result_status),
            )
        if proposal.memory_entry_id:
            self.memory.update_history_event(
                proposal.memory_entry_id,
                content={
                    "result_description": _execution_feedback(
                        processed, str(result_status), result_error
                    ),
                    "result_status": str(result_status),
                    "result_data": processed or None,
                },
                metadata={"status": str(result_status), "completed": True},
            )
        if processed.get("ok"):
            self._record_active_chat(proposal)
        if broadcast and (self.broadcast_results or self.task_category == "trade"):
            self._broadcast_result(
                proposal,
                str(result_status),
                str(result_error) if result_error else None,
                deferred,
                processed,
            )
        if self.event_sink is not None:
            self.event_sink(
                "action.finished",
                agent_id=self.agent_id,
                plan_id=proposal.plan_id,
                request_id=proposal.request_id,
                action_id=self.active_action_id,
                action=proposal.action_unit.action,
                status=str(result_status),
                error=str(result_error) if result_error else None,
            )
        self.active_proposal = None
        self.active_action_id = None
        self.active_buffer_version = None
        self.status_poll_errors = 0
        self._write_execution_row(
            proposal,
            str(result_status),
            result_error,
            processed,
        )

    def _write_execution_row(
        self,
        proposal: PPAProposal,
        result_status: str,
        result_error: str | None,
        processed: dict[str, Any],
    ) -> None:
        if self.execution_table is None:
            return
        planning_duration = None
        if (
            proposal.planning_started_at is not None
            and proposal.planning_finished_at is not None
        ):
            planning_duration = (
                proposal.planning_finished_at - proposal.planning_started_at
            )
        execution_finished = time.time()
        execution_duration = None
        if self.execution_started_at is not None:
            execution_duration = execution_finished - self.execution_started_at
        self.execution_table.write_row(
            {
                "agent": self.agent_id,
                "action": render_action_call(
                    proposal.action_unit.action,
                    proposal.action_unit.params,
                ),
                "call_file": (
                    self.call_file_resolver(proposal.request_id)
                    if self.call_file_resolver is not None
                    else None
                ),
                "planning_started_at": proposal.planning_started_at,
                "planning_finished_at": proposal.planning_finished_at,
                "planning_duration_seconds": planning_duration,
                "execution_started_at": self.execution_started_at,
                "execution_finished_at": execution_finished,
                "execution_duration_seconds": execution_duration,
                "interrupted_previous": bool(self.interrupted_previous),
                "interrupted": result_status == "interrupted",
                "feedback": _execution_feedback(
                    processed,
                    result_status,
                    result_error,
                ),
                "plan_source": "",
            }
        )

    def _broadcast_result(
        self,
        proposal: PPAProposal,
        result_status: str,
        result_error: str | None,
        deferred: bool,
        processed: dict[str, Any] | None = None,
    ) -> None:
        """Record the action-result feedback into the ``result`` chat channel.

        When a ``broadcast`` callback is wired (e.g. RCON ``say``), the text is
        also sent to the server; the memory copy feeds the planning thread's
        passive communication prompt as the previous-action feedback.
        """
        if self.task_category == "trade":
            trade_text = publish_trade_purchase_broadcast(
                memory=self.memory,
                entry_id=proposal.memory_entry_id,
                agent_id=self.agent_id,
                action=proposal.action_unit.action,
                result=processed or {},
                status=result_status,
                broadcast=self.broadcast,
                broadcast_enabled=self.broadcast_results,
                event_sink=self.event_sink,
            )
            if trade_text:
                return
        text = self._broadcast_text(
            proposal,
            result_status,
            result_error,
            deferred,
        )
        if not text:
            return
        try:
            if proposal.memory_entry_id:
                self.memory.update_history_event(
                    proposal.memory_entry_id,
                    content={"result_broadcast": text},
                )
            if self.broadcast_results and callable(self.broadcast):
                self.broadcast(text)
        except Exception as exc:  # noqa: BLE001
            if self.event_sink is not None:
                self.event_sink(
                    "action.broadcast_failed",
                    agent_id=self.agent_id,
                    plan_id=proposal.plan_id,
                    error=str(exc),
                )

    def _broadcast_text(
        self,
        proposal: PPAProposal,
        result_status: str,
        result_error: str | None,
        deferred: bool,
    ) -> str:
        action = proposal.action_unit.action
        alias = action
        if self.instructions is not None:
            instruction = self.instructions.instruction_for_action(action)
            if instruction is not None:
                alias = instruction.alias
        call = f"执行{alias}({self._render_input_args(proposal)})"
        if result_status == "interrupted":
            suffix = "中断"
        elif result_status == "failed":
            reason = (result_error or "").strip()
            suffix = f"失败：{reason}" if reason else "失败"
        else:
            suffix = "完成"
        if deferred:
            suffix += "（中断延后）"
        return f"{self.agent_id}：{call} {suffix}"

    @staticmethod
    def _render_input_args(proposal: PPAProposal) -> str:
        if proposal.input_positional:
            return ", ".join(repr(value) for value in proposal.input_positional)
        return ", ".join(
            f"{name}={value!r}" for name, value in proposal.input_params.items()
        )

    def _record_active_chat(self, proposal: PPAProposal) -> None:
        action = proposal.action_unit.action
        params = proposal.action_unit.params
        if action == "chat.say" and params.get("message"):
            if proposal.memory_entry_id:
                self.memory.update_history_event(
                    proposal.memory_entry_id,
                    content={"communication": str(params["message"])},
                )
        elif action == "chat.whisper" and params.get("message"):
            if proposal.memory_entry_id:
                self.memory.update_history_event(
                    proposal.memory_entry_id,
                    content={"communication": str(params["message"])},
                    metadata={"recipient": params.get("username")},
                )

    def _set_entry_metadata(self, proposal: PPAProposal, **updates: Any) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        entry_id = proposal.memory_entry_id
        if not entry_id:
            return
        entry = self.memory.get(entry_id)
        if entry is None:
            return
        metadata = dict(entry.metadata)
        metadata.update(updates)
        self.memory.update(entry_id, metadata=metadata)

    def _abort_active_due_to_liveness(self) -> None:
        proposal = self.active_proposal
        if proposal is None:
            return
        if proposal.action_unit.mode == ActionMode.CONTINUOUS and self.active_action_id:
            try:
                self.planner.stop(
                    self.active_action_id,
                    reason="agent_not_live",
                )
            except Exception as exc:  # noqa: BLE001
                self.teardown_error = str(exc)
        self.active_proposal = None
        self.active_action_id = None
        self.active_buffer_version = None


def _execution_feedback(
    processed: dict[str, Any],
    status: str,
    error: str | None,
) -> str:
    description = processed.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    text = f"状态 {status}"
    return f"{text}，错误 {error}" if error else text
