"""BasicAsync：最普通异步规划的 worker（无缓存/并行/定时规划）。"""

from __future__ import annotations

import time
from typing import Any, Callable, Sequence

from src.agents.action import ActionMode, ActionStatus
from src.agents.action.result_postprocessor import postprocess_action_result
from src.agents.action.task_defaults import apply_task_action_defaults
from src.memory import TeamMemory, action_text
from src.perception.attack_detection import is_successful_execution, tracker_for
from src.runtime import LoopWorker, SingleSlotOverwriteBuffer
from src.runtime.liveness import EntityLivenessGate

from src.baseline.COAP.adapters import broadcast_text
from src.tasks.trade import publish_trade_purchase_broadcast
from src.baseline.COAP.codec import COAPDecisionCodec
from src.baseline.COAP.schemas import COAPProposal

from .prompts import BasicAsyncPromptBuilder
from .request_builder import build_planning_request
from .schemas import BasicAsyncConfig


EventSink = Callable[..., None]


class BasicActingWorker(LoopWorker):
    """动作执行：取缓冲提案 → 执行/轮询 → 反馈广播 → 主动通信提交。"""

    def __init__(
        self,
        *,
        agent: Any,
        buffer: SingleSlotOverwriteBuffer,
        memory: TeamMemory,
        config: BasicAsyncConfig,
        instructions: Any | None = None,
        broadcast: Callable[[str], None] | None = None,
        event_sink: EventSink | None = None,
        execution_table: Any | None = None,
        call_file_resolver: Callable[[str | None], str | None] | None = None,
        plan_source: str = "standard",
        task_category: str | None = None,
        trade_action_observer: Callable[..., Any] | None = None,
        liveness_gate: EntityLivenessGate | None = None,
    ) -> None:
        agent_id = str(agent.username)
        super().__init__(
            name=f"basic-acting-{agent_id}",
            loop_interval=config.acting_poll_interval,
        )
        self.agent = agent
        self.agent_id = agent_id
        self.planner = agent.action_planner
        self.buffer = buffer
        self.memory = memory
        self.config = config
        self.instructions = instructions
        self.broadcast = broadcast
        self.event_sink = event_sink
        self.execution_table = execution_table
        self.call_file_resolver = call_file_resolver
        self.plan_source = plan_source
        self.task_category = task_category
        self.trade_action_observer = trade_action_observer
        self.liveness_gate = liveness_gate
        self.attack_detection = tracker_for(agent)
        self.seen_version = 0
        self.active_proposal: COAPProposal | None = None
        self.active_action_id: str | None = None
        self.executed_count = 0
        self.execution_started_at: float | None = None

    @property
    def is_busy(self) -> bool:
        return self.active_proposal is not None

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
        if not isinstance(proposal, COAPProposal):
            raise TypeError("BasicAsync 动作缓冲包含无效条目")
        self.seen_version = version
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
                self._finish_active(result, broadcast=False)
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
        if not self.active_action_id:
            raise RuntimeError("continuous action has no action id")
        result = self.planner.status(self.active_action_id)
        status = getattr(result, "status", None)
        if status in ActionStatus.TERMINAL:
            self._finish_active(result)

    def _dispatch(self, proposal: COAPProposal) -> None:
        apply_task_action_defaults(self.task_category, proposal)
        self.active_proposal = proposal
        self.execution_started_at = time.time()
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
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            self.active_proposal = None
            self.active_action_id = None
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
                    "selected_action": action_text(
                        proposal.action_unit.action,
                        proposal.input_params,
                        instructions=(
                            self.instructions if self.task_category == "trade" else None
                        ),
                        positional=proposal.input_positional,
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
        self._write_execution_row(
            proposal,
            str(result_status),
            result_error,
            processed,
        )
        self.active_proposal = None
        self.active_action_id = None

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
        if proposal.communication_committed:
            return
        text = (proposal.communication or "").strip()
        if not text:
            return
        try:
            if proposal.memory_entry_id:
                self.memory.update_history_event(
                    proposal.memory_entry_id,
                    content={"communication": text},
                )
        except Exception as exc:  # noqa: BLE001
            if self.event_sink is not None:
                self.event_sink(
                    "communication.commit_failed",
                    agent_id=self.agent_id,
                    error=str(exc),
                )
            return
        proposal.communication_committed = True
        if self.config.broadcast_results and callable(self.broadcast):
            try:
                self.broadcast(text)
            except Exception as exc:  # noqa: BLE001
                if self.event_sink is not None:
                    self.event_sink(
                        "communication.broadcast_failed",
                        agent_id=self.agent_id,
                        error=str(exc),
                    )

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
        description = processed.get("description")
        feedback = (
            description.strip()
            if isinstance(description, str) and description.strip()
            else f"状态 {result_status}"
            + (f"，错误 {result_error}" if result_error else "")
        )
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
                "interrupted_previous": False,
                "interrupted": result_status == "interrupted",
                "feedback": feedback,
                "plan_source": self.plan_source,
            }
        )


class BasicPlanningWorker(LoopWorker):
    """普通规划（非流式）：acting 空闲且缓冲无新提案时规划一次。"""

    def __init__(
        self,
        *,
        agent: Any,
        teammates: Sequence[str],
        llm_client: Any,
        prompt_builder: BasicAsyncPromptBuilder,
        codec: COAPDecisionCodec,
        buffer: SingleSlotOverwriteBuffer,
        memory: TeamMemory,
        config: BasicAsyncConfig,
        registry: Any,
        spec: Any,
        instructions: Any | None = None,
        acting_worker: BasicActingWorker,
        budget: Any | None = None,
        event_sink: EventSink | None = None,
        operation: str = "standard_action",
        path_label: str = "standard",
        liveness_gate: EntityLivenessGate | None = None,
        planning_start_gate: Callable[[str], None] | None = None,
        trade_recipe_history: Any | None = None,
    ) -> None:
        agent_id = str(agent.username)
        super().__init__(
            name=f"basic-planning-{agent_id}",
            loop_interval=0.1,
        )
        self.agent = agent
        self.agent_id = agent_id
        self.teammates = tuple(teammates)
        self.llm_client = llm_client
        self.prompt_builder = prompt_builder
        self.codec = codec
        self.buffer = buffer
        self.memory = memory
        self.config = config
        self.registry = registry
        self.spec = spec
        self.instructions = instructions
        self.acting_worker = acting_worker
        self.budget = budget
        self.event_sink = event_sink
        self.operation = operation
        self.path_label = path_label
        self.liveness_gate = liveness_gate
        self.planning_start_gate = planning_start_gate
        self.trade_recipe_history = trade_recipe_history
        self.planned_count = 0
        self.last_plan_error: str | None = None

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
        self._plan_once()

    def _plan_once(self) -> None:
        if self.budget is not None and not self.budget.try_acquire(self.agent_id):
            return
        try:
            request = build_planning_request(
                prompt_builder=self.prompt_builder,
                memory=self.memory,
                spec=self.spec,
                registry=self.registry,
                instructions=self.instructions,
                config=self.config,
                agent_id=self.agent_id,
                teammates=self.teammates,
                operation=self.operation,
                cache_candidates=(),
                last_error=self.last_plan_error,
                trade_recipe_history=self.trade_recipe_history,
            )
            if self.planning_start_gate is not None:
                self.planning_start_gate(self.agent_id)
            started = time.time()
            response = self.llm_client.complete(request)
            finished = time.time()
            if self._stop_event.is_set():
                return
            if self.liveness_gate is not None and not self.liveness_gate.is_live(
                self.agent_id
            ):
                return
            proposal = self.codec.parse_complete(
                response.content,
                agent_id=self.agent_id,
                operation=self.operation,
            )
            proposal.request_id = getattr(response, "request_id", None)
            proposal.planning_started_at = started
            proposal.planning_finished_at = finished
            self._publish(proposal)
            self.planned_count += 1
            self.last_plan_error = None
        except Exception as exc:  # noqa: BLE001
            self.last_plan_error = str(exc)
            if self.event_sink is not None:
                self.event_sink(
                    "planning.failed",
                    agent_id=self.agent_id,
                    error=str(exc),
                )

    def _publish(self, proposal: COAPProposal) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        content = {
            "selected_action": action_text(
                proposal.action_unit.action,
                proposal.input_params,
                instructions=(
                    self.instructions
                    if getattr(self.spec, "category", "") == "trade"
                    else None
                ),
                positional=proposal.input_positional,
            ),
            "communication": None,
            "result_description": "",
            "result_status": None,
            "result_data": None,
        }
        if self.path_label != "direct":
            content["standard_plan"] = action_text(
                proposal.action_unit.action,
                proposal.input_params,
                instructions=(
                    self.instructions
                    if getattr(self.spec, "category", "") == "trade"
                    else None
                ),
                positional=proposal.input_positional,
            )
            content["reasoning"] = proposal.reasoning or None
        entry = self.memory.append_history_event(
            agent_id=self.agent_id,
            source="self",
            event_type="planning",
            content=content,
            metadata={
                "plan_id": proposal.plan_id,
                "request_id": proposal.request_id,
                "path": self.path_label,
                "mode": proposal.action_unit.mode,
            },
        )
        proposal.memory_entry_id = entry.entry_id
        self.buffer.put(proposal)
        self.acting_worker._commit_communication(proposal)
        if self.event_sink is not None:
            self.event_sink(
                "proposal.created",
                agent_id=self.agent_id,
                plan_id=proposal.plan_id,
                request_id=proposal.request_id,
                action=proposal.action_unit.action,
            )


def _render_action(action: str, params: dict[str, Any]) -> str:
    if not params:
        return action
    arguments = ", ".join(f"{name}={value!r}" for name, value in params.items())
    return f"{action}({arguments})"


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


__all__ = ["BasicActingWorker", "BasicPlanningWorker"]
