"""PPA short controlled live smoke fixture (P0-6 / L1 carrier).

Starts a real Minecraft server with 1-2 agents and a scripted LLM client whose
deterministic JSON responses exercise one oneshot action, one continuous
action and one interrupt.  The task auto-terminates with ``survive_until`` and
cleanup is asserted (workers, server, ports).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import socket
import time
from types import SimpleNamespace
from typing import Any, Callable

from src.recording import RecordingConfig, RecordingManager
from src.tasks import (
    BaseTaskManager,
    TaskSpec,
    TaskStatus,
    create_task_environment_bundle,
    resolve_task_world,
)

from .runner import PPARunner
from .schemas import PPAConfig


ROOT = Path(__file__).resolve().parents[3]


def _decision_text(
    action: str,
    *,
    interrupt: bool = False,
    reason: str = "smoke",
) -> str:
    return json.dumps(
        {"skill": action, "interrupt": interrupt, "reason": reason},
        ensure_ascii=False,
    )


def default_sequences() -> dict[str, list[str]]:
    """Deterministic JSON decision sequences per agent (aliases)."""
    wait = _decision_text(
        "wait(seconds=30)",
        reason="smoke continuous",
    )
    interrupt = _decision_text(
        "chat(message='interrupt')",
        interrupt=True,
        reason="smoke interrupt",
    )
    idle = _decision_text("wait(seconds=1)", reason="smoke idle")
    hello = _decision_text("wait(seconds=1)", reason="smoke hello")
    return {
        "Alice": [wait, wait, wait, interrupt, idle, idle, idle, idle],
        "Bob": [hello, hello, hello, hello, hello, hello],
    }


class ScriptedLLMClient:
    """LLM client stub returning deterministic JSON decision sequences."""

    def __init__(
        self,
        sequences: dict[str, list[str]] | None = None,
        *,
        default_text: str | None = None,
    ) -> None:
        self.sequences = {
            agent_id: list(texts)
            for agent_id, texts in (sequences or {}).items()
        }
        self.default_text = default_text or _decision_text(
            "wait(seconds=1)",
            reason="smoke fallback",
        )
        self.requests: list[tuple[str, Any]] = []

    def complete(self, request: Any) -> SimpleNamespace:
        agent_id = str((request.metadata or {}).get("agent_id") or "unknown")
        self.requests.append((agent_id, request))
        queue = self.sequences.get(agent_id)
        text = queue.pop(0) if queue else self.default_text
        return SimpleNamespace(content=text, request_id=request.request_id)


def build_smoke_spec(
    *,
    agents: int = 2,
    duration_seconds: float = 8.0,
    timeout_seconds: float = 45.0,
) -> TaskSpec:
    """In-memory smoke task with a simple auto-detectable terminal condition."""
    if agents < 1 or agents > 4:
        raise ValueError("smoke agents must be between 1 and 4")
    if duration_seconds <= 0 or timeout_seconds <= duration_seconds:
        raise ValueError("duration_seconds must be positive and less than timeout_seconds")
    return TaskSpec.from_dict(
        {
            "task_id": "ppa.smoke.live",
            "category": "smoke",
            "name": "PPA Live Smoke",
            "description": "short controlled smoke with scripted decisions",
            "agents": {
                "count": agents,
                "names": list(("Alice", "Bob", "Carol", "Dave")[:agents]),
            },
            "environment": {"world": "base_env"},
            "completion": {
                "timeout_seconds": timeout_seconds,
                "success": {
                    "type": "survive_until",
                    "duration_seconds": duration_seconds,
                },
            },
        }
    )


@dataclass
class SmokeCheck:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class SmokeReport:
    exit_code: int = 1
    outcome: dict[str, Any] | None = None
    stop_reason: str | None = None
    planned_count: int = 0
    executed_count: int = 0
    checks: list[SmokeCheck] = field(default_factory=list)
    worker_errors: list[list[str]] = field(default_factory=list)
    cleanup_errors: list[list[str]] = field(default_factory=list)
    artifact_path: Path | None = None
    interrupted: bool = False

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(SmokeCheck(name=name, ok=ok, detail=detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "outcome": self.outcome,
            "stop_reason": self.stop_reason,
            "planned_count": self.planned_count,
            "executed_count": self.executed_count,
            "checks": [check.to_dict() for check in self.checks],
            "worker_errors": self.worker_errors,
            "cleanup_errors": self.cleanup_errors,
            "artifact_path": str(self.artifact_path) if self.artifact_path else None,
            "interrupted": self.interrupted,
        }

    def print_report(self, output: Callable[[str], None] = print) -> None:
        for check in self.checks:
            output(f"[{'OK' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        output(f"smoke exit_code={self.exit_code} outcome={self.outcome}")
        output(f"planned={self.planned_count} executed={self.executed_count}")
        if self.stop_reason:
            output(f"stop_reason={self.stop_reason}")
        if self.artifact_path is not None:
            output(f"smoke 证据: {self.artifact_path}")


def run_smoke(
    *,
    server_dir: str | Path,
    bridge_port_start: int = 3201,
    benchmark_root: str | Path,
    agents: int = 2,
    duration_seconds: float = 8.0,
    timeout_seconds: float = 45.0,
    planning_interval: float = 0.3,
    max_run_seconds: float = 40.0,
    record_dir: str | Path | None = None,
    sequences: dict[str, list[str]] | None = None,
    output: Callable[[str], None] = print,
) -> SmokeReport:
    """Run the short controlled live smoke and return an assertion report."""
    report = SmokeReport()
    runner: PPARunner | None = None
    bundle = None
    episode = None
    run_recorder = None
    try:
        spec = build_smoke_spec(
            agents=agents,
            duration_seconds=duration_seconds,
            timeout_seconds=timeout_seconds,
        )
        source_world = resolve_task_world(spec, benchmark_root)
        if record_dir is not None:
            recording_config = RecordingConfig(root_dir=Path(record_dir))
            run_recorder = RecordingManager(recording_config).start_run(
                "parallelized",
                task=spec.task_id,
                metadata={"kind": "smoke", "task_id": spec.task_id},
            )
            episode = run_recorder.start_episode(
                metadata={
                    "task_id": spec.task_id,
                    "task_spec": spec.to_dict(),
                    "smoke": {
                        "agents": agents,
                        "duration_seconds": duration_seconds,
                        "timeout_seconds": timeout_seconds,
                        "planning_interval": planning_interval,
                        "max_run_seconds": max_run_seconds,
                    },
                }
            )
        client = ScriptedLLMClient(sequences or default_sequences())
        bundle = create_task_environment_bundle(
            spec=spec,
            manager=BaseTaskManager(),
            source_world=source_world,
            server_dir=server_dir,
            bridge_port_start=bridge_port_start,
            log_dir=episode.logs_dir if episode is not None else None,
            runtime_root=ROOT / ".tmp",
        )
        effective_max_run_seconds = max(max_run_seconds, timeout_seconds + 15.0)
        config = PPAConfig(
            observation_interval=0.5,
            planning_interval=planning_interval,
            acting_poll_interval=0.05,
            task_poll_interval=0.3,
            initial_observation_timeout=45.0,
            worker_stop_timeout=15.0,
            max_consecutive_errors=30,
            max_run_seconds=effective_max_run_seconds,
        )
        runner = PPARunner.from_bundle(
            bundle,
            llm_client=client,
            config=config,
            episode_recorder=episode,
        )
        result = runner.run()
        report.outcome = result.outcome.to_dict() if result.outcome else None
        report.stop_reason = result.stop_reason
        report.planned_count = result.planned_count
        report.executed_count = result.executed_count
        report.worker_errors = [list(item) for item in result.worker_errors]
        report.cleanup_errors = [list(item) for item in result.cleanup_errors]
        if episode is not None:
            report.artifact_path = episode.path

        report.add(
            "outcome.success",
            bool(
                result.outcome is not None
                and result.outcome.status == TaskStatus.SUCCESS
            ),
            str(report.outcome),
        )
        report.add(
            "planned.positive",
            result.planned_count >= 1,
            f"planned={result.planned_count}",
        )
        report.add(
            "executed.positive",
            result.executed_count >= 1,
            f"executed={result.executed_count}",
        )
        report.add(
            "interrupt.observed",
            _interrupt_observed(runner.memory, agents),
            "wait.wait 条目应为 interrupted",
        )
        report.add(
            "chat.interrupt_executed",
            _chat_executed(runner.memory, agents, "interrupt"),
            "chat(message='interrupt') 应已执行",
        )
        status = runner.status()
        workers_stopped = all(
            not worker["running"]
            for agent_state in status["agents"].values()
            for worker in agent_state.values()
        )
        report.add("workers.stopped", workers_stopped, "")
        server_stopped = not bool(getattr(bundle.server, "is_running", False))
        report.add("server.stopped", server_stopped, "")
        report.add(
            "runner.clean",
            not result.worker_errors and not result.cleanup_errors,
            f"worker_errors={report.worker_errors} cleanup_errors={report.cleanup_errors}",
        )
        server_port = _server_port(server_dir)
        ports = [server_port, *[bridge_port_start + i for i in range(agents)]]
        report.add("ports.released", _ports_released(ports), f"ports={ports}")
        report.exit_code = (
            0
            if result.exit_code == 0 and all(check.ok for check in report.checks)
            else 1
        )
    except KeyboardInterrupt:
        report.interrupted = True
        report.add("interrupted", False, "KeyboardInterrupt")
        report.exit_code = 130
    except Exception as exc:  # noqa: BLE001
        report.add("smoke.failed", False, str(exc))
        report.exit_code = 1
    finally:
        if runner is None and bundle is not None:
            try:
                bundle.close_runtime()
            except Exception:  # noqa: BLE001
                pass
        if episode is not None and episode.status == "started":
            try:
                episode_status = (
                    "completed"
                    if report.exit_code == 0
                    else "failed"
                )
                episode.finalize(status=episode_status)
                if run_recorder is not None:
                    run_recorder.finalize(status=episode_status)
            except Exception:  # noqa: BLE001
                pass
    return report


def _interrupt_observed(memory: Any, agents: int) -> bool:
    for index in range(agents):
        for entry in _history(memory, ("Alice", "Bob", "Carol", "Dave")[index]):
            content = entry.get("content") or {}
            if (
                content.get("selected_action", "").startswith("wait.wait")
                and content.get("result_status") == "interrupted"
            ):
                return True
    return False


def _chat_executed(memory: Any, agents: int, message: str) -> bool:
    for index in range(agents):
        for entry in _history(memory, ("Alice", "Bob", "Carol", "Dave")[index]):
            content = entry.get("content") or {}
            if (
                content.get("selected_action", "").startswith("chat.say")
                and message in content.get("selected_action", "")
                and content.get("result_status") == "completed"
            ):
                return True
    return False


def _history(memory: Any, agent_id: str) -> list[Any]:
    return [
        entry
        for entry in memory.prompt_history(
            agent_id,
            team_limit=50,
            system_limit=1,
        )
        if isinstance(entry.get("content"), dict)
    ]


def _server_port(server_dir: str | Path) -> int:
    properties = Path(server_dir) / "server.properties"
    for raw_line in properties.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "server-port":
            try:
                return int(value.strip())
            except ValueError:
                return 25565
    return 25565


def _port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _ports_released(ports: list[int], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_port_in_use(port) for port in ports):
            return True
        time.sleep(0.3)
    return not any(_port_in_use(port) for port in ports)


__all__ = [
    "ScriptedLLMClient",
    "SmokeCheck",
    "SmokeReport",
    "build_smoke_spec",
    "default_sequences",
    "run_smoke",
]
