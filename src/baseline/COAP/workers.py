"""COAP 四条并行线路的 LoopWorker 实现。"""

from __future__ import annotations

import inspect
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from uuid import uuid4
from typing import Any, Callable, Sequence

from src.agents.action import ActionMode, ActionStatus
from src.agents.action.result_postprocessor import postprocess_action_result
from src.agents.action.task_defaults import apply_task_action_defaults
from src.llm import (
    LLMCallSource,
    LLMRequest,
    LLMRequestCancelled,
    StreamEventType,
    StreamTrigger,
)
from src.memory import TeamMemory, action_text
from src.perception.attack_detection import tracker_for
from src.runtime import LoopWorker, SingleSlotOverwriteBuffer
from src.runtime.liveness import EntityLivenessGate

from .adapters import action_unit_to_action, broadcast_text, render_catalog
from .cache import CacheService, SystemClock
from .codec import COAPDecisionCodec, should_publish_to_buffer
from .config import COAPConfig
from .consistency import ImportantParameterActionConsistency
from .prompts import COAPPromptBuilder
from .protocol import parse_action_response
from .schemas import (
    Action,
    CandidateRecord,
    CandidateStatus,
    CacheEntryChange,
    COAPProposal,
)
from .scoring import ScoreUpdater
from .request_builder import build_planning_request
from src.tasks.trade import publish_trade_purchase_broadcast


EventSink = Callable[..., None]
ChangeSink = Callable[[Sequence[CacheEntryChange]], str | None]

_STREAM_TERMINAL_EVENTS = frozenset(
    {
        StreamEventType.COMPLETED,
        StreamEventType.FAILED,
        StreamEventType.STOPPED,
    }
)


@dataclass
class PlanHandle:
    """后台普通规划请求的生命周期状态。"""

    request_id: str
    plan_id: str | None
    race_id: str | None
    epoch: int
    operation: str
    previous_action: Action
    feedbacks: tuple[str, ...] = ()
    attempt: int = 0
    detached: bool = False
    action_ready: bool = False
    stream_completed: bool = False
    terminal: bool = False
    proposal: COAPProposal | None = None
    full_output: str = ""
    error: str | None = None
    detached_recorded: bool = False
    action_emitted: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


@dataclass(frozen=True)
class PlanResult:
    """后台规划线程交给规划 worker 串行应用的事件。"""

    handle: PlanHandle
    kind: str
    proposal: COAPProposal | None = None
    feedback: str | None = None
    full_output: str = ""


def _drain_stream_in_background(
    iterator: Any,
    *,
    name: str,
    on_event: Callable[[Any], None],
) -> None:
    """Continue consuming a stream after the action-ready event is returned."""

    def drain() -> None:
        try:
            for event in iterator:
                on_event(event)
        except Exception:
            # The action has already been parsed and published. A malformed or
            # interrupted tail must not turn it into a planning failure.
            return

    threading.Thread(target=drain, name=name, daemon=True).start()


def _close_stream_iterator(iterator: Any) -> None:
    close = getattr(iterator, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            return


def _complete_with_stop_event(
    client: Any,
    request: LLMRequest,
    stop_event: threading.Event,
) -> Any:
    complete = client.complete
    try:
        parameters = inspect.signature(complete).parameters.values()
        accepts_stop_event = any(
            parameter.name == "stop_event"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        accepts_stop_event = False
    if accepts_stop_event:
        return complete(request, stop_event=stop_event)
    return complete(request)


def _discard_proposal_tail(proposal: COAPProposal, memory: TeamMemory) -> None:
    proposal.discard_stream_tail = True
    if not proposal.memory_entry_id:
        return
    field = "cache_plan" if proposal.path == "cache" else "standard_plan"
    memory.update_history_event(
        proposal.memory_entry_id,
        content={field: None},
        metadata={f"{field}_discarded": True},
    )


def _proposal_history_action(
    proposal: COAPProposal,
    instructions: Any | None,
) -> str:
    """Render only the model-supplied action arguments for history."""
    params = proposal.input_params or proposal.action_unit.params
    return action_text(
        proposal.action_unit.action,
        params,
        instructions=instructions,
        positional=proposal.input_positional,
    )


def _commit_proposal_communication(
    proposal: COAPProposal,
    *,
    memory: TeamMemory,
    agent_id: str,
    config: COAPConfig,
    broadcast: Callable[[str], None] | None = None,
    event_sink: EventSink | None = None,
) -> None:
    if proposal.communication_committed:
        return
    text = (proposal.communication or "").strip()
    if not text:
        return
    try:
        if proposal.memory_entry_id:
            memory.update_history_event(
                proposal.memory_entry_id,
                content={"communication": text},
                metadata={"communication_committed": True},
            )
        else:
            memory.append_history_event(
                agent_id=agent_id,
                source="self",
                event_type="communication",
                content={"message": text},
                metadata={"plan_id": proposal.plan_id},
            )
    except Exception as exc:  # noqa: BLE001
        if event_sink is not None:
            event_sink(
                "communication.commit_failed",
                agent_id=agent_id,
                error=str(exc),
            )
        return
    proposal.communication_committed = True
    if config.broadcast_results and callable(broadcast):
        try:
            broadcast(text)
        except Exception as exc:  # noqa: BLE001
            if event_sink is not None:
                event_sink(
                    "communication.broadcast_failed",
                    agent_id=agent_id,
                    error=str(exc),
                )


class COAPDecisionCoordinator:
    """每个 Agent 的决策协调状态：上一动作、动作结束通知、竞速 claim。"""

    def __init__(
        self,
        *,
        agent_id: str,
        cache_service: CacheService,
        task_scope: str,
        clock: Any | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.cache_service = cache_service
        self.task_scope = task_scope
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        self.last_action: Action | None = None
        self.action_active = False
        self.buffer_has_candidate = False
        self.pending_interrupt_prev: Action | None = None
        self.race_active = False
        self.force_dual_next = False
        self.cache_plan_failure_reason: str | None = None
        self._race_claim = threading.Lock()
        self.action_finished = threading.Event()
        self.cache_race_event = threading.Event()
        self.cache_plan_failed = threading.Event()
        self.history_entry_id: str | None = None
        self.history_race_id: str | None = None
        self._race_id: str | None = None
        self._cache_race_id: str | None = None
        self._race_winners: dict[str, str] = {}
        self._race_cache_candidates: dict[str, tuple[str, ...]] = {}
        self._race_actions: dict[str, dict[str, Action]] = {}

    def on_action_started(self) -> None:
        with self._lock:
            self.action_active = True
            self.buffer_has_candidate = False
            if self._race_claim.locked():
                self._race_claim.release()
            self.race_active = False
            self.history_entry_id = None
            self.history_race_id = None

    def mark_candidate_written(self) -> None:
        with self._lock:
            self.buffer_has_candidate = True

    def on_action_finished(self, last_action: Action | None) -> None:
        with self._lock:
            self.last_action = last_action
            self.action_active = False
        self.action_finished.set()

    def begin_race(self) -> None:
        with self._lock:
            if self._race_claim.locked():
                self._race_claim.release()
            self._race_id = uuid4().hex
            self._cache_race_id = self._race_id
            self.race_active = True
            self._race_winners[self._race_id] = ""
            self._race_cache_candidates[self._race_id] = ()
            self._race_actions[self._race_id] = {}

    @property
    def race_id(self) -> str | None:
        with self._lock:
            return self._race_id

    @property
    def active_race_id(self) -> str | None:
        with self._lock:
            if not self.race_active:
                return None
            return self._race_id

    @property
    def cache_race_id(self) -> str | None:
        with self._lock:
            return self._cache_race_id

    def request_cache_race(self) -> None:
        with self._lock:
            self._cache_race_id = self._race_id if self.race_active else None
            self.cache_race_event.set()

    def is_race_open(self, race_id: str | None) -> bool:
        with self._lock:
            return bool(
                race_id
                and self.race_active
                and self._race_id == race_id
            )

    def mark_race_winner(self, path: str, race_id: str | None = None) -> None:
        with self._lock:
            current = race_id
            if not current or current != self._race_id:
                return
            if current in self._race_winners and not self._race_winners[current]:
                self._race_winners[current] = path

    def race_winner(self, race_id: str | None) -> str | None:
        with self._lock:
            if not race_id:
                return None
            winner = self._race_winners.get(race_id)
            return winner or None

    def record_cache_candidates(
        self,
        race_id: str | None,
        entry_ids: Sequence[str],
    ) -> None:
        with self._lock:
            if race_id and race_id in self._race_cache_candidates:
                self._race_cache_candidates[race_id] = tuple(
                    dict.fromkeys(str(entry_id) for entry_id in entry_ids)
                )

    def cache_candidate_ids(self, race_id: str | None) -> tuple[str, ...]:
        with self._lock:
            return self._race_cache_candidates.get(race_id or "", ())

    def record_race_action(self, race_id: str | None, path: str, action: Action) -> None:
        with self._lock:
            if race_id and race_id in self._race_actions:
                self._race_actions[race_id][path] = action

    def race_action(self, race_id: str | None, path: str) -> Action | None:
        with self._lock:
            return self._race_actions.get(race_id or "", {}).get(path)

    def ensure_history(self, memory: TeamMemory) -> str:
        with self._lock:
            if self.history_entry_id is not None:
                return self.history_entry_id
            self.history_race_id = f"race-{uuid4().hex[:12]}"
            entry = memory.append_history_event(
                agent_id=self.agent_id,
                source="self",
                event_type="planning",
                content={
                    "coap_record": True,
                    "cache_plan": None,
                    "standard_plan": None,
                    "selected_action": None,
                    "reasoning": None,
                    "communication": None,
                    "result_description": "",
                    "result_status": None,
                    "result_data": None,
                },
                metadata={"race_id": self.history_race_id, "method": "coap"},
            )
            self.history_entry_id = entry.entry_id
            return entry.entry_id

    def update_history(
        self,
        memory: TeamMemory,
        *,
        content: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        memory.update_history_event(
            self.ensure_history(memory),
            content=content,
            metadata=metadata,
        )

    def try_claim(self, path: str | None = None) -> bool:
        """竞速获胜判定：非竞速（定时）直接放行；竞速则抢占唯一席位。"""
        with self._lock:
            if not self.race_active:
                return True
            acquired = self._race_claim.acquire(blocking=False)
            if acquired and path and self._race_id is not None:
                self._race_winners[self._race_id] = path
            return acquired

    def end_race_if_open(self) -> None:
        with self._lock:
            if self.race_active:
                self.race_active = False
                if self._race_claim.locked():
                    self._race_claim.release()

    def mark_cache_plan_failed(self, reason: str | None = None) -> None:
        with self._lock:
            self.cache_plan_failure_reason = reason or "cache planning failed"
            self.cache_plan_failed.set()

    def consume_cache_plan_failure(self) -> str | None:
        with self._lock:
            if not self.cache_plan_failed.is_set():
                return None
            reason = self.cache_plan_failure_reason
            self.cache_plan_failure_reason = None
            self.cache_plan_failed.clear()
            self.history_entry_id = None
            self.history_race_id = None
            return reason or "cache planning failed"

    def owner(self) -> str | None:
        return self.cache_service.owner_for(self.agent_id)

    def on_agent_not_live(self) -> None:
        with self._lock:
            self.action_active = False
            self.buffer_has_candidate = False
            self.pending_interrupt_prev = None
            self.race_active = False
            self.force_dual_next = False
            self.cache_plan_failure_reason = None
            self._race_id = None
            self._cache_race_id = None
            self._race_winners.clear()
            self._race_cache_candidates.clear()
            self._race_actions.clear()
            self.action_finished.clear()
            self.cache_race_event.clear()
            self.cache_plan_failed.clear()
            if self._race_claim.locked():
                self._race_claim.release()


class COAPActingWorker(LoopWorker):
    """线路①：动作执行、轮询、中断、结果反馈与候选记录。"""

    def __init__(
        self,
        *,
        agent: Any,
        buffer: SingleSlotOverwriteBuffer,
        interrupt_event: threading.Event,
        memory: TeamMemory,
        config: COAPConfig,
        coordinator: COAPDecisionCoordinator,
        cache_service: CacheService,
        score_updater: ScoreUpdater | None = None,
        consistency: Any | None = None,
        instructions: Any | None = None,
        broadcast: Callable[[str], None] | None = None,
        event_sink: EventSink | None = None,
        execution_table: Any | None = None,
        call_file_resolver: Callable[[str | None], str | None] | None = None,
        task_category: str | None = None,
        trade_action_observer: Callable[..., Any] | None = None,
        liveness_gate: EntityLivenessGate | None = None,
    ) -> None:
        agent_id = str(agent.username)
        super().__init__(
            name=f"coap-acting-{agent_id}",
            loop_interval=config.acting_poll_interval,
        )
        self.agent = agent
        self.agent_id = agent_id
        self.planner = agent.action_planner
        self.buffer = buffer
        self.interrupt_event = interrupt_event
        self.memory = memory
        self.config = config
        self.coordinator = coordinator
        self.cache_service = cache_service
        self.score_updater = score_updater
        self.consistency = consistency
        self.instructions = instructions
        self.broadcast = broadcast
        self.event_sink = event_sink
        self.execution_table = execution_table
        self.call_file_resolver = call_file_resolver
        self.liveness_gate = liveness_gate
        self.seen_version = 0
        self.active_proposal: COAPProposal | None = None
        self.active_action_id: str | None = None
        self.executed_count = 0
        self.record_sequence = 0
        self._interrupted_previous_flag = False
        self.execution_started_at: float | None = None
        self.interrupted_previous = False
        self.status_poll_errors = 0
        self.task_category = task_category
        self.trade_action_observer = trade_action_observer
        self.attack_detection = tracker_for(agent)

    def iterate(self) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            if self.active_proposal is not None:
                self._abort_active_due_to_liveness()
            self.coordinator.on_agent_not_live()
            return
        if self.active_proposal is not None:
            self._advance_active()
            return
        latest = self.buffer.peek()
        if latest is None or latest[0] <= self.seen_version:
            return
        version, proposal = latest
        if not isinstance(proposal, COAPProposal):
            raise TypeError("COAP 动作缓冲包含无效条目")
        self.seen_version = version
        self.interrupt_event.clear()
        self.coordinator.on_action_started()
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
                self._finish_active(
                    result,
                    status=(
                        "interrupted"
                        if not getattr(result, "ok", False)
                        else "completed"
                    ),
                    broadcast=False,
                )
            except Exception as exc:  # noqa: BLE001
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
                raise RuntimeError(
                    f"failed to interrupt {proposal.action_unit.action}: {result.error}"
                )
            self.coordinator.pending_interrupt_prev = action_unit_to_action(
                proposal.action_unit
            )
            self._interrupted_previous_flag = True
            self._finish_active(result, status="interrupted")
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
        status = getattr(result, "status", None)
        if status in ActionStatus.TERMINAL:
            self._finish_active(result)

    def _dispatch(self, proposal: COAPProposal) -> None:
        apply_task_action_defaults(self.task_category, proposal)
        self.active_proposal = proposal
        self.execution_started_at = time.time()
        self.status_poll_errors = 0
        self.interrupted_previous = self._interrupted_previous_flag
        self._interrupted_previous_flag = False
        self.executed_count += 1
        if proposal.action_unit.mode == ActionMode.CONTINUOUS:
            result = self.planner.start_unit(proposal.action_unit)
            self.active_action_id = getattr(result, "action_id", None)
            if not result.ok or getattr(result, "status", None) in ActionStatus.TERMINAL:
                self._finish_active(result)
            return
        result = self.planner.execute_unit(proposal.action_unit)
        self._finish_active(result)

    def _finish_active(
        self,
        result: Any,
        *,
        status: str | None = None,
        error: str | None = None,
        broadcast: bool = True,
    ) -> None:
        proposal = self.active_proposal
        if proposal is None:
            return
        proposal.execution_finished = True
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            self.coordinator.on_agent_not_live()
            self.active_proposal = None
            self.active_action_id = None
            self.status_poll_errors = 0
            return
        processed = postprocess_action_result(result) if result is not None else {}
        result_status = status or processed.get("status")
        if not result_status:
            result_status = "completed" if processed.get("ok") else "failed"
        result_error = error or processed.get("error")
        if self.trade_action_observer is not None:
            self.trade_action_observer(
                self.agent_id,
                proposal.action_unit.action,
                proposal.input_params,
                processed,
                str(result_status),
            )
        interrupted = result_status == "interrupted"
        success = (
            bool(processed.get("ok"))
            and result_status != "failed"
            and not interrupted
        )
        if success:
            self.attack_detection.mark_success()
        previous = (
            self.coordinator.pending_interrupt_prev
            or self.coordinator.last_action
            or Action(name="START")
        )
        self.coordinator.pending_interrupt_prev = None
        executed = action_unit_to_action(proposal.action_unit)
        if proposal.memory_entry_id:
            self.memory.update_history_event(
                proposal.memory_entry_id,
                content={
                    "selected_action": _proposal_history_action(
                        proposal,
                        self.instructions,
                    ),
                    "result_description": _execution_feedback(
                        processed,
                        str(result_status),
                        result_error,
                    ),
                    "result_status": str(result_status),
                    "result_data": processed or None,
                },
                metadata={"completed": True},
            )
        if broadcast:
            self._passive_broadcast(
                proposal,
                str(result_status),
                result_error,
                processed,
            )
        if result_status in {"completed", "failed", "interrupted", "stopped"}:
            self._commit_communication(proposal)
        cache_statistics_applied = False
        if interrupted:
            candidate_status = CandidateStatus.UNCERTAIN
        elif success:
            candidate_status = CandidateStatus.SUCCESS
            cache_statistics_applied = self._record_cache_feedback(
                previous,
                executed,
                feedback_id=proposal.plan_id,
                success=True,
            )
        else:
            candidate_status = CandidateStatus.FAILURE
            cache_statistics_applied = self._record_cache_feedback(
                previous,
                executed,
                feedback_id=proposal.plan_id,
                success=False,
            )
        self._record_candidate(
            proposal,
            previous=previous,
            executed=executed,
            status=candidate_status,
            reasoning=proposal.reasoning,
            cache_statistics_applied=cache_statistics_applied,
        )
        self._write_execution_row(
            proposal,
            str(result_status),
            result_error,
            processed,
        )
        self.coordinator.on_action_finished(executed)
        self.active_proposal = None
        self.active_action_id = None
        self.status_poll_errors = 0

    def _write_execution_row(
        self,
        proposal: COAPProposal,
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
                "action": _render_action(
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
                "plan_source": proposal.path,
            }
        )

    def _passive_broadcast(
        self,
        proposal: COAPProposal,
        result_status: str,
        result_error: str | None,
        processed: dict[str, Any] | None = None,
    ) -> None:
        if self.task_category == "trade":
            trade_text = publish_trade_purchase_broadcast(
                memory=self.memory,
                entry_id=proposal.memory_entry_id,
                agent_id=self.agent_id,
                action=proposal.action_unit.action,
                result=processed or {},
                status=result_status,
                broadcast=self.broadcast,
                broadcast_enabled=self.config.broadcast_results,
                event_sink=self.event_sink,
            )
            if trade_text:
                return
        text = broadcast_text(
            self.agent_id,
            proposal,
            result_status,
            result_error,
            self.instructions,
        )
        if not text:
            return
        try:
            if proposal.memory_entry_id:
                self.memory.update_history_event(
                    proposal.memory_entry_id,
                    content={"result_broadcast": text},
                )
            if self.config.broadcast_results and callable(self.broadcast):
                self.broadcast(text)
        except Exception as exc:  # noqa: BLE001
            if self.event_sink is not None:
                self.event_sink(
                    "action.broadcast_failed",
                    agent_id=self.agent_id,
                    error=str(exc),
                )

    def _commit_communication(self, proposal: COAPProposal) -> None:
        _commit_proposal_communication(
            proposal,
            memory=self.memory,
            agent_id=self.agent_id,
            config=self.config,
            broadcast=self.broadcast,
            event_sink=self.event_sink,
        )

    def _record_candidate(
        self,
        proposal: COAPProposal,
        *,
        previous: Action,
        executed: Action,
        status: CandidateStatus,
        reasoning: str,
        cache_statistics_applied: bool = False,
    ) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        if (
            previous.name
            == self.cache_service.retrieval_config.start_action_name
        ):
            return
        self.record_sequence += 1
        record = CandidateRecord(
            decision_id=f"{self.agent_id}:{proposal.plan_id}",
            agent_id=self.agent_id,
            previous_action=previous,
            next_action=executed,
            reasoning=reasoning or "coap execution",
            status=status,
            plan_id=proposal.plan_id,
            cache_statistics_applied=cache_statistics_applied,
            created_at=self.coordinator.clock.now(),
        )
        self.cache_service.buffer.add(record)

    def _record_cache_feedback(
        self,
        previous: Action,
        executed: Action,
        *,
        feedback_id: str,
        success: bool,
    ) -> bool:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return False
        if (
            previous.name
            == self.cache_service.retrieval_config.start_action_name
        ):
            return False
        if self.score_updater is None or self.consistency is None:
            return False
        candidates = self.cache_service.retriever.retrieve(
            previous,
            task_scope=self.coordinator.task_scope,
            owner_agent_id=self.coordinator.owner(),
        )
        for entry in candidates.entries:
            if self.consistency.equivalent(entry.next_action, executed):
                return self.score_updater.record_feedback(
                    entry.entry_id,
                    feedback_id=feedback_id,
                    success=success,
                )
        return False

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
            except Exception:  # noqa: BLE001
                pass
        self.active_proposal = None
        self.active_action_id = None


class COAPCacheWorker(LoopWorker):
    """线路④：缓存维护——flush 缓冲、聚合、内容更新、评分更新。"""

    def __init__(
        self,
        *,
        cache_service: CacheService,
        score_updater: ScoreUpdater,
        task_scope: str,
        config: COAPConfig,
        liveness_gate: EntityLivenessGate | None = None,
        event_sink: EventSink | None = None,
        change_sink: ChangeSink | None = None,
    ) -> None:
        super().__init__(
            name="coap-cache-maintenance",
            loop_interval=1.0,
        )
        self.cache_service = cache_service
        self.score_updater = score_updater
        self.task_scope = task_scope
        self.config = config
        self.liveness_gate = liveness_gate
        self.event_sink = event_sink
        self.change_sink = change_sink
        self.update_count = 0

    def iterate(self) -> None:
        if self._stop_event.is_set():
            return
        try:
            self._flush_candidates()
            self._update_scores()
        except Exception as exc:  # noqa: BLE001
            if self.event_sink is not None:
                self.event_sink(
                    "cache.maintenance_failed",
                    error=str(exc),
                )

    def _flush_candidates(self) -> None:
        if self._stop_event.is_set():
            return
        if not self.cache_service.buffer.should_flush():
            return
        records = self.cache_service.buffer.snapshot()
        records = self._live_records(records)
        if not records:
            return
        processed_ids: list[str] = []
        for owner, scoped in self.cache_service.partition_candidates(records):
            if self._stop_event.is_set():
                break
            aggregated = self.cache_service.aggregator.aggregate(scoped)
            if not aggregated:
                continue
            if self.cache_service.content_updater is None:
                processed_ids.extend(record.record_id for record in scoped)
                continue
            for start in range(0, len(aggregated), 3):
                if self._stop_event.is_set():
                    break
                report = self.cache_service.content_updater.apply(
                    aggregated[start : start + 3],
                    task_scope=self.task_scope,
                    owner_agent_id=owner,
                )
                processed_ids.extend(report.processed_record_ids)
                self.update_count += 1
                changes_file = (
                    self.change_sink(report.changes)
                    if self.change_sink is not None
                    else None
                )
                if self.event_sink is not None:
                    event_data: dict[str, Any] = {
                        "created": len(report.created_entry_ids),
                        "updated": len(report.updated_entry_ids),
                        "llm_calls": report.llm_call_count,
                        "change_count": len(report.changes),
                    }
                    if changes_file is not None:
                        event_data["changes_file"] = changes_file
                    self.event_sink("cache.updated", **event_data)
        if processed_ids:
            self.cache_service.buffer.acknowledge(processed_ids)

    def flush_pending_existing_statistics(self) -> tuple[Any, ...]:
        records = self.cache_service.buffer.snapshot()
        records = self._live_records(records)
        reports: list[Any] = []
        processed_ids: list[str] = []
        if records:
            for owner, scoped in self.cache_service.partition_candidates(records):
                report = self.cache_service.apply_existing_statistics(
                    scoped,
                    task_scope=self.task_scope,
                    owner_agent_id=owner,
                )
                reports.append(report)
                processed_ids.extend(report.processed_record_ids)
                changes_file = (
                    self.change_sink(report.changes)
                    if self.change_sink is not None
                    else None
                )
                if self.event_sink is not None and (
                    report.updated_entry_ids or report.processed_record_ids
                ):
                    event_data: dict[str, Any] = {
                        "updated": len(report.updated_entry_ids),
                        "processed": len(report.processed_record_ids),
                        "change_count": len(report.changes),
                    }
                    if changes_file is not None:
                        event_data["changes_file"] = changes_file
                    self.event_sink("cache.final_statistics_updated", **event_data)
        if processed_ids:
            self.cache_service.buffer.acknowledge(processed_ids)
        self._update_scores()
        return tuple(reports)

    def _update_scores(self) -> None:
        if not self.config.score_update_enabled:
            return
        if not self.score_updater.should_update():
            return
        live_agent_ids = self._live_agent_ids()
        if self.liveness_gate is not None and not live_agent_ids:
            return
        for owner in self.cache_service.maintenance_owners(live_agent_ids):
            report = self.score_updater.update_scores(
                task_scope=self.task_scope,
                owner_agent_id=owner,
                match_owner=owner is not None,
            )
            changes_file = (
                self.change_sink(report.changes)
                if self.change_sink is not None
                else None
            )
            if self.event_sink is not None:
                event_data: dict[str, Any] = {
                    "updated": len(report.updated_entry_ids),
                    "skipped": len(report.skipped_entry_ids),
                    "change_count": len(report.changes),
                }
                if changes_file is not None:
                    event_data["changes_file"] = changes_file
                self.event_sink("cache.score_updated", **event_data)

    def _agent_ids(self) -> tuple[str, ...]:
        # 由 runner 注入；此处从共享状态读取，无则返回空
        return getattr(self, "_agent_ids_value", ())

    def _live_agent_ids(self) -> tuple[str, ...]:
        agent_ids = self._agent_ids()
        if self.liveness_gate is None:
            return agent_ids
        return self.liveness_gate.live_agent_ids(agent_ids)

    def _live_records(self, records: Sequence[CandidateRecord]) -> tuple[CandidateRecord, ...]:
        if self.liveness_gate is None:
            return tuple(records)
        live: list[CandidateRecord] = []
        dropped: list[str] = []
        for record in records:
            if self.liveness_gate.is_live(record.agent_id):
                live.append(record)
            else:
                dropped.append(record.record_id)
        if dropped:
            self.cache_service.buffer.acknowledge(dropped)
        return tuple(live)


class COAPPlanningWorker(LoopWorker):
    """线路②：定时普通规划（含双路竞速中的普通路径）。"""

    def __init__(
        self,
        *,
        agent: Any,
        teammates: Sequence[str],
        llm_client: Any,
        prompt_builder: COAPPromptBuilder,
        codec: COAPDecisionCodec,
        buffer: SingleSlotOverwriteBuffer,
        interrupt_event: threading.Event,
        memory: TeamMemory,
        config: COAPConfig,
        coordinator: COAPDecisionCoordinator,
        registry: Any,
        spec: Any,
        instructions: Any | None = None,
        budget: Any | None = None,
        broadcast: Callable[[str], None] | None = None,
        event_sink: EventSink | None = None,
        clock: Any | None = None,
        trade_recipe_history: Any | None = None,
        liveness_gate: EntityLivenessGate | None = None,
    ) -> None:
        agent_id = str(agent.username)
        super().__init__(
            name=f"coap-planning-{agent_id}",
            loop_interval=0.05,
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
        self.coordinator = coordinator
        self.registry = registry
        self.spec = spec
        self.instructions = instructions
        self.budget = budget
        self.broadcast = broadcast
        self.event_sink = event_sink
        self.clock = clock or SystemClock()
        self.trade_recipe_history = trade_recipe_history
        self.liveness_gate = liveness_gate
        self._next_plan_at = self.clock.monotonic()
        self._planning_in_flight = False
        self._initial_standard_plan_started = False
        self.planned_count = 0
        self.consecutive_errors = 0
        self.last_plan_error: str | None = None
        self._race_failed_this_iteration = False
        self._plan_epoch = 0
        self._plan_results: Queue[PlanResult] = Queue()
        self._plan_handles: dict[int, PlanHandle] = {}
        self._background_threads: set[threading.Thread] = set()
        self._background_lock = threading.RLock()
        self._consistency = ImportantParameterActionConsistency(
            config.important_parameters,
            coordinator.cache_service.action_canonicalizer,
        )

    def teardown(self) -> None:
        """等待有限时间让已提交的普通规划完成收尾。"""
        deadline = time.monotonic() + self.config.worker_stop_timeout
        while True:
            with self._background_lock:
                threads = [thread for thread in self._background_threads if thread.is_alive()]
            if not threads:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for thread in threads:
                thread.join(timeout=min(0.01, remaining))
        self._drain_plan_results()

    def iterate(self) -> None:
        self._race_failed_this_iteration = False
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            self.coordinator.on_agent_not_live()
            return
        self._drain_plan_results()
        self._detach_cache_losers()
        self._refresh_planning_in_flight()
        if self.memory.get_observation(self.agent_id) is None:
            return
        self._handle_action_finished()
        if self._handle_cache_plan_failure():
            return
        if self._handle_forced_dual_restart():
            return
        if self._maybe_run_initial_standard_plan():
            return
        if not self.config.timed_planning_enabled:
            return
        now = self.clock.monotonic()
        if now >= self._next_plan_at and not self._planning_in_flight:
            self._next_plan_at = now + self.config.planning_interval
            self._start_standard_plan()

    def _refresh_planning_in_flight(self) -> None:
        self._planning_in_flight = any(
            not handle.detached
            and not handle.action_ready
            and not handle.terminal
            for handle in self._plan_handles.values()
        )

    def _detach_cache_losers(self) -> None:
        """缓存先抢到竞速席位后，解除普通规划对下一轮的占用。"""
        for handle in tuple(self._plan_handles.values()):
            if handle.detached or handle.race_id is None:
                continue
            if self.coordinator.race_winner(handle.race_id) != "cache":
                continue
            handle.detached = True
            if handle.proposal is not None and handle.action_ready:
                self._record_detached_plan(
                    handle.proposal,
                    previous=handle.previous_action,
                    handle=handle,
                    reason="cache path won race",
                )
        self._refresh_planning_in_flight()

    def _drain_plan_results(self) -> None:
        while True:
            try:
                result = self._plan_results.get_nowait()
            except Empty:
                return
            handle = self._plan_handles.get(result.handle.epoch)
            if handle is None:
                continue
            if result.kind == "action_ready":
                self._apply_plan_action_ready(handle, result)
            elif result.kind == "completed":
                self._apply_plan_completed(handle, result)
            elif result.kind == "tail_failed":
                self._apply_plan_tail_failed(handle, result)
            elif result.kind == "failed":
                self._apply_plan_failed(handle, result)
            if handle.terminal:
                self._plan_handles.pop(handle.epoch, None)
            self._refresh_planning_in_flight()

    def _apply_plan_action_ready(
        self,
        handle: PlanHandle,
        result: PlanResult,
    ) -> None:
        proposal = result.proposal
        if proposal is None or handle.action_ready:
            return
        handle.action_ready = True
        handle.proposal = proposal
        handle.plan_id = proposal.plan_id
        handle.full_output = result.full_output or handle.full_output
        self.planned_count += 1
        if handle.detached or not self.coordinator.is_race_open(handle.race_id):
            handle.detached = True
            self._record_detached_plan(
                proposal,
                previous=handle.previous_action,
                handle=handle,
                reason="race already closed",
            )
            self._record_standard_action(handle, proposal)
            return
        self._publish(proposal, handle=handle)
        self.consecutive_errors = 0
        self.last_plan_error = None
        self._record_standard_action(handle, proposal)

    def _apply_plan_completed(self, handle: PlanHandle, result: PlanResult) -> None:
        handle.stream_completed = True
        handle.terminal = True
        handle.full_output = result.full_output or handle.full_output
        proposal = result.proposal or handle.proposal
        if proposal is None:
            return
        handle.proposal = proposal
        if handle.detached:
            self._record_detached_plan(
                proposal,
                previous=handle.previous_action,
                handle=handle,
                reason="race loser completed",
            )
            self._record_standard_action(handle, proposal)
            return
        if proposal.execution_finished:
            return
        if handle.full_output:
            self._fill_communication(
                proposal,
                handle.full_output,
                operation=handle.operation,
            )
        if proposal.memory_entry_id:
            self.memory.update_history_event(
                proposal.memory_entry_id,
                content={"communication": proposal.communication or None},
                metadata={"standard_stream_completed": True},
            )
            _commit_proposal_communication(
                proposal,
                memory=self.memory,
                agent_id=self.agent_id,
                config=self.config,
                broadcast=self.broadcast,
                event_sink=self.event_sink,
            )

    def _apply_plan_tail_failed(self, handle: PlanHandle, result: PlanResult) -> None:
        handle.terminal = True
        handle.error = result.feedback
        if handle.detached and handle.proposal is not None:
            self._record_detached_plan(
                handle.proposal,
                previous=handle.previous_action,
                handle=handle,
                reason="race loser tail failed",
            )
            self._record_standard_action(handle, handle.proposal)

    def _apply_plan_failed(self, handle: PlanHandle, result: PlanResult) -> None:
        handle.terminal = True
        handle.error = result.feedback or "普通规划失败"
        if handle.action_ready:
            self._apply_plan_tail_failed(handle, result)
            return
        if handle.detached or not self.coordinator.is_race_open(handle.race_id):
            return
        self.consecutive_errors += 1
        self.last_plan_error = handle.error
        if handle.attempt < self.config.maximum_replans and not self._stop_event.is_set():
            feedbacks = (*handle.feedbacks, handle.error)
            if self.event_sink is not None:
                self.event_sink(
                    "planning.replanned",
                    agent_id=self.agent_id,
                    attempt=handle.attempt + 1,
                    feedback=handle.error,
                )
            self._submit_race_standard_plan(
                initial_feedbacks=feedbacks,
                attempt=handle.attempt + 1,
            )
            return
        self.coordinator.end_race_if_open()
        self.last_plan_error = None
        self.coordinator.force_dual_next = True
        self._race_failed_this_iteration = True
        if self.event_sink is not None:
            self.event_sink("race.failed", agent_id=self.agent_id)

    def _handle_action_finished(self) -> None:
        if not self.coordinator.action_finished.is_set():
            return
        self.coordinator.action_finished.clear()
        now = self.clock.monotonic()
        self._next_plan_at = now + self.config.planning_interval
        if not self.config.cache_planning_enabled:
            self.coordinator.force_dual_next = False
            self.coordinator.end_race_if_open()
            self._run_single_standard_plan(feedbacks=())
            return
        if not self.config.standard_verification_enabled:
            self.coordinator.force_dual_next = False
            self.coordinator.end_race_if_open()
            self.coordinator.request_cache_race()
            return
        initial_feedbacks: tuple[str, ...] = ()
        if not self.coordinator.buffer_has_candidate:
            reason = self.coordinator.consume_cache_plan_failure()
            if reason is not None:
                initial_feedbacks = (reason,)
        if self.coordinator.force_dual_next:
            # 上一轮重规划失败：忽略缓冲候选，强制从双路规划开始
            self.coordinator.force_dual_next = False
            self.coordinator.begin_race()
            self.coordinator.request_cache_race()
            if not self._planning_in_flight:
                self._start_standard_plan(initial_feedbacks=initial_feedbacks)
            if self.event_sink is not None:
                self.event_sink("race.forced_dual", agent_id=self.agent_id)
            return
        if self.coordinator.buffer_has_candidate:
            # 情形 A：定时规划已完成，直接采用缓冲结果，不启动竞速
            return
        # 情形 B/C：进入双路竞速
        self.coordinator.begin_race()
        self.coordinator.request_cache_race()
        if not self._planning_in_flight:
            self._start_standard_plan(initial_feedbacks=initial_feedbacks)

    def _handle_cache_plan_failure(self) -> bool:
        if self._planning_in_flight:
            return False
        if (
            self.config.standard_verification_enabled
            and (
                self.coordinator.action_active
                or self.coordinator.buffer_has_candidate
            )
        ):
            # 已有动作或候选时不能并发发布回退动作；保留失败事件，
            # 在当前动作完成后进入下一轮双路竞速。
            return False
        reason = self.coordinator.consume_cache_plan_failure()
        if reason is None:
            return False
        if self.config.standard_verification_enabled:
            if not self.coordinator.race_active:
                self.coordinator.begin_race()
                self.coordinator.request_cache_race()
            self._start_standard_plan(
                initial_feedbacks=(reason,),
            )
            return True
        self.coordinator.end_race_if_open()
        self._run_single_standard_plan(
            feedbacks=(reason,),
            failed_event="planning.fallback_failed",
        )
        return True

    def _maybe_run_initial_standard_plan(self) -> bool:
        if self._initial_standard_plan_started:
            return False
        if self._planning_in_flight:
            return True
        if self.coordinator.action_active or self.coordinator.last_action is not None:
            return False
        if (
            not self.config.standard_verification_enabled
            and self.config.cache_planning_enabled
        ):
            self._initial_standard_plan_started = True
            self._next_plan_at = (
                self.clock.monotonic() + self.config.planning_interval
            )
            self.coordinator.end_race_if_open()
            self.coordinator.request_cache_race()
            return True
        if self._run_single_standard_plan(feedbacks=()):
            self._initial_standard_plan_started = True
            self._next_plan_at = (
                self.clock.monotonic() + self.config.planning_interval
            )
        return True

    def _handle_forced_dual_restart(self) -> bool:
        if not self.config.standard_verification_enabled:
            return False
        if not self.config.cache_planning_enabled:
            self.coordinator.force_dual_next = False
            self.coordinator.end_race_if_open()
            if not self.coordinator.action_active and not self.coordinator.buffer_has_candidate:
                self._run_single_standard_plan(feedbacks=())
            return True
        if self._race_failed_this_iteration:
            return False
        if not self.coordinator.force_dual_next:
            return False
        if self._planning_in_flight:
            return False
        if self.coordinator.action_active or self.coordinator.buffer_has_candidate:
            return False
        self.coordinator.force_dual_next = False
        self.coordinator.begin_race()
        self.coordinator.request_cache_race()
        self._start_standard_plan()
        if self.event_sink is not None:
            self.event_sink("race.forced_dual", agent_id=self.agent_id)
        return True

    def _start_standard_plan(
        self,
        *,
        initial_feedbacks: Sequence[str] = (),
    ) -> None:
        if self.coordinator.race_active and self.config.cache_planning_enabled:
            self._run_race_standard_plan(initial_feedbacks=initial_feedbacks)
            return
        self._planning_in_flight = True
        try:
            self.coordinator.end_race_if_open()
            self._run_plan_once(
                operation="timed_standard_action",
                cache_candidates=(),
                feedbacks=initial_feedbacks,
            )
        finally:
            self._planning_in_flight = False

    def _run_single_standard_plan(
        self,
        *,
        feedbacks: Sequence[str],
        failed_event: str | None = None,
    ) -> bool:
        self._planning_in_flight = True
        try:
            proposal, feedback = self._run_plan_once(
                operation="standard_action",
                cache_candidates=(),
                feedbacks=feedbacks,
            )
            if proposal is None and failed_event and self.event_sink is not None:
                self.event_sink(
                    failed_event,
                    agent_id=self.agent_id,
                    feedback=feedback,
                )
            return proposal is not None
        finally:
            self._planning_in_flight = False

    def _run_race_standard_plan(
        self,
        *,
        initial_feedbacks: Sequence[str] = (),
    ) -> None:
        """提交后台普通规划；主 worker 只处理动作就绪和终态事件。"""
        feedbacks = tuple(
            str(feedback).strip()
            for feedback in initial_feedbacks
            if str(feedback).strip()
        )
        self._submit_race_standard_plan(
            initial_feedbacks=feedbacks,
            attempt=0,
        )
        self._settle_immediate_plan_results()

    def _settle_immediate_plan_results(self) -> None:
        """给已立即可用的结果一个短暂交接窗口，不等待慢模型输出。"""
        deadline = time.monotonic() + 0.02
        while time.monotonic() < deadline:
            self._drain_plan_results()
            if not any(
                not handle.detached
                and not handle.action_ready
                and not handle.terminal
                for handle in self._plan_handles.values()
            ):
                return
            time.sleep(0.001)

    def _submit_race_standard_plan(
        self,
        *,
        initial_feedbacks: Sequence[str],
        attempt: int,
    ) -> None:
        if self._stop_event.is_set():
            return
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        if self.budget is not None and not self.budget.try_acquire(self.agent_id):
            self._fail_race_without_request(
                "LLM 调用预算已用尽",
                initial_feedbacks=initial_feedbacks,
                attempt=attempt,
            )
            return
        self.coordinator.ensure_history(self.memory)
        request = build_planning_request(
            prompt_builder=self.prompt_builder,
            memory=self.memory,
            spec=self.spec,
            registry=self.registry,
            instructions=self.instructions,
            config=self.config,
            agent_id=self.agent_id,
            teammates=self.teammates,
            operation="standard_action",
            cache_candidates=(),
            last_error=self.last_plan_error,
            feedbacks=initial_feedbacks,
            trade_recipe_history=self.trade_recipe_history,
        )
        self._plan_epoch += 1
        handle = PlanHandle(
            request_id=request.request_id,
            plan_id=None,
            race_id=self.coordinator.active_race_id,
            epoch=self._plan_epoch,
            operation="standard_action",
            previous_action=self.coordinator.last_action or Action(name="START"),
            feedbacks=tuple(initial_feedbacks),
            attempt=attempt,
        )
        self._plan_handles[handle.epoch] = handle
        thread = threading.Thread(
            target=self._run_background_standard_plan,
            args=(handle, request),
            name=f"coap-standard-plan-{self.agent_id}-{handle.epoch}",
            daemon=True,
        )
        handle.thread = thread
        with self._background_lock:
            self._background_threads.add(thread)
        thread.start()
        self._refresh_planning_in_flight()

    def _fail_race_without_request(
        self,
        reason: str,
        *,
        initial_feedbacks: Sequence[str],
        attempt: int,
    ) -> None:
        if attempt < self.config.maximum_replans and not self._stop_event.is_set():
            self._submit_race_standard_plan(
                initial_feedbacks=(*initial_feedbacks, reason),
                attempt=attempt + 1,
            )
            return
        self.coordinator.end_race_if_open()
        self.coordinator.force_dual_next = True
        self._race_failed_this_iteration = True
        if self.event_sink is not None:
            self.event_sink("race.failed", agent_id=self.agent_id)

    def _run_background_standard_plan(
        self,
        handle: PlanHandle,
        request: LLMRequest,
    ) -> None:
        try:
            proposal, feedback = self._invoke_plan(
                request,
                operation=handle.operation,
                cache_candidates=(),
                race_id=handle.race_id,
                event_callback=lambda kind, item, error, output: self._queue_plan_result(
                    handle,
                    kind,
                    item,
                    error,
                    output,
                ),
            )
            if proposal is None:
                self._queue_plan_result(
                    handle,
                    "failed",
                    None,
                    feedback or "普通规划失败",
                    "",
                )
        except Exception as exc:  # noqa: BLE001
            self._queue_plan_result(
                handle,
                "tail_failed" if handle.action_emitted.is_set() else "failed",
                handle.proposal,
                str(exc),
                handle.full_output,
            )
        finally:
            with self._background_lock:
                if handle.thread is not None:
                    self._background_threads.discard(handle.thread)

    def _queue_plan_result(
        self,
        handle: PlanHandle,
        kind: str,
        proposal: COAPProposal | None,
        feedback: str | None,
        full_output: str,
    ) -> None:
        if kind == "action_ready":
            handle.action_emitted.set()
            handle.proposal = proposal
            handle.plan_id = proposal.plan_id if proposal is not None else None
            handle.full_output = full_output or handle.full_output
        self._plan_results.put(
            PlanResult(
                handle=handle,
                kind=kind,
                proposal=proposal,
                feedback=feedback,
                full_output=full_output,
            )
        )

    def _record_standard_action(
        self,
        handle: PlanHandle,
        proposal: COAPProposal,
    ) -> None:
        self.coordinator.record_race_action(
            handle.race_id,
            "standard",
            action_unit_to_action(proposal.action_unit),
        )

    def _record_detached_plan(
        self,
        proposal: COAPProposal,
        *,
        previous: Action,
        handle: PlanHandle | None = None,
        reason: str,
    ) -> None:
        cache_action = self.coordinator.race_action(proposal.race_id, "cache")
        standard_action = action_unit_to_action(proposal.action_unit)
        comparison = {
            "cache_action": _action_to_dict(cache_action),
            "standard_action": _action_to_dict(standard_action),
            "same_action_name": bool(
                cache_action is not None
                and cache_action.name == standard_action.name
            ),
            "important_parameters_match": False,
        }
        if cache_action is not None:
            try:
                comparison["important_parameters_match"] = (
                    self._consistency.equivalent(cache_action, standard_action)
                )
            except Exception:  # noqa: BLE001
                comparison["important_parameters_match"] = False
        if handle is not None and handle.detached_recorded:
            if proposal.memory_entry_id:
                self._update_detached_history(handle)
            elif handle.terminal:
                self._append_detached_history(
                    proposal,
                    handle=handle,
                    comparison=comparison,
                    reason=reason,
                )
            return
        if handle is None or handle.terminal:
            self._append_detached_history(
                proposal,
                handle=handle,
                comparison=comparison,
                reason=reason,
            )
        self._record_uncertain(
            proposal,
            previous=previous,
            handle=handle,
            comparison=comparison,
        )
        if handle is not None:
            handle.detached_recorded = True

    def _append_detached_history(
        self,
        proposal: COAPProposal,
        *,
        handle: PlanHandle | None,
        comparison: dict[str, Any],
        reason: str,
    ) -> None:
        entry = self.memory.append_history_event(
            agent_id=self.agent_id,
            source="self",
            event_type="planning",
            content={
                "coap_record": True,
                "standard_plan": _proposal_history_action(
                    proposal,
                    self.instructions,
                ),
                "selected_action": None,
                "reasoning": proposal.reasoning or None,
                "communication": proposal.communication or None,
                "planning_output": (
                    handle.full_output if handle is not None else None
                ),
                "result_description": "竞速败方，未执行",
                "result_status": "uncertain",
                "result_data": None,
            },
            metadata={
                "method": "coap",
                "race_id": proposal.race_id,
                "plan_id": proposal.plan_id,
                "request_id": proposal.request_id,
                "detached": True,
                "detached_reason": reason,
                "standard_stream_completed": (
                    handle.stream_completed if handle is not None else False
                ),
                "cache_candidate_ids": list(
                    self.coordinator.cache_candidate_ids(proposal.race_id)
                ),
                "race_comparison": comparison,
            },
        )
        proposal.memory_entry_id = entry.entry_id

    def _update_detached_history(self, handle: PlanHandle) -> None:
        proposal = handle.proposal
        if proposal is None or not proposal.memory_entry_id:
            return
        self.memory.update_history_event(
            proposal.memory_entry_id,
            content={
                "communication": proposal.communication or None,
                "planning_output": handle.full_output or None,
            },
            metadata={
                "detached": True,
                "standard_stream_completed": handle.stream_completed,
            },
        )

    def _run_plan_once(
        self,
        *,
        operation: str,
        cache_candidates: Sequence[Any],
        feedbacks: Sequence[str],
    ) -> tuple[COAPProposal | None, str | None]:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return None, "agent is not live"
        if self.budget is not None and not self.budget.try_acquire(self.agent_id):
            return None, "LLM 调用预算已用尽"
        try:
            self.coordinator.ensure_history(self.memory)
            if cache_candidates:
                self.coordinator.update_history(
                    self.memory,
                    content={},
                    metadata={
                        "cache_candidates": [
                            _history_cache_candidate(item)
                            for item in cache_candidates
                        ],
                    },
                )
            request = build_planning_request(
                prompt_builder=self.prompt_builder,
                memory=self.memory,
                spec=self.spec,
                registry=self.registry,
                instructions=self.instructions,
                config=self.config,
                agent_id=self.agent_id,
                teammates=self.teammates,
                operation=operation,
                cache_candidates=cache_candidates,
                last_error=self.last_plan_error,
                feedbacks=feedbacks,
                trade_recipe_history=self.trade_recipe_history,
            )
            race_id = self.coordinator.active_race_id
            proposal, feedback = self._invoke_plan(
                request,
                operation=operation,
                cache_candidates=cache_candidates,
                race_id=race_id,
            )
            if self.liveness_gate is not None and not self.liveness_gate.is_live(
                self.agent_id
            ):
                return None, "agent is not live"
            if proposal is None or self._stop_event.is_set():
                return None, feedback
            self.planned_count += 1
            self._publish(proposal)
            self.consecutive_errors = 0
            self.last_plan_error = None
            return proposal, None
        except Exception as exc:  # noqa: BLE001
            self.consecutive_errors += 1
            self.last_plan_error = str(exc)
            self.coordinator.update_history(
                self.memory,
                content={},
                metadata={"standard_result": "failed", "standard_error": str(exc)},
            )
            if self.event_sink is not None:
                self.event_sink(
                    "planning.failed",
                    agent_id=self.agent_id,
                    operation=operation,
                    consecutive_errors=self.consecutive_errors,
                    error=str(exc),
                )
            if self.consecutive_errors >= self.config.max_consecutive_errors:
                raise
            return None, str(exc)

    def _invoke_plan(
        self,
        request: LLMRequest,
        *,
        operation: str,
        cache_candidates: Sequence[Any],
        race_id: str | None,
        event_callback: Callable[[str, COAPProposal | None, str | None, str], None]
        | None = None,
    ) -> tuple[COAPProposal | None, str | None]:
        timeout = (
            self.config.standard_action_timeout_seconds
            if operation in {"standard_action", "timed_standard_action"}
            else self.config.cache_action_timeout_seconds
        )
        stream = getattr(self.llm_client, "stream", None)
        if callable(stream):
            return self._invoke_stream(
                request,
                operation=operation,
                cache_candidates=cache_candidates,
                timeout=timeout,
                race_id=race_id,
                event_callback=event_callback,
            )
        start = self.clock.monotonic()
        wall_start = self.clock.now()
        try:
            response = _complete_with_stop_event(
                self.llm_client,
                request,
                self._stop_event,
            )
        except LLMRequestCancelled:
            return None, None
        if self._stop_event.is_set():
            return None, None
        elapsed = self.clock.monotonic() - start
        if elapsed > timeout:
            if self.event_sink is not None:
                self.event_sink(
                    "planning.timeout",
                    agent_id=self.agent_id,
                    operation=operation,
                    elapsed=elapsed,
                )
            return None, f"规划超时（{timeout:.0f}s）"
        try:
            proposal = self.codec.parse_complete(
                response.content,
                agent_id=self.agent_id,
                operation=operation,
                cache_candidates=cache_candidates,
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"输出解析失败：{exc}"
        proposal.request_id = getattr(response, "request_id", None)
        proposal.planning_started_at = wall_start
        proposal.planning_finished_at = self.clock.now()
        proposal.race_id = race_id
        if event_callback is not None:
            event_callback("action_ready", proposal, None, response.content)
            event_callback("completed", proposal, None, response.content)
        return proposal, None

    def _invoke_stream(
        self,
        request: LLMRequest,
        *,
        operation: str,
        cache_candidates: Sequence[Any],
        timeout: float,
        race_id: str | None,
        event_callback: Callable[[str, COAPProposal | None, str | None, str], None]
        | None = None,
    ) -> tuple[COAPProposal | None, str | None]:
        trigger = StreamTrigger(
            name="action_ready",
            keyword=self.config.action_ready_keyword,
        )
        proposal: COAPProposal | None = None
        start = self.clock.monotonic()
        wall_start = self.clock.now()
        iterator = iter(
            self.llm_client.stream(
                request,
                triggers=[trigger],
                stop_event=self._stop_event,
            )
        )
        for event in iterator:
            if self._stop_event.is_set():
                break
            if self.clock.monotonic() - start > timeout:
                if self.event_sink is not None:
                    self.event_sink(
                        "planning.timeout",
                        agent_id=self.agent_id,
                        operation=operation,
                        elapsed=self.clock.monotonic() - start,
                    )
                _close_stream_iterator(iterator)
                return None, f"规划超时（{timeout:.0f}s）"
            if (
                event.type is StreamEventType.KEYWORD_REACHED
                and proposal is None
                and event.trigger is not None
            ):
                try:
                    proposal = self.codec.parse_streamed_action(
                        event.trigger.segment,
                        agent_id=self.agent_id,
                        operation=operation,
                        cache_candidates=cache_candidates,
                    )
                    proposal.request_id = event.request_id
                    proposal.planning_started_at = wall_start
                    proposal.planning_finished_at = getattr(
                        event.trigger,
                        "triggered_at",
                        self.clock.now(),
                    )
                    proposal.race_id = race_id
                    if event_callback is not None:
                        event_callback("action_ready", proposal, None, "")
                        continue
                    _drain_stream_in_background(
                        iterator,
                        name=f"coap-stream-tail-{self.agent_id}",
                        on_event=lambda tail_event: self._finish_stream_tail(
                            proposal,
                            tail_event,
                            operation=operation,
                        ),
                    )
                    return proposal, None
                except Exception as exc:  # noqa: BLE001
                    _close_stream_iterator(iterator)
                    return None, f"输出解析失败：{exc}"
            if event.type in (
                StreamEventType.COMPLETED,
                StreamEventType.FAILED,
                StreamEventType.STOPPED,
            ):
                if proposal is None and event.response is not None:
                    try:
                        proposal = self.codec.parse_complete(
                            event.response.content,
                            agent_id=self.agent_id,
                            operation=operation,
                            cache_candidates=cache_candidates,
                        )
                        proposal.request_id = event.response.request_id
                        proposal.planning_started_at = wall_start
                        proposal.race_id = race_id
                    except Exception as exc:  # noqa: BLE001
                        _close_stream_iterator(iterator)
                        return None, f"输出解析失败：{exc}"
                elif proposal is not None and event.response is not None:
                    self._fill_communication(
                        proposal,
                        event.response.content,
                        operation=operation,
                    )
                    if operation == "timed_standard_action":
                        try:
                            full = parse_action_response(
                                event.response.content,
                                operation=operation,
                            )
                            proposal.interrupt = full.interrupt
                            proposal.publish_to_buffer = should_publish_to_buffer(
                                operation,
                                proposal.interrupt,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                if proposal is not None and proposal.planning_finished_at is None:
                    proposal.planning_finished_at = self.clock.now()
                if proposal is not None:
                    proposal.stream_completed = True
                    if event_callback is not None:
                        event_callback(
                            "completed",
                            proposal,
                            getattr(event, "error", None),
                            (
                                event.response.content
                                if event.response is not None
                                else ""
                            ),
                        )
                break
        _close_stream_iterator(iterator)
        return proposal, None

    def _finish_stream_tail(
        self,
        proposal: COAPProposal,
        event: Any,
        *,
        operation: str,
    ) -> None:
        if event.type not in _STREAM_TERMINAL_EVENTS:
            return
        proposal.stream_completed = True
        if proposal.execution_finished or proposal.discard_stream_tail:
            return
        if event.response is None:
            return
        self._fill_communication(
            proposal,
            event.response.content,
            operation=operation,
        )
        if operation == "timed_standard_action":
            try:
                full = parse_action_response(
                    event.response.content,
                    operation=operation,
                )
                proposal.interrupt = full.interrupt
                proposal.publish_to_buffer = should_publish_to_buffer(
                    operation,
                    proposal.interrupt,
                )
            except Exception:  # noqa: BLE001
                pass

    def _fill_communication(
        self,
        proposal: COAPProposal,
        content: str,
        *,
        operation: str,
    ) -> None:
        try:
            parsed = parse_action_response(content, operation=operation)
            proposal.communication = parsed.communication
        except Exception:  # noqa: BLE001
            return

    def _publish(
        self,
        proposal: COAPProposal,
        *,
        handle: PlanHandle | None = None,
    ) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        if proposal.race_id is not None and not self.coordinator.is_race_open(
            proposal.race_id
        ):
            if handle is not None:
                handle.detached = True
            self._record_detached_plan(
                proposal,
                previous=(
                    handle.previous_action
                    if handle is not None
                    else self.coordinator.last_action or Action(name="START")
                ),
                handle=handle,
                reason="race already closed",
            )
            return
        if proposal.publish_to_buffer:
            self.coordinator.record_race_action(
                proposal.race_id,
                "standard",
                action_unit_to_action(proposal.action_unit),
            )
        if proposal.publish_to_buffer and not self.coordinator.try_claim("standard"):
            if handle is not None:
                handle.detached = True
            self._record_detached_plan(
                proposal,
                previous=(
                    handle.previous_action
                    if handle is not None
                    else self.coordinator.last_action or Action(name="START")
                ),
                handle=handle,
                reason="cache path won race",
            )
            return
        if proposal.publish_to_buffer:
            self.coordinator.mark_race_winner("standard", proposal.race_id)
        # 非强制中断的周期性规划只记录历史，不进入缓冲，也不触发 cache 竞速。
        entry_id = self.coordinator.ensure_history(self.memory)
        self.memory.update_history_event(
            entry_id,
            content={
                "standard_plan": _proposal_history_action(
                    proposal,
                    self.instructions,
                ),
                "reasoning": proposal.reasoning or None,
            },
            metadata={
                "standard_plan_id": proposal.plan_id,
                "standard_request_id": proposal.request_id,
                "standard_path": proposal.path,
            },
        )
        proposal.memory_entry_id = entry_id
        if proposal.publish_to_buffer:
            self.memory.update_history_event(
                entry_id,
                content={
                    "selected_action": _proposal_history_action(
                        proposal,
                        self.instructions,
                    ),
                    "selected_source": "standard",
                },
            )
            # Mark the candidate before publishing it. The acting worker may
            # consume the buffer immediately after put() returns.
            self.coordinator.mark_candidate_written()
            self.buffer.put(proposal)
            _commit_proposal_communication(
                proposal,
                memory=self.memory,
                agent_id=self.agent_id,
                config=self.config,
                broadcast=self.broadcast,
                event_sink=self.event_sink,
            )
            if proposal.interrupt:
                self.interrupt_event.set()
        if self.event_sink is not None:
            self.event_sink(
                "proposal.created",
                agent_id=self.agent_id,
                plan_id=proposal.plan_id,
                request_id=proposal.request_id,
                path=proposal.path,
                action=proposal.action_unit.action,
                interrupt=proposal.interrupt,
            )

    def _record_uncertain(
        self,
        proposal: COAPProposal,
        *,
        previous: Action | None = None,
        handle: PlanHandle | None = None,
        comparison: dict[str, Any] | None = None,
    ) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        previous = previous or self.coordinator.last_action or Action(name="START")
        if (
            previous.name
            == self.coordinator.cache_service.retrieval_config.start_action_name
        ):
            return
        record = CandidateRecord(
            decision_id=f"{self.agent_id}:{proposal.plan_id}",
            agent_id=self.agent_id,
            previous_action=previous,
            next_action=action_unit_to_action(proposal.action_unit),
            reasoning=proposal.reasoning or "dual-path loser",
            status=CandidateStatus.UNCERTAIN,
            plan_id=proposal.plan_id,
            created_at=self.clock.now(),
            metadata={
                "detached": True,
                "race_id": proposal.race_id,
                "request_id": proposal.request_id,
                "cache_candidate_ids": list(
                    self.coordinator.cache_candidate_ids(proposal.race_id)
                ),
                "race_comparison": comparison or {},
            },
        )
        self.coordinator.cache_service.buffer.add(record)
        if self.event_sink is not None:
            self.event_sink(
                "cache_candidate.uncertain",
                agent_id=self.agent_id,
                plan_id=proposal.plan_id,
                path="standard",
                detached=True,
            )


class COAPCachePlanningWorker(LoopWorker):
    """线路③：双路竞速中的缓存动作规划。"""

    def __init__(
        self,
        *,
        agent: Any,
        llm_client: Any,
        prompt_builder: COAPPromptBuilder,
        codec: COAPDecisionCodec,
        buffer: SingleSlotOverwriteBuffer,
        interrupt_event: threading.Event,
        memory: TeamMemory,
        config: COAPConfig,
        coordinator: COAPDecisionCoordinator,
        registry: Any,
        spec: Any,
        instructions: Any | None = None,
        budget: Any | None = None,
        broadcast: Callable[[str], None] | None = None,
        event_sink: EventSink | None = None,
        clock: Any | None = None,
        trade_recipe_history: Any | None = None,
        liveness_gate: EntityLivenessGate | None = None,
    ) -> None:
        agent_id = str(agent.username)
        super().__init__(
            name=f"coap-cache-planning-{agent_id}",
            loop_interval=0.05,
        )
        self.agent = agent
        self.agent_id = agent_id
        self.llm_client = llm_client
        self.prompt_builder = prompt_builder
        self.codec = codec
        self.buffer = buffer
        self.interrupt_event = interrupt_event
        self.memory = memory
        self.config = config
        self.coordinator = coordinator
        self.registry = registry
        self.spec = spec
        self.instructions = instructions
        self.budget = budget
        self.broadcast = broadcast
        self.event_sink = event_sink
        self.clock = clock or SystemClock()
        self.trade_recipe_history = trade_recipe_history
        self.liveness_gate = liveness_gate
        self.planned_count = 0
        self.retrieval_count = 0
        self.cache_miss_count = 0
        self.planning_call_count = 0
        self.planning_success_count = 0
        self.planning_failure_count = 0
        self.budget_exhausted_count = 0
        self.last_plan_error: str | None = None

    def iterate(self) -> None:
        if not self.config.cache_planning_enabled:
            return
        if not self.coordinator.cache_race_event.is_set():
            return
        self.coordinator.cache_race_event.clear()
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            self.coordinator.on_agent_not_live()
            return
        if self.memory.get_observation(self.agent_id) is None:
            return
        self._run_cache_plan()

    def _run_cache_plan(self) -> None:
        race_id = self.coordinator.cache_race_id
        self.coordinator.ensure_history(self.memory)
        previous = self.coordinator.last_action
        if previous is None:
            previous = Action(name="START")
        self.retrieval_count += 1
        lookup = self.coordinator.cache_service.retrieve(
            previous,
            task_scope=self.coordinator.task_scope,
            agent_id=self.agent_id,
        )
        self.coordinator.record_cache_candidates(
            race_id,
            [entry.entry_id for entry in lookup.entries],
        )
        self.coordinator.update_history(
            self.memory,
            content={},
            metadata={
                "cache_previous_action": previous.name,
                "cache_matched_count": lookup.matched_count,
                "cache_candidate_count": len(lookup.entries),
                "cache_filtered_count": lookup.filtered_count,
                "cache_candidates": [
                    _history_cache_candidate(item) for item in lookup.entries
                ],
            },
        )
        if self.event_sink is not None:
            self.event_sink(
                "cache_retrieval.completed",
                agent_id=self.agent_id,
                previous_action=previous.name,
                matched_count=lookup.matched_count,
                candidate_count=len(lookup.entries),
                filtered_count=lookup.filtered_count,
            )
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        if not lookup.entries:
            self.cache_miss_count += 1
            if self.event_sink is not None:
                self.event_sink(
                    "cache_retrieval.empty",
                    agent_id=self.agent_id,
                    previous_action=previous.name,
                    matched_count=lookup.matched_count,
                    filtered_count=lookup.filtered_count,
                )
            self._record_cache_failure(
                "no cache candidates",
                emit_when_verifying=False,
            )
            return
        score_updater = getattr(self.coordinator.cache_service, "score_updater", None)
        if score_updater is not None:
            score_updater.record_hits(
                [entry.entry_id for entry in lookup.entries]
            )
        if self.budget is not None and not self.budget.try_acquire(self.agent_id):
            self.budget_exhausted_count += 1
            if self.event_sink is not None:
                self.event_sink(
                    "cache_planning.budget_exhausted",
                    agent_id=self.agent_id,
                )
            self._record_cache_failure(
                "LLM 调用预算已用尽",
                emit_when_verifying=False,
            )
            return
        try:
            self.planning_call_count += 1
            if self.event_sink is not None:
                self.event_sink(
                    "cache_planning.started",
                    agent_id=self.agent_id,
                    previous_action=previous.name,
                    candidate_count=len(lookup.entries),
                )
            request = build_planning_request(
                prompt_builder=self.prompt_builder,
                memory=self.memory,
                spec=self.spec,
                registry=self.registry,
                instructions=self.instructions,
                config=self.config,
                agent_id=self.agent_id,
                teammates=(),
                operation="cache_action",
                cache_candidates=lookup.entries,
                last_error=self.last_plan_error,
                trade_recipe_history=self.trade_recipe_history,
            )
            proposal, feedback = self._invoke_plan(
                request,
                operation="cache_action",
                cache_candidates=lookup.entries,
                race_id=race_id,
            )
            if self.liveness_gate is not None and not self.liveness_gate.is_live(
                self.agent_id
            ):
                return
            if proposal is None:
                self._record_cache_failure(
                    feedback or "cache planning failed",
                    planning_attempted=True,
                )
                return
            if self._stop_event.is_set():
                return
            self.planned_count += 1
            self.planning_success_count += 1
            self._publish(proposal)
            self.last_plan_error = None
            if self.event_sink is not None:
                self.event_sink(
                    "cache_planning.completed",
                    agent_id=self.agent_id,
                    plan_id=proposal.plan_id,
                )
        except Exception as exc:  # noqa: BLE001
            self.last_plan_error = str(exc)
            self._record_cache_failure(str(exc), planning_attempted=True)

    def _record_cache_failure(
        self,
        reason: str,
        *,
        emit_when_verifying: bool = True,
        planning_attempted: bool = False,
    ) -> None:
        if planning_attempted:
            self.planning_failure_count += 1
        if planning_attempted or not self.config.standard_verification_enabled:
            self.coordinator.mark_cache_plan_failed(reason)
            self.coordinator.mark_cache_plan_failed(reason)
        self.coordinator.update_history(
            self.memory,
            content={},
            metadata={"cache_result": "failed", "cache_error": reason},
        )
        if (
            self.event_sink is not None
            and (emit_when_verifying or not self.config.standard_verification_enabled)
        ):
            self.event_sink(
                "cache_planning.failed",
                agent_id=self.agent_id,
                error=reason,
            )

    def _invoke_plan(
        self,
        request: LLMRequest,
        *,
        operation: str,
        cache_candidates: Sequence[Any],
        race_id: str | None,
    ) -> tuple[COAPProposal | None, str | None]:
        timeout = self.config.cache_action_timeout_seconds
        stream = getattr(self.llm_client, "stream", None)
        if callable(stream):
            trigger = StreamTrigger(
                name="action_ready",
                keyword=self.config.action_ready_keyword,
            )
            proposal: COAPProposal | None = None
            start = self.clock.monotonic()
            wall_start = self.clock.now()
            iterator = iter(
                self.llm_client.stream(
                    request,
                    triggers=[trigger],
                    stop_event=self._stop_event,
                )
            )
            for event in iterator:
                if self._stop_event.is_set():
                    break
                if self.clock.monotonic() - start > timeout:
                    _close_stream_iterator(iterator)
                    return None, f"缓存规划超时（{timeout:.0f}s）"
                if (
                    event.type is StreamEventType.KEYWORD_REACHED
                    and proposal is None
                    and event.trigger is not None
                ):
                    try:
                        proposal = self.codec.parse_streamed_action(
                            event.trigger.segment,
                            agent_id=self.agent_id,
                            operation=operation,
                            cache_candidates=cache_candidates,
                        )
                        proposal.request_id = event.request_id
                        proposal.planning_started_at = wall_start
                        proposal.planning_finished_at = getattr(
                            event.trigger,
                            "triggered_at",
                            self.clock.now(),
                        )
                        proposal.race_id = race_id
                        _drain_stream_in_background(
                            iterator,
                            name=f"coap-cache-stream-tail-{self.agent_id}",
                            on_event=lambda tail_event: self._finish_stream_tail(
                                proposal,
                                tail_event,
                            ),
                        )
                        return proposal, None
                    except Exception as exc:  # noqa: BLE001
                        _close_stream_iterator(iterator)
                        return None, f"缓存输出解析失败：{exc}"
                if event.type in (
                    StreamEventType.COMPLETED,
                    StreamEventType.FAILED,
                    StreamEventType.STOPPED,
                ):
                    if proposal is None and event.response is not None:
                        try:
                            proposal = self.codec.parse_complete(
                                event.response.content,
                                agent_id=self.agent_id,
                                operation=operation,
                                cache_candidates=cache_candidates,
                            )
                            proposal.request_id = event.response.request_id
                            proposal.planning_started_at = wall_start
                            proposal.race_id = race_id
                        except Exception as exc:  # noqa: BLE001
                            _close_stream_iterator(iterator)
                            return None, f"缓存输出解析失败：{exc}"
                    elif proposal is not None and event.response is not None:
                        try:
                            parsed = parse_action_response(
                                event.response.content,
                                operation=operation,
                            )
                            proposal.communication = parsed.communication
                        except Exception:  # noqa: BLE001
                            pass
                    if proposal is not None and proposal.planning_finished_at is None:
                        proposal.planning_finished_at = self.clock.now()
                    break
            _close_stream_iterator(iterator)
            return proposal, None
        start = self.clock.monotonic()
        wall_start = self.clock.now()
        try:
            response = _complete_with_stop_event(
                self.llm_client,
                request,
                self._stop_event,
            )
        except LLMRequestCancelled:
            return None, None
        if self._stop_event.is_set():
            return None, None
        if self.clock.monotonic() - start > timeout:
            return None, f"缓存规划超时（{timeout:.0f}s）"
        try:
            proposal = self.codec.parse_complete(
                response.content,
                agent_id=self.agent_id,
                operation=operation,
                cache_candidates=cache_candidates,
            )
        except Exception as exc:  # noqa: BLE001
            return None, f"缓存输出解析失败：{exc}"
        proposal.request_id = getattr(response, "request_id", None)
        proposal.planning_started_at = wall_start
        proposal.planning_finished_at = self.clock.now()
        proposal.race_id = race_id
        return proposal, None

    def _finish_stream_tail(
        self,
        proposal: COAPProposal,
        event: Any,
    ) -> None:
        if event.type not in _STREAM_TERMINAL_EVENTS:
            return
        proposal.stream_completed = True
        if proposal.execution_finished or proposal.discard_stream_tail:
            return
        if event.response is None:
            return
        try:
            parsed = parse_action_response(
                event.response.content,
                operation="cache_action",
            )
            proposal.communication = parsed.communication
        except Exception:  # noqa: BLE001
            return

    def _publish(self, proposal: COAPProposal) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        if proposal.race_id is not None and not self.coordinator.is_race_open(
            proposal.race_id
        ):
            _discard_proposal_tail(proposal, self.memory)
            return
        self.coordinator.record_race_action(
            proposal.race_id,
            "cache",
            action_unit_to_action(proposal.action_unit),
        )
        entry_id = self.coordinator.ensure_history(self.memory)
        self.memory.update_history_event(
            entry_id,
            content={
                "cache_plan": _proposal_history_action(
                    proposal,
                    self.instructions,
                ),
            },
            metadata={
                "cache_plan_id": proposal.plan_id,
                "cache_request_id": proposal.request_id,
                "cache_entry_id": getattr(proposal, "cache_entry_id", None),
            },
        )
        proposal.memory_entry_id = entry_id
        if not self.coordinator.try_claim("cache"):
            _discard_proposal_tail(proposal, self.memory)
            self.memory.update_history_event(
                entry_id,
                metadata={"cache_result": "uncertain"},
            )
            if self.event_sink is not None:
                self.event_sink(
                    "cache_candidate.uncertain",
                    agent_id=self.agent_id,
                    plan_id=proposal.plan_id,
                    path="cache",
                )
            return
        self.coordinator.mark_race_winner("cache", proposal.race_id)
        self.memory.update_history_event(
            entry_id,
            content={
                "selected_action": _proposal_history_action(
                    proposal,
                    self.instructions,
                ),
                "selected_source": "cache",
            },
        )
        # Mark the candidate before publishing it. The acting worker may
        # consume the buffer immediately after put() returns.
        self.coordinator.mark_candidate_written()
        self.buffer.put(proposal)
        _commit_proposal_communication(
            proposal,
            memory=self.memory,
            agent_id=self.agent_id,
            config=self.config,
            broadcast=self.broadcast,
            event_sink=self.event_sink,
        )
        if self.event_sink is not None:
            self.event_sink(
                "proposal.created",
                agent_id=self.agent_id,
                plan_id=proposal.plan_id,
                request_id=proposal.request_id,
                path=proposal.path,
                action=proposal.action_unit.action,
                interrupt=False,
            )

    def _record_uncertain(self, proposal: COAPProposal) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        previous = self.coordinator.last_action or Action(name="START")
        if (
            previous.name
            == self.coordinator.cache_service.retrieval_config.start_action_name
        ):
            return
        record = CandidateRecord(
            decision_id=f"{self.agent_id}:{proposal.plan_id}",
            agent_id=self.agent_id,
            previous_action=previous,
            next_action=action_unit_to_action(proposal.action_unit),
            reasoning=proposal.reasoning or "dual-path loser",
            status=CandidateStatus.UNCERTAIN,
            plan_id=proposal.plan_id,
            created_at=self.clock.now(),
        )
        self.coordinator.cache_service.buffer.add(record)


def _render_action(action: str, params: dict[str, Any]) -> str:
    if not params:
        return action
    arguments = ", ".join(f"{name}={value!r}" for name, value in params.items())
    return f"{action}({arguments})"


def _history_cache_candidate(value: Any) -> Any:
    serializer = getattr(value, "to_dict", None)
    if callable(serializer):
        try:
            return serializer()
        except Exception:  # noqa: BLE001
            pass
    return str(value)


def _action_to_dict(action: Action | None) -> dict[str, Any] | None:
    if action is None:
        return None
    return {
        "name": action.name,
        "parameters": dict(action.parameters),
    }


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


__all__ = [
    "COAPActingWorker",
    "COAPCachePlanningWorker",
    "COAPCacheWorker",
    "COAPDecisionCoordinator",
    "COAPPlanningWorker",
]
