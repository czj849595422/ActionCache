"""AgenticCache workers.

The runtime shape mirrors BasicAsync: observation, planning, and acting.  The
planning worker checks the transition cache before falling back to the LLM.
"""

from __future__ import annotations

import time
import threading
from typing import Any, Callable, Mapping, Sequence

from src.agents.action import ActionMode, ActionStatus
from src.agents.action.result_postprocessor import postprocess_action_result
from src.agents.action.task_defaults import apply_task_action_defaults
from src.memory import TeamMemory, action_text
from src.perception.attack_detection import tracker_for, with_detection_context
from src.runtime import LoopWorker, SingleSlotOverwriteBuffer
from src.runtime.liveness import EntityLivenessGate

from .budget import RunBudget
from .cache import AgenticCacheStore
from .codec import (
    AgenticCacheDecisionCodec,
    action_unit_to_plan,
)
from .metadata import current_state_metadata, metadata_from_observation
from .prompts import AgenticCachePromptBuilder, build_planning_request
from .schemas import ActionPlan, AgenticCacheConfig, AgenticCacheProposal
from .templates import binding_context_from_observation
from src.tasks.trade import publish_trade_purchase_broadcast


EventSink = Callable[..., None]


def _commit_proposal_communication(
    proposal: AgenticCacheProposal,
    *,
    memory: TeamMemory,
    agent_id: str,
    config: AgenticCacheConfig,
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


class ObservationWorker(LoopWorker):
    """Poll, filter, render, and store one agent observation."""

    def __init__(
        self,
        *,
        agent_id: str,
        perception: Any,
        verbalizer: Any,
        memory: TeamMemory,
        config: AgenticCacheConfig,
        rule_factory: Callable[[], Any] | None = None,
        liveness_gate: EntityLivenessGate | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        super().__init__(
            name=f"agentic-cache-observation-{agent_id}",
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
            cache_metadata = metadata_from_observation(
                observation,
                max_fields=self.config.max_metadata_fields,
            )
            binding_context = binding_context_from_observation(observation)
            first = not self.first_observation.is_set()
            self.memory.update_observation(
                self.agent_id,
                text or "本轮没有可报告的观测。",
                metadata={
                    "observed_at": time.time(),
                    "format": "text",
                    "agentic_cache_metadata": cache_metadata,
                    "agentic_cache_binding_context": binding_context,
                },
            )
            self.consecutive_errors = 0
            self.first_observation.set()
            if self.event_sink is not None:
                self.event_sink(
                    "observation.updated",
                    agent_id=self.agent_id,
                    first=first,
                    text_length=len(text or ""),
                    cache_metadata_count=len(cache_metadata),
                )
        except Exception as exc:  # noqa: BLE001
            self.consecutive_errors += 1
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


class AgenticCacheCoordinator:
    """Per-agent action history state shared by planning and acting."""

    def __init__(self, *, start_action_name: str = "START") -> None:
        import threading

        self.start_action_name = start_action_name
        self._lock = threading.RLock()
        self.last_action: ActionPlan | None = None
        self.action_index = 0

    def previous_action(self) -> ActionPlan:
        with self._lock:
            return self.last_action or ActionPlan(self.start_action_name)

    def current_index(self) -> int:
        with self._lock:
            return self.action_index

    def on_action_finished(self, action: ActionPlan) -> None:
        with self._lock:
            self.last_action = action
            self.action_index += 1


class AgenticCachePlanningWorker(LoopWorker):
    """Decide next action after the acting worker becomes idle."""

    def __init__(
        self,
        *,
        agent: Any,
        teammates: Sequence[str],
        llm_client: Any,
        prompt_builder: AgenticCachePromptBuilder,
        codec: AgenticCacheDecisionCodec,
        buffer: SingleSlotOverwriteBuffer,
        memory: TeamMemory,
        config: AgenticCacheConfig,
        registry: Any,
        spec: Any,
        cache: AgenticCacheStore,
        coordinator: AgenticCacheCoordinator,
        instructions: Any | None = None,
        acting_worker: "AgenticCacheActingWorker",
        metrics_provider: Callable[[], Mapping[str, Any]] | None = None,
        elapsed_provider: Callable[[], float] | None = None,
        budget: RunBudget | None = None,
        broadcast: Callable[[str], None] | None = None,
        event_sink: EventSink | None = None,
        liveness_gate: EntityLivenessGate | None = None,
        trade_recipe_history: Any | None = None,
    ) -> None:
        agent_id = str(agent.username)
        super().__init__(
            name=f"agentic-cache-planning-{agent_id}",
            loop_interval=config.planning_poll_interval,
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
        self.cache = cache
        self.trade_recipe_history = trade_recipe_history
        self.coordinator = coordinator
        self.instructions = instructions
        self.acting_worker = acting_worker
        self.metrics_provider = metrics_provider or (lambda: {})
        self.elapsed_provider = elapsed_provider or (lambda: 0.0)
        self.budget = budget
        self.broadcast = broadcast
        self.event_sink = event_sink
        self.liveness_gate = liveness_gate
        self.planned_count = 0
        self.cache_hit_count = 0
        self.cache_miss_count = 0
        self.llm_planned_count = 0
        self.last_plan_error: str | None = None
        self.planning_error_history: list[str] = []
        self._budget_exhausted_key: tuple[str, str] | None = None
        self._budget_exhausted_retry_at = 0.0
        self.consecutive_errors = 0

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
        previous = self.coordinator.previous_action()
        state_metadata = self._state_metadata()
        binding_context = self._binding_context()
        budget_available = self._llm_budget_available()
        budget_key = self._budget_key(previous)
        now = time.time()
        if not budget_available and now < self._budget_exhausted_retry_at:
            return
        if self._publish_from_cache(
            previous,
            state_metadata,
            binding_context,
            record_miss=budget_available
            or self._budget_exhausted_key != budget_key,
        ):
            self._budget_exhausted_key = None
            self._budget_exhausted_retry_at = 0.0
            return
        if not budget_available:
            self._record_budget_exhausted(budget_key)
            return
        self._plan_with_llm(previous, state_metadata, binding_context)

    def _publish_from_cache(
        self,
        previous: ActionPlan,
        state_metadata: dict[str, float],
        binding_context: dict[str, Any],
        *,
        record_miss: bool = True,
    ) -> bool:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return False
        lookup = self.cache.lookup(
            previous,
            state_metadata,
            owner_agent_id=self.agent_id,
            top_k=self.config.cache_top_k,
        )
        if not lookup.entries:
            self._record_cache_miss(previous, lookup, record_miss=record_miss)
            return False
        for entry in lookup.entries:
            try:
                proposal = self.codec.from_cached_plan(
                    entry.next_action,
                    agent_id=self.agent_id,
                    previous_action=previous,
                    state_metadata=state_metadata,
                    cache_entry_id=entry.entry_id,
                    reason=entry.reason,
                    binding_context=binding_context,
                )
            except Exception as exc:  # noqa: BLE001
                if self.liveness_gate is not None and not self.liveness_gate.is_live(
                    self.agent_id
                ):
                    return False
                rejected = self.cache.reject_entry(entry.entry_id)
                if self.event_sink is not None:
                    self.event_sink(
                        "cache.candidate_rejected",
                        agent_id=self.agent_id,
                        entry_id=entry.entry_id,
                        failure_count=(
                            rejected.failure_count if rejected is not None else None
                        ),
                        error=str(exc),
                    )
                continue
            if self.liveness_gate is not None and not self.liveness_gate.is_live(
                self.agent_id
            ):
                return False
            self._publish(proposal)
            self.cache_hit_count += 1
            self.planned_count += 1
            self.consecutive_errors = 0
            self.last_plan_error = None
            self.planning_error_history.clear()
            if self.event_sink is not None:
                self.event_sink(
                    "cache.hit",
                    agent_id=self.agent_id,
                    entry_id=entry.entry_id,
                    score=entry.score,
                    action=proposal.action_unit.action,
                )
            return True
        self._record_cache_miss(previous, lookup, record_miss=record_miss)
        return False

    def _plan_with_llm(
        self,
        previous: ActionPlan,
        state_metadata: dict[str, float],
        binding_context: dict[str, Any],
    ) -> None:
        if self.budget is not None and not self.budget.try_acquire(self.agent_id):
            self._record_budget_exhausted(self._budget_key(previous))
            return
        try:
            started = time.time()
            request = build_planning_request(
                prompt_builder=self.prompt_builder,
                memory=self.memory,
                spec=self.spec,
                registry=self.registry,
                instructions=self.instructions,
                config=self.config,
                agent_id=self.agent_id,
                teammates=self.teammates,
                last_error=self.last_plan_error,
                error_history=tuple(self.planning_error_history),
                cache_context="缓存未命中或候选不可执行，请基于当前观测规划下一步。",
                trade_recipe_history=self.trade_recipe_history,
            )
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
                binding_context=binding_context,
            )
            proposal.request_id = getattr(response, "request_id", None)
            proposal.previous_action = previous
            proposal.state_metadata = dict(state_metadata)
            proposal.planning_started_at = started
            proposal.planning_finished_at = finished
            self._publish(proposal)
            self.planned_count += 1
            self.llm_planned_count += 1
            self.consecutive_errors = 0
            self.last_plan_error = None
            self.planning_error_history.clear()
        except Exception as exc:  # noqa: BLE001
            self.consecutive_errors += 1
            error = str(exc)
            self.last_plan_error = error
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
                self.last_plan_error = None
                self.planning_error_history.clear()

    def _publish(self, proposal: AgenticCacheProposal) -> None:
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
        content["cache_plan" if proposal.path == "cache" else "standard_plan"] = action_text(
            proposal.action_unit.action,
            proposal.input_params,
            instructions=(
                self.instructions
                if getattr(self.spec, "category", "") == "trade"
                else None
            ),
            positional=proposal.input_positional,
        )
        if proposal.path != "cache":
            content["reasoning"] = proposal.reason or None
        entry = self.memory.append_history_event(
            agent_id=self.agent_id,
            source="self",
            event_type="planning",
            content=content,
            metadata={
                "plan_id": proposal.plan_id,
                "request_id": proposal.request_id,
                "path": proposal.path,
                "mode": proposal.action_unit.mode,
                "cache_entry_id": proposal.cache_entry_id,
            },
        )
        proposal.memory_entry_id = entry.entry_id
        buffer_version = self.buffer.put(proposal)
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
                buffer_version=buffer_version,
                path=proposal.path,
                action=proposal.action_unit.action,
            )

    def _state_metadata(self) -> dict[str, float]:
        elapsed = max(0.0, float(self.elapsed_provider()))
        remaining = _remaining_seconds(self.spec, elapsed)
        return current_state_metadata(
            memory=self.memory,
            agent_id=self.agent_id,
            metrics=self.metrics_provider(),
            elapsed_seconds=elapsed,
            remaining_seconds=remaining,
            action_index=self.coordinator.current_index(),
            max_fields=self.config.max_metadata_fields,
        )

    def _binding_context(self) -> dict[str, Any]:
        observation = self.memory.get_observation(self.agent_id)
        if observation is None:
            return {}
        value = observation.metadata.get("agentic_cache_binding_context")
        return dict(value) if isinstance(value, Mapping) else {}

    def _llm_budget_available(self) -> bool:
        if self.budget is None:
            return True
        can_acquire = getattr(self.budget, "can_acquire", None)
        if callable(can_acquire):
            return bool(can_acquire(self.agent_id))
        snapshot = self.budget.snapshot()
        team_cap = snapshot.get("team_cap")
        team_used = snapshot.get("team_used")
        if team_cap is not None and int(team_used or 0) >= int(team_cap):
            return False
        per_agent_cap = snapshot.get("per_agent_cap")
        per_agent_used = snapshot.get("per_agent_used") or {}
        used = (
            per_agent_used.get(self.agent_id, 0)
            if isinstance(per_agent_used, Mapping)
            else 0
        )
        return not (per_agent_cap is not None and int(used) >= int(per_agent_cap))

    def _record_budget_exhausted(self, budget_key: tuple[str, str]) -> None:
        first_for_key = self._budget_exhausted_key != budget_key
        self._budget_exhausted_key = budget_key
        self._budget_exhausted_retry_at = time.time() + max(
            1.0,
            self.config.observation_interval,
        )
        if first_for_key and self.event_sink is not None:
            self.event_sink(
                "planning.budget_exhausted",
                agent_id=self.agent_id,
                previous_action=self.coordinator.previous_action().to_dict(),
                budget=(self.budget.snapshot() if self.budget is not None else None),
            )

    def _record_cache_miss(
        self,
        previous: ActionPlan,
        lookup: Any,
        *,
        record_miss: bool,
    ) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        if not record_miss:
            return
        self.cache_miss_count += 1
        if self.event_sink is not None:
            self.event_sink(
                "cache.miss",
                agent_id=self.agent_id,
                previous_action=previous.to_dict(),
                matched=lookup.matched_count,
                filtered=lookup.filtered_count,
            )

    @staticmethod
    def _budget_key(previous: ActionPlan) -> tuple[str, str]:
        return (previous.name, repr(sorted(previous.parameters.items())))


class AgenticCacheActingWorker(LoopWorker):
    """Execute proposals and feed execution results back into the cache."""

    def __init__(
        self,
        *,
        agent: Any,
        buffer: SingleSlotOverwriteBuffer,
        memory: TeamMemory,
        config: AgenticCacheConfig,
        cache: AgenticCacheStore,
        coordinator: AgenticCacheCoordinator,
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
            name=f"agentic-cache-acting-{agent_id}",
            loop_interval=config.acting_poll_interval,
        )
        self.agent = agent
        self.agent_id = agent_id
        self.planner = agent.action_planner
        self.buffer = buffer
        self.memory = memory
        self.config = config
        self.cache = cache
        self.coordinator = coordinator
        self.instructions = instructions
        self.broadcast = broadcast
        self.event_sink = event_sink
        self.execution_table = execution_table
        self.call_file_resolver = call_file_resolver
        self.task_category = task_category
        self.trade_action_observer = trade_action_observer
        self.liveness_gate = liveness_gate
        self.attack_detection = tracker_for(agent)
        self.seen_version = 0
        self.active_proposal: AgenticCacheProposal | None = None
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
        if not isinstance(proposal, AgenticCacheProposal):
            raise TypeError("AgenticCache 动作缓冲包含无效条目")
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
                result = self.planner.stop(self.active_action_id, reason="runner_stopped")
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
        if proposal is None or proposal.action_unit.mode != ActionMode.CONTINUOUS:
            return
        if not self.active_action_id:
            raise RuntimeError("continuous action has no action id")
        result = self.planner.status(self.active_action_id)
        status = getattr(result, "status", None)
        if status in ActionStatus.TERMINAL:
            self._finish_active(result)

    def _dispatch(self, proposal: AgenticCacheProposal) -> None:
        apply_task_action_defaults(self.task_category, proposal)
        self.active_proposal = proposal
        self.execution_started_at = time.time()
        self.executed_count += 1
        if self.event_sink is not None:
            self.event_sink(
                "action.started",
                agent_id=self.agent_id,
                plan_id=proposal.plan_id,
                request_id=proposal.request_id,
                path=proposal.path,
                action=proposal.action_unit.action,
                mode=proposal.action_unit.mode,
            )
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
        if self.trade_action_observer is not None:
            self.trade_action_observer(
                self.agent_id,
                proposal.action_unit.action,
                proposal.input_params,
                processed,
                str(result_status),
            )
        success = bool(processed.get("ok")) and result_status != "failed"
        if success:
            self.attack_detection.mark_success()
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
                metadata={"completed": True},
            )
        if broadcast:
            self._passive_broadcast(
                proposal,
                str(result_status),
                result_error,
                processed,
            )
        if success:
            self._commit_communication(proposal)
        self._update_cache(proposal, success=success)
        executed = proposal.cache_action or action_unit_to_plan(
            proposal.action_unit,
            instructions=self.instructions,
            binding_context=self._binding_context(),
            input_params=proposal.input_params,
            input_positional=proposal.input_positional,
        )
        self.coordinator.on_action_finished(executed)
        self._write_execution_row(proposal, str(result_status), result_error, processed)
        if self.event_sink is not None:
            self.event_sink(
                "action.finished",
                agent_id=self.agent_id,
                plan_id=proposal.plan_id,
                request_id=proposal.request_id,
                path=proposal.path,
                action=proposal.action_unit.action,
                status=str(result_status),
                error=str(result_error) if result_error else None,
            )
        self.active_proposal = None
        self.active_action_id = None

    def _update_cache(
        self,
        proposal: AgenticCacheProposal,
        *,
        success: bool,
    ) -> None:
        if self.liveness_gate is not None and not self.liveness_gate.is_live(
            self.agent_id
        ):
            return
        previous = proposal.previous_action or self.coordinator.previous_action()
        executed = proposal.cache_action or action_unit_to_plan(
            proposal.action_unit,
            instructions=self.instructions,
            binding_context=self._binding_context(),
            input_params=proposal.input_params,
            input_positional=proposal.input_positional,
        )
        if success:
            entry = self.cache.upsert_success(
                previous_action=previous,
                next_action=executed,
                state_metadata=proposal.state_metadata,
                owner_agent_id=self.agent_id,
                reason=proposal.reason,
                source="cache_execution" if proposal.path == "cache" else "llm_execution",
            )
            if self.event_sink is not None:
                self.event_sink(
                    "cache.confirmed",
                    agent_id=self.agent_id,
                    entry_id=entry.entry_id,
                    path=proposal.path,
                    score=entry.score,
                )
            return
        if proposal.cache_entry_id:
            entry = self.cache.reject_entry(proposal.cache_entry_id)
            if self.event_sink is not None:
                self.event_sink(
                    "cache.rejected",
                    agent_id=self.agent_id,
                    entry_id=proposal.cache_entry_id,
                    failure_count=(entry.failure_count if entry is not None else None),
                )

    def _passive_broadcast(
        self,
        proposal: AgenticCacheProposal,
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
        text = _broadcast_text(
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

    def _commit_communication(self, proposal: AgenticCacheProposal) -> None:
        _commit_proposal_communication(
            proposal,
            memory=self.memory,
            agent_id=self.agent_id,
            config=self.config,
            broadcast=self.broadcast,
            event_sink=self.event_sink,
        )

    def _write_execution_row(
        self,
        proposal: AgenticCacheProposal,
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
            planning_duration = proposal.planning_finished_at - proposal.planning_started_at
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
                "interrupted_previous": False,
                "interrupted": result_status == "interrupted",
                "feedback": _execution_feedback(processed, result_status, result_error),
                "plan_source": proposal.path,
            }
        )

    def _binding_context(self) -> dict[str, Any]:
        observation = self.memory.get_observation(self.agent_id)
        if observation is None:
            return {}
        value = observation.metadata.get("agentic_cache_binding_context")
        return dict(value) if isinstance(value, Mapping) else {}

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


def _remaining_seconds(spec: Any, elapsed: float) -> float | None:
    completion = getattr(spec, "completion", None)
    if not isinstance(completion, Mapping):
        return None
    value = completion.get("timeout_seconds")
    if value is None:
        return None
    try:
        return max(0.0, float(value) - elapsed)
    except (TypeError, ValueError):
        return None


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


def _broadcast_text(
    agent_id: str,
    proposal: AgenticCacheProposal,
    result_status: str,
    result_error: str | None,
    instructions: Any | None,
) -> str:
    action = proposal.action_unit.action
    alias = action
    if instructions is not None:
        instruction = instructions.instruction_for_action(action)
        if instruction is not None:
            alias = instruction.alias
    call = f"执行{alias}({_render_input_args(proposal)})"
    if result_status == "interrupted":
        suffix = "中断"
    elif result_status == "failed":
        reason = (result_error or "").strip()
        suffix = f"失败：{reason}" if reason else "失败"
    else:
        suffix = "完成"
    return f"{agent_id}：{call} {suffix}"


def _render_input_args(proposal: AgenticCacheProposal) -> str:
    if proposal.input_positional:
        return ", ".join(repr(value) for value in proposal.input_positional)
    return ", ".join(
        f"{name}={value!r}" for name, value in proposal.input_params.items()
    )


__all__ = [
    "AgenticCacheActingWorker",
    "AgenticCacheCoordinator",
    "AgenticCachePlanningWorker",
    "ObservationWorker",
]
