"""PPA live preflight: environment, config, world, port, RCON and LLM readiness."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import importlib
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

from src.agents.action import SkillRegistry
from src.launcher.mcrcon import load_rcon_config
from src.launcher.server import Server
from src.llm import LLMClient, LLMConfig, LLMRequest
from src.recording import (
    LLMCallSource,
    RecordingManager,
    resolve_recording_config,
)
from src.tasks import (
    TaskSpec,
    TaskWorldError,
    TaskWorldWorkspace,
    resolve_task_world,
)

from .text_protocol import JsonDecisionCodec


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MINEFLAYER_DIR = ROOT / "src" / "env" / "mineflayer"
PROBE_USER_PROMPT = (
    "输出一个 wait.wait(seconds=0) 的动作选择，"
    "严格按系统指令的 JSON 格式。"
)


class PreflightStatus:
    OK = "ok"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


@dataclass(frozen=True)
class PreflightCheck:
    """One named readiness check result."""

    name: str
    category: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class PreflightReport:
    """Aggregated preflight results with a deterministic exit code."""

    checks: list[PreflightCheck] = field(default_factory=list)
    started_at: float | None = None
    finished_at: float | None = None
    server_was_started: bool = False
    llm_probe_request_id: str | None = None
    path: Path | None = None
    interrupted: bool = False

    def add(
        self,
        name: str,
        category: str,
        status: str,
        detail: str = "",
    ) -> None:
        self.checks.append(
            PreflightCheck(name=name, category=category, status=status, detail=detail)
        )

    @property
    def failed_checks(self) -> list[PreflightCheck]:
        return [check for check in self.checks if check.status == PreflightStatus.FAIL]

    @property
    def exit_code(self) -> int:
        if self.interrupted:
            return 130
        return 1 if self.failed_checks else 0

    def summary_dict(self) -> dict[str, Any]:
        counts = {
            PreflightStatus.OK: 0,
            PreflightStatus.FAIL: 0,
            PreflightStatus.WARN: 0,
            PreflightStatus.SKIP: 0,
        }
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return {
            "status": "interrupted" if self.interrupted else (
                "ok" if not self.failed_checks else "failed"
            ),
            "counts": counts,
            "server_was_started": self.server_was_started,
            "llm_probe_request_id": self.llm_probe_request_id,
        }

    def print_report(self, output: Callable[[str], None] = print) -> None:
        for check in self.checks:
            output(f"[{check.status.upper():>4}] {check.category}.{check.name}: {check.detail}")
        counts = self.summary_dict()["counts"]
        output(
            f"preflight: ok={counts.get('ok', 0)} "
            f"fail={counts.get('fail', 0)} "
            f"warn={counts.get('warn', 0)} "
            f"skip={counts.get('skip', 0)}"
        )
        if self.path is not None:
            output(f"preflight 证据: {self.path}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [check.to_dict() for check in self.checks],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "server_was_started": self.server_was_started,
            "llm_probe_request_id": self.llm_probe_request_id,
            "path": str(self.path) if self.path is not None else None,
            "interrupted": self.interrupted,
            "exit_code": self.exit_code,
            "summary": self.summary_dict(),
        }


def _port_in_use(host: str, port: int) -> bool:
    """Return True when a TCP listener accepts connections on host:port."""
    try:
        with socket.create_connection((host, int(port)), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


def _read_properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _java_major(path: str) -> tuple[int | None, str]:
    """Best-effort parse of ``java -version`` into a major version."""
    try:
        proc = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        text = (proc.stderr or "") + (proc.stdout or "")
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    match = re.search(r'version "([^"]+)"', text)
    if not match:
        return None, (text.strip() or "java -version 输出无法解析")
    version = match.group(1)
    try:
        parts = version.split(".")
        if parts[0] == "1" and len(parts) > 1:
            major = int(parts[1])
        else:
            major = int(re.split(r"[-+]", parts[0])[0])
        return major, version
    except (ValueError, IndexError):
        return None, version


def _assert_writable(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        dir=str(directory),
        prefix=".preflight-",
        delete=False,
    )
    handle.close()
    probe = Path(handle.name)
    try:
        probe.unlink()
    finally:
        if probe.exists():
            probe.unlink()


def _redact_text(text: str, secret: str | None) -> str:
    if secret:
        return text.replace(secret, "***")
    return text


def _default_registry() -> SkillRegistry:
    return SkillRegistry(allowed_actions={"wait.wait"})


def run_preflight(
    *,
    spec: TaskSpec,
    server_dir: str | Path,
    bridge_port_start: int,
    benchmark_root: str | Path,
    env_file: str | Path | None = None,
    record_dir: str | Path | None = None,
    output: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,  # reserved for paced retries
    server_factory: Callable[..., Any] = Server,
    llm_config_factory: Callable[[Any], LLMConfig] = LLMConfig.from_env,
    llm_client_factory: Callable[[LLMConfig], Any] = LLMClient,
    registry_factory: Callable[[], SkillRegistry] | None = None,
    port_checker: Callable[[str, int], bool] | None = None,
    command_locator: Callable[[str], str | None] | None = None,
    java_version: Callable[[str], tuple[int | None, str]] | None = None,
    world_workspace_factory: Callable[..., Any] = TaskWorldWorkspace,
    mineflayer_dir: str | Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> PreflightReport:
    """Run all live preflight checks and return an aggregated report.

    Checks are independent and always collected together; any fail makes
    ``exit_code`` 1 (Ctrl+C makes it 130).  A real Minecraft server may be
    started and stopped for an RCON query, and one LLM call is made for the
    protocol probe; no task action is executed and the disposable world is
    never prepared.
    """
    report = PreflightReport(started_at=monotonic())
    server_dir = Path(server_dir)
    benchmark_root = Path(benchmark_root)
    mineflayer_dir = Path(mineflayer_dir) if mineflayer_dir is not None else DEFAULT_MINEFLAYER_DIR
    registry_factory = registry_factory or _default_registry
    port_checker = port_checker or _port_in_use
    command_locator = command_locator or shutil.which
    java_version = java_version or _java_major

    agent_count = int(spec.agents.get("count", 0) or 0)
    if agent_count < 1:
        report.add("agents.count", "config", PreflightStatus.FAIL, "spec.agents.count 必须 >= 1")

    llm_config: LLMConfig | None = None
    episode = None
    run_recorder = None
    server = None
    client = None
    try:
        # --- LLM configuration (check) ---
        try:
            llm_config = llm_config_factory(env_file)
            report.add(
                "llm.config",
                "llm",
                PreflightStatus.OK,
                f"model={llm_config.model}",
            )
        except Exception as exc:  # noqa: BLE001
            report.add(
                "llm.config",
                "llm",
                PreflightStatus.FAIL,
                _redact_text(str(exc), None),
            )

        # --- recording setup (episode for the probe and report evidence) ---
        try:
            recording_config = resolve_recording_config(
                spec.output,
                root_dir=record_dir,
                base_dir=ROOT,
            )
            if llm_config is not None and llm_config.api_key:
                recording_config = replace(
                    recording_config,
                    redact_values=(
                        *recording_config.redact_values,
                        llm_config.api_key,
                    ),
                )
            run_recorder = RecordingManager(recording_config).start_run(
                "parallelized",
                task=spec.task_id,
                metadata={
                    "kind": "preflight",
                    "task_id": spec.task_id,
                },
            )
            episode = run_recorder.start_episode(
                metadata={
                    "kind": "preflight",
                    "task_id": spec.task_id,
                    "task_spec": spec.to_dict(),
                    "agents": list(spec.agents.get("names") or []),
                }
            )
            report.path = episode.path
            if llm_config is not None:
                episode.update_metadata(llm_config=llm_config.safe_dict())
            output(f"[preflight] episode: {episode.path}")
        except Exception as exc:  # noqa: BLE001
            report.add("recording.setup", "recording", PreflightStatus.FAIL, str(exc))

        # --- toolchain ---
        try:
            importlib.import_module("openai")
            importlib.import_module("mcrcon")
            report.add(
                "python.imports",
                "toolchain",
                PreflightStatus.OK,
                f"interpreter={sys.executable}",
            )
        except Exception as exc:  # noqa: BLE001
            report.add(
                "python.imports",
                "toolchain",
                PreflightStatus.FAIL,
                str(exc),
            )

        java_path = command_locator("java")
        if java_path is None:
            report.add("toolchain.java", "toolchain", PreflightStatus.FAIL, "未找到 java")
        else:
            major, raw = java_version(java_path)
            if major is None:
                report.add(
                    "toolchain.java",
                    "toolchain",
                    PreflightStatus.WARN,
                    f"java 版本无法解析: {raw[:80]}",
                )
            elif major >= 17:
                report.add(
                    "toolchain.java",
                    "toolchain",
                    PreflightStatus.OK,
                    f"major={major}",
                )
            else:
                report.add(
                    "toolchain.java",
                    "toolchain",
                    PreflightStatus.WARN,
                    f"major={major} < 17（Minecraft 1.19 需要 Java 17+）",
                )

        if command_locator("node") is None:
            report.add("toolchain.node", "toolchain", PreflightStatus.FAIL, "未找到 node")
        else:
            report.add("toolchain.node", "toolchain", PreflightStatus.OK, "node 可用")

        package_json = mineflayer_dir / "package.json"
        node_modules = mineflayer_dir / "node_modules"
        if package_json.is_file() and node_modules.is_dir():
            report.add(
                "toolchain.node_modules",
                "toolchain",
                PreflightStatus.OK,
                str(mineflayer_dir),
            )
        else:
            report.add(
                "toolchain.node_modules",
                "toolchain",
                PreflightStatus.FAIL,
                f"mineflayer 依赖不完整: {mineflayer_dir}",
            )

        # --- files and configuration ---
        jar_path = server_dir / "server.jar"
        jar_ok = jar_path.is_file()
        if jar_ok:
            report.add("files.server_jar", "files", PreflightStatus.OK, str(jar_path))
        else:
            report.add(
                "files.server_jar",
                "files",
                PreflightStatus.FAIL,
                f"缺少 server.jar: {jar_path}",
            )

        properties = _read_properties(server_dir / "server.properties")
        properties_ok = False
        if not properties:
            report.add(
                "files.server_properties",
                "files",
                PreflightStatus.FAIL,
                f"缺少或无法读取 server.properties: {server_dir}",
            )
        else:
            try:
                int(properties.get("server-port", "25565"))
                int(properties.get("rcon.port", "25575"))
            except ValueError:
                report.add(
                    "files.server_properties",
                    "files",
                    PreflightStatus.FAIL,
                    "server-port / rcon.port 必须为整数",
                )
            else:
                if properties.get("online-mode", "true").lower() == "true":
                    report.add(
                        "files.server_properties",
                        "files",
                        PreflightStatus.FAIL,
                        "online-mode=true 与本地离线 Mineflayer 登录不兼容",
                    )
                else:
                    report.add(
                        "files.server_properties",
                        "files",
                        PreflightStatus.OK,
                        f"port={properties.get('server-port')} "
                        f"rcon_port={properties.get('rcon.port')}",
                    )
                    properties_ok = True

        rcon_cfg = load_rcon_config(server_dir)
        if rcon_cfg["enabled"] and rcon_cfg["password"]:
            report.add(
                "server.rcon_config",
                "server",
                PreflightStatus.OK,
                f"host={rcon_cfg['host']} port={rcon_cfg['port']}",
            )
        else:
            report.add(
                "server.rcon_config",
                "server",
                PreflightStatus.FAIL,
                "enable-rcon 未开启或 rcon.password 为空",
            )

        # --- world ---
        try:
            source_world = resolve_task_world(spec, benchmark_root)
            report.add(
                "world.source",
                "world",
                PreflightStatus.OK,
                str(source_world),
            )
        except TaskWorldError as exc:
            source_world = None
            report.add("world.source", "world", PreflightStatus.FAIL, str(exc))
        if source_world is not None:
            try:
                workspace = world_workspace_factory(
                    source_world=source_world,
                    server_dir=server_dir,
                )
                workspace.check_disposable()
                report.add(
                    "world.disposable",
                    "world",
                    PreflightStatus.OK,
                    "一次性世界目标无锁且路径安全",
                )
            except Exception as exc:  # noqa: BLE001
                report.add(
                    "world.disposable",
                    "world",
                    PreflightStatus.FAIL,
                    str(exc),
                )
        else:
            report.add(
                "world.disposable",
                "world",
                PreflightStatus.SKIP,
                "基准世界不可用，跳过一次性世界检查",
            )

        # --- ports ---
        if properties_ok and agent_count >= 1:
            try:
                server_port = int(properties.get("server-port", "25565"))
                rcon_port = int(properties.get("rcon.port", "25575"))
            except ValueError:
                report.add("ports.config", "ports", PreflightStatus.FAIL, "端口配置无效")
            else:
                bridge_ports = [
                    bridge_port_start + index for index in range(agent_count)
                ]
                all_ports = [server_port, rcon_port, *bridge_ports]
                duplicates = sorted(
                    {
                        port
                        for port in all_ports
                        if all_ports.count(port) > 1
                    }
                )
                if duplicates:
                    report.add(
                        "ports.overlap",
                        "ports",
                        PreflightStatus.FAIL,
                        f"端口互相重叠: {duplicates}",
                    )
                else:
                    report.add(
                        "ports.overlap",
                        "ports",
                        PreflightStatus.OK,
                        "端口无重叠",
                    )
                port_entries = [
                    ("ports.minecraft", "Minecraft", server_port),
                    ("ports.rcon", "RCON", rcon_port),
                    *[
                        (f"ports.bridge_{index}", f"bridge-{index}", port)
                        for index, port in enumerate(bridge_ports)
                    ],
                ]
                for name, label, port in port_entries:
                    if port_checker("127.0.0.1", port):
                        report.add(
                            name,
                            "ports",
                            PreflightStatus.FAIL,
                            f"{label} 端口 {port} 已被占用",
                        )
                    else:
                        report.add(
                            name,
                            "ports",
                            PreflightStatus.OK,
                            f"{label} 端口 {port} 空闲",
                        )
        else:
            report.add(
                "ports.config",
                "ports",
                PreflightStatus.SKIP,
                "缺少有效端口配置或 agent 数量无效",
            )

        # --- writability ---
        if episode is not None:
            for name, label, directory in (
                ("writable.record_root", "record root", recording_config.root_dir),
                ("writable.logs", "episode logs", episode.logs_dir),
            ):
                try:
                    _assert_writable(directory)
                    report.add(name, "writable", PreflightStatus.OK, str(directory))
                except Exception as exc:  # noqa: BLE001
                    report.add(name, "writable", PreflightStatus.FAIL, str(exc))
        else:
            report.add(
                "writable.record_root",
                "writable",
                PreflightStatus.SKIP,
                "记录目录未初始化",
            )
        for name, label, directory in (
            ("writable.server_dir", "server dir", server_dir),
            ("writable.runtime_root", "runtime root", ROOT / ".tmp"),
        ):
            try:
                _assert_writable(directory)
                report.add(name, "writable", PreflightStatus.OK, str(directory))
            except Exception as exc:  # noqa: BLE001
                report.add(name, "writable", PreflightStatus.FAIL, str(exc))

        # --- server start + RCON query ---
        if jar_ok:
            output("[preflight] 正在启动 Minecraft 服务器做 RCON 检查...")
            try:
                server = server_factory(
                    server_dir=server_dir,
                    log_dir=episode.logs_dir if episode is not None else None,
                )
                server.start()
                report.server_was_started = True
                response = server.send_command("list", response_timeout=10.0)
                report.add(
                    "server.rcon",
                    "server",
                    PreflightStatus.OK,
                    f"list 响应: {str(response)[:80]}",
                )
            except Exception as exc:  # noqa: BLE001
                report.add("server.rcon", "server", PreflightStatus.FAIL, str(exc))
            finally:
                if server is not None and getattr(server, "is_running", False):
                    try:
                        server.stop(grace=30)
                    except Exception as exc:  # noqa: BLE001
                        report.add("server.stop", "server", PreflightStatus.FAIL, str(exc))
        else:
            report.add(
                "server.rcon",
                "server",
                PreflightStatus.SKIP,
                "缺少 server.jar，跳过服务器启动检查",
            )

        # --- LLM protocol probe ---
        if llm_config is not None:
            try:
                client = llm_client_factory(llm_config)
                bind_recorder = getattr(client, "bind_call_recorder", None)
                if callable(bind_recorder):
                    bind_recorder(episode.llm_calls)
                registry = registry_factory()
                codec = JsonDecisionCodec()
                request = LLMRequest(
                    messages=(
                        {"role": "system", "content": codec.format_instructions()},
                        {"role": "user", "content": PROBE_USER_PROMPT},
                    ),
                    source=LLMCallSource(
                        component="preflight",
                        operation="protocol_probe",
                        task_id=spec.task_id,
                        agent_id="preflight",
                    ),
                    metadata={"kind": "preflight", "task_id": spec.task_id},
                )
                response = client.complete(request)
                proposal = codec.parse(
                    response.content,
                    agent_id="preflight",
                    registry=registry,
                )
                report.llm_probe_request_id = getattr(response, "request_id", None)
                report.add(
                    "llm.probe",
                    "llm",
                    PreflightStatus.OK,
                    f"request_id={report.llm_probe_request_id} "
                    f"action=wait.wait mode={proposal.action_unit.mode}",
                )
            except Exception as exc:  # noqa: BLE001
                report.add(
                    "llm.probe",
                    "llm",
                    PreflightStatus.FAIL,
                    _redact_text(
                        str(exc),
                        getattr(llm_config, "api_key", None),
                    ),
                )
        else:
            report.add(
                "llm.probe",
                "llm",
                PreflightStatus.SKIP,
                "LLM 配置不可用，跳过协议探测",
            )
    except KeyboardInterrupt:
        report.interrupted = True
        report.add(
            "preflight.interrupted",
            "preflight",
            PreflightStatus.FAIL,
            "收到 Ctrl+C，preflight 中断",
        )
    except Exception as exc:  # noqa: BLE001
        report.add(
            "preflight.internal",
            "preflight",
            PreflightStatus.FAIL,
            str(exc),
        )
    finally:
        if server is not None and getattr(server, "is_running", False):
            try:
                server.stop(grace=30)
            except Exception as exc:  # noqa: BLE001
                report.add("server.stop", "server", PreflightStatus.FAIL, str(exc))
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001
                report.add(
                    "llm.client_close",
                    "llm",
                    PreflightStatus.FAIL,
                    str(exc),
                )
        if episode is not None and episode.status == "started":
            try:
                episode.update_metadata(
                    preflight_summary=report.summary_dict(),
                    llm_probe_request_id=report.llm_probe_request_id,
                    server_was_started=report.server_was_started,
                )
                episode_status = (
                    "interrupted"
                    if report.interrupted
                    else "completed"
                    if not report.failed_checks
                    else "failed"
                )
                error = (
                    "; ".join(
                        f"{check.name}: {check.detail}"
                        for check in report.failed_checks[:10]
                    )
                    if report.failed_checks
                    else None
                )
                episode.finalize(
                    status=episode_status,
                    error=error,
                )
                if run_recorder is not None:
                    run_recorder.finalize(status=episode_status)
            except Exception as exc:  # noqa: BLE001
                report.add(
                    "recording.finalize",
                    "recording",
                    PreflightStatus.FAIL,
                    str(exc),
                )
        report.finished_at = monotonic()
    return report


__all__ = [
    "PreflightCheck",
    "PreflightReport",
    "PreflightStatus",
    "run_preflight",
]
