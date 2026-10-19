"""纯任务环境到 DebugController 的同进程适配器。"""

from __future__ import annotations

import threading
from typing import Any

from src.agents.action import (
    SimpleInstructionError,
    SimpleInstructionRegistry,
    parse_action_call,
)
from src.perception import (
    RconBlockEntityProvider,
    WorldRuleOptions,
    select_world_sections,
    task_perception,
    task_verbalizer,
    world_snapshot,
    world_verbalizer,
)
from src.perception.filters import filter_observer_players
from src.perception.rule import RuleContext, apply_rule
from src.tasks import TaskEnvironmentBundle, TaskEnvironmentSession, TaskStatus

from .providers import ComponentInspector
from .recorder import DebugRecorder
from .schemas import DebugOperationError, to_jsonable


class TaskDebugAdapter:
    """启动并监控 TaskEnvironmentSession，但终态后保留现场。"""

    def __init__(
        self,
        session: TaskEnvironmentSession,
        *,
        bundle: TaskEnvironmentBundle | None = None,
        recorder: DebugRecorder | None = None,
        poll_interval: float = 0.2,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.session = session
        self.bundle = bundle
        self.recorder = recorder
        self.poll_interval = poll_interval
        self._agents = {agent.username: agent for agent in session.agents}
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._last_metrics: dict[str, Any] = {}
        self._outcome: dict[str, Any] | None = None
        self._monitor_error: dict[str, str] | None = None
        self._cleanup_errors: list[tuple[str, str]] = []
        self._block_entity_provider = RconBlockEntityProvider(session.server)
        self.instructions = SimpleInstructionRegistry()

    @property
    def agent_names(self) -> list[str]:
        return list(self._agents)

    def start(self) -> None:
        with self._state_lock:
            if self._started:
                raise RuntimeError("TaskDebugAdapter can only start once")
            if self._closed:
                raise RuntimeError("TaskDebugAdapter is closed")
            self._started = True
        try:
            self.session.start()
        except BaseException as exc:
            cleanup_errors = self.session.close()
            self._close_runtime(cleanup_errors)
            if self.recorder is not None:
                self.recorder.record_event(
                    "session.start_failed",
                    {"cleanup_errors": cleanup_errors},
                    ok=False,
                    error={"code": "startup_failed", "message": str(exc)},
                )
            raise
        self._monitor_thread = threading.Thread(
            target=self._monitor,
            name=f"debug-monitor-{self.session.spec.task_id}",
            daemon=True,
        )
        if self.recorder is not None:
            self.recorder.record_event(
                "session.started",
                self.session.snapshot(),
            )
        self._monitor_thread.start()

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
        target = self._require_agent(agent)
        planner = getattr(target, "action_planner", None)
        if planner is None:
            raise DebugOperationError(
                "action_planner_unavailable",
                f"Agent has no action planner: {agent}",
            )
        if operation == "list":
            return target.list_skills()
        if operation in {"run", "start"}:
            if not action:
                raise DebugOperationError("invalid_action", "Action name is required")
            internal_actions = frozenset(getattr(planner, "internal_actions", ()))
            if "(" in action:
                try:
                    name, call_params, positional = parse_action_call(action)
                    canonical, filled, mode = self.instructions.expand(
                        name,
                        call_params,
                        positional,
                    )
                except SimpleInstructionError as exc:
                    raise DebugOperationError(
                        "invalid_shorthand_action",
                        str(exc),
                    ) from exc
                if canonical in internal_actions:
                    raise DebugOperationError(
                        "internal_action_forbidden",
                        f"Manager internal action is not exposed: {canonical}",
                    )
                if operation == "run":
                    return planner.execute(canonical, filled, mode=mode)
                return planner.start(canonical, filled, mode=mode)
            if action in internal_actions:
                raise DebugOperationError(
                    "internal_action_forbidden",
                    f"Manager internal action is not exposed: {action}",
                )
            if operation == "run":
                return planner.execute(action, params or {})
            return planner.start(action, params or {})
        if operation == "update":
            if not action_id:
                raise DebugOperationError("invalid_action_id", "action_id is required")
            return planner.update(action_id, params or {})
        if operation == "stop":
            if not action_id:
                raise DebugOperationError("invalid_action_id", "action_id is required")
            return planner.stop(action_id, reason=reason or "debug_stop")
        if operation == "status":
            return planner.status(action_id)
        raise DebugOperationError(
            "unsupported_action_operation",
            f"Unsupported action operation: {operation}",
        )

    def send_server_command(self, command: str) -> Any:
        return self.session.server.send_command(command)

    def observe_agent(self, agent: str, options: dict[str, Any]) -> Any:
        target = self._require_agent(agent)
        aliases = {
            "subMode": "sub_mode",
            "blockRadius": "block_radius",
            "fovDegrees": "fov_degrees",
        }
        normalized = {aliases.get(key, key): value for key, value in options.items()}
        allowed = {
            "mode",
            "sub_mode",
            "radius",
            "block_radius",
            "fov_degrees",
            "timeout",
            "representation",
            "sections",
            "limits",
            "language",
            "container_internal_enabled",
            "processor_internal_enabled",
            "use_range",
            "require_line_of_sight",
            "show_disabled",
        }
        unknown = sorted(set(normalized) - allowed)
        if unknown:
            raise DebugOperationError(
                "invalid_observation_options",
                f"Unknown observation options: {unknown}",
            )
        mode = normalized.get("mode", "global")
        if mode not in {"global", "local"}:
            raise DebugOperationError(
                "invalid_observation_mode",
                f"Observation mode must be global or local: {mode}",
            )
        representation = normalized.pop("representation", "raw")
        if representation not in {"raw", "world", "text", "task", "task_text"}:
            raise DebugOperationError(
                "invalid_observation_representation",
                "Observation representation must be raw, world, text, task, or task_text",
            )
        sections = normalized.pop("sections", None)
        if sections is not None and (
            not isinstance(sections, list)
            or any(not isinstance(section, str) for section in sections)
        ):
            raise DebugOperationError(
                "invalid_observation_sections",
                "Observation sections must be a list of section names",
            )
        limits = normalized.pop("limits", None)
        if limits is not None and (
            not isinstance(limits, dict)
            or any(
                not isinstance(name, str)
                or not isinstance(limit, int)
                or isinstance(limit, bool)
                or limit < 0
                for name, limit in limits.items()
            )
        ):
            raise DebugOperationError(
                "invalid_observation_limits",
                "Observation limits must map section names to non-negative integers",
            )
        language = normalized.pop("language", "zh")
        if representation in {"task", "task_text"} and (
            sections is not None or limits is not None
        ):
            raise DebugOperationError(
                "invalid_task_observation_options",
                "Task representations use the registered profile sections and limits",
            )
        container_internal_enabled = _pop_bool(
            normalized, "container_internal_enabled", True
        )
        processor_internal_enabled = _pop_bool(
            normalized, "processor_internal_enabled", True
        )
        require_line_of_sight = _pop_bool(
            normalized, "require_line_of_sight", True
        )
        show_disabled = _pop_bool(normalized, "show_disabled", False)
        use_range = normalized.pop("use_range", 4.5)
        try:
            world_options = WorldRuleOptions(
                container_internal_enabled=container_internal_enabled,
                processor_internal_enabled=processor_internal_enabled,
                use_range=float(use_range),
                require_line_of_sight=require_line_of_sight,
            )
        except (TypeError, ValueError) as exc:
            raise DebugOperationError(
                "invalid_world_observation_options", str(exc)
            ) from exc

        raw = filter_observer_players(target.get_raw_perception(**normalized))
        if representation == "raw":
            return raw
        metadata = raw.get("metadata") if isinstance(raw, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        context = RuleContext(
            mode=str(normalized.get("mode", metadata.get("mode", "global"))),
            sub_mode=str(
                normalized.get("sub_mode", metadata.get("subMode", "radius"))
            ),
            radius=normalized.get("radius", metadata.get("radius")),
            agent_name=agent,
            metadata={
                "raw_data": raw,
                "raw_metadata": metadata,
                "block_entity_provider": self._block_entity_provider,
            },
        )
        world_input = apply_rule(
            world_snapshot(options=world_options, provider=self._block_entity_provider),
            raw,
            context,
        )
        if representation in {"task", "task_text"}:
            session_snapshot = self.session.snapshot()
            task_metrics = self.metrics() or dict(session_snapshot.get("metrics") or {})
            registry_path = self.bundle.registry_file if self.bundle is not None else None
            task_input = apply_rule(
                task_perception(
                    self.session.spec,
                    metrics=task_metrics,
                    elapsed_seconds=float(session_snapshot.get("elapsed_seconds") or 0.0),
                    registry_path=registry_path,
                ),
                world_input,
                context,
            )
            if representation == "task":
                return task_input
            try:
                return task_verbalizer(
                    self.session.spec.category,
                    language=str(language),
                ).render(task_input)
            except ValueError as exc:
                raise DebugOperationError(
                    "invalid_task_observation_options", str(exc)
                ) from exc

        rendered_input = world_input
        if sections is not None:
            try:
                rendered_input = apply_rule(
                    select_world_sections(*sections), rendered_input, context
                )
            except ValueError as exc:
                raise DebugOperationError(
                    "invalid_observation_sections", str(exc)
                ) from exc
        if representation == "world":
            return rendered_input
        try:
            return world_verbalizer(
                language=str(language),
                sections=sections,
                limits=limits,
                show_disabled=show_disabled,
            ).render(rendered_input)
        except ValueError as exc:
            raise DebugOperationError(
                "invalid_world_observation_options", str(exc)
            ) from exc

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            monitor_alive = bool(
                self._monitor_thread and self._monitor_thread.is_alive()
            )
            monitor_error = dict(self._monitor_error) if self._monitor_error else None
            cleanup_errors = [list(item) for item in self._cleanup_errors]
        session_snapshot = self.session.snapshot()
        return {
            "session": session_snapshot,
            "server": {
                "running": bool(self.session.server.is_running),
                "pid": getattr(self.session.server, "pid", None),
            },
            "monitor": {
                "running": monitor_alive,
                "error": monitor_error,
            },
            "cleanup_errors": cleanup_errors,
        }

    def metrics(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._last_metrics)

    def outcome(self) -> dict[str, Any] | None:
        with self._state_lock:
            return dict(self._outcome) if self._outcome is not None else None

    def component_providers(self) -> dict[str, ComponentInspector]:
        return {
            "session": lambda params: self.session.snapshot(),
            "server": self._inspect_server,
            "task": self._inspect_task,
            "manager": self._inspect_manager,
            "agents": self._inspect_agents,
            "agent": self._inspect_agent,
            "skills": self._inspect_skills,
            "memory": self._inspect_memory,
            "scheduler": self._inspect_scheduler,
        }

    def close(self) -> list[tuple[str, str]]:
        with self._state_lock:
            if self._closed:
                return list(self._cleanup_errors)
            self._closed = True
            self._stop_event.set()
            thread = self._monitor_thread
        errors: list[tuple[str, str]] = []
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self.poll_interval * 4))
            if thread.is_alive():
                errors.append(("monitor", "monitor thread did not stop in time"))
        errors.extend(self.session.close())
        self._close_runtime(errors)
        with self._state_lock:
            self._cleanup_errors = list(errors)
        if self.recorder is not None:
            self.recorder.record_event(
                "session.closed",
                {"cleanup_errors": [list(item) for item in errors]},
                ok=not errors,
                error=(
                    None
                    if not errors
                    else {
                        "code": "cleanup_failed",
                        "message": "One or more cleanup steps failed",
                    }
                ),
            )
        return list(errors)

    def _monitor(self) -> None:
        try:
            while not self._stop_event.is_set():
                poll = self.session.poll()
                with self._state_lock:
                    self._last_metrics = dict(poll.metrics)
                    if poll.outcome.status != TaskStatus.RUNNING:
                        self._outcome = poll.outcome.to_dict()
                if poll.outcome.status != TaskStatus.RUNNING:
                    if self.recorder is not None:
                        self.recorder.record_event(
                            "task.terminal",
                            {
                                "outcome": poll.outcome.to_dict(),
                                "metrics": poll.metrics,
                                "environment_retained": True,
                            },
                        )
                    return
                self._stop_event.wait(self.poll_interval)
        except Exception as exc:  # noqa: BLE001
            error = {"code": "monitor_failed", "message": str(exc)}
            with self._state_lock:
                self._monitor_error = error
            if self.recorder is not None:
                self.recorder.record_event(
                    "monitor.failed",
                    ok=False,
                    error=error,
                )

    def _require_agent(self, name: str) -> Any:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise DebugOperationError("unknown_agent", f"Unknown agent: {name}") from exc

    def _close_runtime(self, errors: list[tuple[str, str]]) -> None:
        if self.bundle is None:
            return
        try:
            self.bundle.close_runtime()
        except Exception as exc:  # noqa: BLE001
            errors.append(("runtime_directory", str(exc)))

    def _inspect_server(self, params: dict[str, Any]) -> dict[str, Any]:
        server = self.session.server
        return {
            "type": type(server).__name__,
            "running": bool(server.is_running),
            "pid": getattr(server, "pid", None),
            "server_dir": str(getattr(server, "server_dir", "")) or None,
            "rcon_enabled": bool(getattr(server, "rcon_enabled", False)),
        }

    def _inspect_task(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "spec": self.session.spec.to_dict(),
            "source_path": (
                str(self.session.spec.source_path)
                if self.session.spec.source_path is not None
                else None
            ),
        }

    def _inspect_manager(self, params: dict[str, Any]) -> dict[str, Any]:
        manager = self.session.manager
        return {
            "type": type(manager).__name__,
            "prepared": bool(getattr(manager, "_prepared", False)),
            "started": bool(getattr(manager, "_started", False)),
            "started_at": getattr(manager, "started_at", None),
            "outcome": self.outcome(),
            "metrics": self.metrics(),
        }

    def _inspect_agents(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "agents": [self._agent_snapshot(name) for name in self.agent_names]
        }

    def _inspect_agent(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("agent") or params.get("name")
        if not isinstance(name, str) or not name:
            raise DebugOperationError(
                "agent_parameter_required",
                "inspect agent requires {'agent': 'agent-N'}",
            )
        return self._agent_snapshot(name)

    def _agent_snapshot(self, name: str) -> dict[str, Any]:
        agent = self._require_agent(name)
        try:
            status = agent.get_status()
            status_error = None
        except Exception as exc:  # noqa: BLE001
            status = None
            status_error = str(exc)
        return {
            "name": name,
            "type": type(agent).__name__,
            "bridge_port": getattr(agent, "bridge_port", None),
            "bridge_running": (
                agent.is_bridge_running()
                if callable(getattr(agent, "is_bridge_running", None))
                else None
            ),
            "status": status,
            "status_error": status_error,
        }

    def _inspect_skills(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("agent")
        names = [requested] if isinstance(requested, str) else self.agent_names
        result: dict[str, Any] = {}
        for name in names:
            agent = self._require_agent(name)
            try:
                result[name] = {"ok": True, "skills": agent.list_skills()}
            except Exception as exc:  # noqa: BLE001
                result[name] = {"ok": False, "error": str(exc)}
        return result

    def _inspect_memory(self, params: dict[str, Any]) -> dict[str, Any]:
        memory = self.session.memory
        if memory is None:
            return {"configured": False}
        try:
            entries = memory.snapshot()
        except Exception as exc:  # noqa: BLE001
            return {
                "configured": True,
                "type": type(memory).__name__,
                "error": str(exc),
            }
        return {
            "configured": True,
            "type": type(memory).__name__,
            "entries": to_jsonable(entries),
        }

    def _inspect_scheduler(self, params: dict[str, Any]) -> dict[str, Any]:
        manager = self.session.manager
        scheduler = getattr(manager, "_scheduler", None)
        thread = getattr(manager, "_scheduler_thread", None)
        error = getattr(manager, "_scheduler_error", None)
        if scheduler is None:
            return {"configured": False, "error": str(error) if error else None}
        try:
            snapshot = scheduler.result_snapshot()
        except Exception as exc:  # noqa: BLE001
            snapshot = {"error": str(exc)}
        return {
            "configured": True,
            "type": type(scheduler).__name__,
            "thread_alive": bool(thread and thread.is_alive()),
            "error": str(error) if error else None,
            "result": to_jsonable(snapshot),
        }


def _pop_bool(options: dict[str, Any], name: str, default: bool) -> bool:
    value = options.pop(name, default)
    if not isinstance(value, bool):
        raise DebugOperationError(
            "invalid_world_observation_options",
            f"{name} must be a boolean",
        )
    return value
