"""统一 Debug Runner 的终端 REPL。"""

from __future__ import annotations

import cmd
import json
from typing import Any

from .controller import DebugController
from .schemas import DebugResult, to_jsonable


class DebugRepl(cmd.Cmd):
    intro = (
        "交互式任务环境已启动。输入 help 查看命令；输入 exit 或 Ctrl+C 清理现场。"
    )
    prompt = "debug> "

    def __init__(
        self,
        controller: DebugController,
        *,
        output_limit: int = 6000,
        stdout=None,
    ) -> None:
        super().__init__(stdout=stdout)
        if output_limit <= 0:
            raise ValueError("output_limit must be positive")
        self.controller = controller
        self.output_limit = output_limit
        self.exit_reason: str | None = None

    def parseline(self, line: str):
        """RCON 参数不经 strip，保留命令本身的原始空格。"""
        if line.startswith("rcon") and (
            len(line) == 4 or line[4].isspace()
        ):
            argument = line[5:] if len(line) > 4 else ""
            return "rcon", argument, line
        return super().parseline(line)

    def emptyline(self) -> bool | None:
        return None

    def default(self, line: str) -> None:
        self._emit(
            self.controller.error_result(
                "repl",
                "unknown_command",
                f"Unknown debug command: {line}",
                request={"line": line},
            )
        )

    def do_action(self, arg: str) -> None:
        """action list/run/start/update/stop/status ..."""
        parts = arg.split(maxsplit=3)
        if not parts:
            return self._syntax("action", "Missing action subcommand", arg)
        operation = parts[0]
        try:
            if operation == "list" and len(parts) == 2:
                result = self.controller.execute_action(parts[1], "list")
            elif operation in {"run", "start"} and len(parts) >= 3:
                if "(" in parts[2]:
                    if len(parts) == 4:
                        return self._syntax(
                            "action",
                            "Shorthand action does not accept separate JSON params",
                            arg,
                        )
                    result = self.controller.execute_action(
                        parts[1],
                        operation,
                        action=parts[2],
                        params={},
                    )
                    self._emit(result)
                    return
                params = _json_object(parts[3] if len(parts) == 4 else "")
                result = self.controller.execute_action(
                    parts[1],
                    operation,
                    action=parts[2],
                    params=params,
                )
            elif operation == "update" and len(parts) >= 3:
                params = _json_object(parts[3] if len(parts) == 4 else "")
                result = self.controller.execute_action(
                    parts[1],
                    "update",
                    action_id=parts[2],
                    params=params,
                )
            elif operation == "stop" and len(parts) >= 3:
                result = self.controller.execute_action(
                    parts[1],
                    "stop",
                    action_id=parts[2],
                    reason=parts[3] if len(parts) == 4 else None,
                )
            elif operation == "status" and len(parts) in {2, 3}:
                result = self.controller.execute_action(
                    parts[1],
                    "status",
                    action_id=parts[2] if len(parts) == 3 else None,
                )
            else:
                return self._syntax("action", "Invalid action command", arg)
        except ValueError as exc:
            return self._syntax("action", str(exc), arg)
        self._emit(result)

    def do_rcon(self, arg: str) -> None:
        """rcon <完整 Minecraft 指令>"""
        self._emit(self.controller.send_server_command(arg))

    def do_observe(self, arg: str) -> None:
        """observe agent <agent> [JSON] | observe all [JSON]"""
        parts = arg.split(maxsplit=2)
        if not parts:
            return self._syntax("observe", "Missing observe subcommand", arg)
        try:
            if parts[0] == "all":
                _, _, options_text = arg.partition(" ")
                options = _json_object(options_text)
                result = self.controller.observe_all(options)
            elif parts[0] == "agent" and len(parts) >= 2:
                options = _json_object(parts[2] if len(parts) == 3 else "")
                result = self.controller.observe_agent(parts[1], options)
            else:
                return self._syntax("observe", "Invalid observe command", arg)
        except ValueError as exc:
            return self._syntax("observe", str(exc), arg)
        self._emit(result)

    def do_prompt(self, arg: str) -> None:
        """prompt list [agent] | prompt show <agent> <provider.method>"""
        parts = arg.split()
        if parts and parts[0] == "list" and len(parts) in {1, 2}:
            self._emit(
                self.controller.list_prompt_methods(
                    parts[1] if len(parts) == 2 else None
                )
            )
            return
        if len(parts) == 3 and parts[0] == "show":
            self._emit(self.controller.build_prompt(parts[1], parts[2]))
            return
        self._syntax("prompt", "Invalid prompt command", arg)

    def do_inspect(self, arg: str) -> None:
        """inspect list | inspect show <component> [JSON]"""
        parts = arg.split(maxsplit=2)
        if parts == ["list"]:
            self._emit(self.controller.list_components())
            return
        if len(parts) >= 2 and parts[0] == "show":
            try:
                params = _json_object(parts[2] if len(parts) == 3 else "")
            except ValueError as exc:
                return self._syntax("inspect", str(exc), arg)
            self._emit(self.controller.inspect_component(parts[1], params))
            return
        self._syntax("inspect", "Invalid inspect command", arg)

    def do_status(self, arg: str) -> None:
        """显示环境、服务器和 monitor 状态。"""
        if arg.strip():
            return self._syntax("status", "status does not accept arguments", arg)
        self._emit(self.controller.status())

    def do_metrics(self, arg: str) -> None:
        """显示最近一次任务指标。"""
        if arg.strip():
            return self._syntax("metrics", "metrics does not accept arguments", arg)
        self._emit(self.controller.metrics())

    def do_outcome(self, arg: str) -> None:
        """显示已锁定的任务终态。"""
        if arg.strip():
            return self._syntax("outcome", "outcome does not accept arguments", arg)
        self._emit(self.controller.outcome())

    def do_history(self, arg: str) -> None:
        """history [数量]"""
        try:
            count = int(arg.strip()) if arg.strip() else 20
            if count <= 0:
                raise ValueError("history count must be positive")
        except ValueError as exc:
            return self._syntax("history", str(exc), arg)
        self._emit(self.controller.history(count))

    def do_show(self, arg: str) -> None:
        """show last：显示上一次 Controller 结果的完整内容。"""
        if arg.strip() != "last":
            return self._syntax("show", "Expected: show last", arg)
        last = self.controller.last_result()
        if last is None:
            self._emit(
                self.controller.error_result(
                    "show.last",
                    "last_result_unavailable",
                    "No debug result has been produced yet",
                )
            )
            return
        self._write_json(last.to_dict())

    def do_exit(self, arg: str) -> bool:
        """退出并清理任务环境。"""
        self.exit_reason = "exit"
        return True

    def do_quit(self, arg: str) -> bool:
        """exit 的别名。"""
        return self.do_exit(arg)

    def do_EOF(self, arg: str) -> bool:
        self.stdout.write("\n")
        self.exit_reason = "eof"
        return True

    def _syntax(self, operation: str, message: str, raw: str) -> None:
        self._emit(
            self.controller.error_result(
                operation,
                "invalid_syntax",
                message,
                request={"raw": raw},
            )
        )

    def _emit(self, result: DebugResult) -> None:
        payload = result.to_dict()
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        if len(encoded) <= self.output_limit:
            self.stdout.write(encoded + "\n")
            return
        summary = {
            "ok": result.ok,
            "operation": result.operation,
            "timestamp": result.timestamp,
            "error": to_jsonable(result.error),
            "artifact_path": result.artifact_path,
            "result_summary": _summary(result.result),
            "note": "结果过大；使用 show last 查看完整内容",
        }
        self._write_json(summary)

    def _write_json(self, payload: Any) -> None:
        self.stdout.write(
            json.dumps(
                to_jsonable(payload),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n"
        )


def _json_object(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON parameters must be an object")
    return value


def _summary(value: Any) -> Any:
    normalized = to_jsonable(value)
    if isinstance(normalized, dict):
        return {"type": "object", "keys": list(normalized)[:20], "size": len(normalized)}
    if isinstance(normalized, list):
        return {"type": "array", "size": len(normalized)}
    return str(normalized)[:240]
