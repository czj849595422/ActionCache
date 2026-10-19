"""多阶段合成贸易任务管理器。"""

from __future__ import annotations

import re
import threading
from typing import Any

from src.tasks.manager import (
    BaseTaskManager,
    EvaluatorContext,
    TaskOutcome,
    TaskStatus,
)
from src.tasks.spec import TaskSpec, TaskSpecError
from src.tasks.trade import (
    BlockPosition,
    SnbtParseError,
    TradeAgentConfig,
    TradeConfigError,
    TradeTaskConfig,
    batch_plans_equal,
    find_trade_plans,
    load_command_plan,
    load_trade_config,
    parse_chest_items,
    trade_config_to_prepare_plan,
)


_SCORE_RE = re.compile(r"\bhas(?: the following score)?[: ]+(-?\d+)")
TRADE_TIMEOUT_SECONDS = 300


class TradeTaskManager(BaseTaskManager):
    """初始化贸易箱并按四个私人箱内的目标成品判定完成。"""

    def __init__(self) -> None:
        super().__init__()
        self._server: Any = None
        self._config: TradeTaskConfig | None = None
        self._prepare_commands: list[str] = []
        self._terminal_outcome: TaskOutcome | None = None
        self._state_lock = threading.Lock()
        self._last_metrics: dict[str, Any] | None = None

    def configure(self, spec: TaskSpec) -> None:
        self._reset_runtime_state()
        super().configure(spec)

    def _validate_spec(self, spec: TaskSpec) -> None:
        if spec.category != "trade":
            raise TaskSpecError("TradeTaskManager 仅支持 trade")
        expected_names = list(("Alice", "Bob", "Carol", "Dave"))
        if int(spec.agents.get("count", 0)) != 4 or list(
            spec.agents.get("names") or []
        ) != expected_names:
            raise TaskSpecError("trade 要求 Alice 至 Dave 四名 Agent")
        success = spec.completion.get("success") or {}
        if success.get("type") != "all_personal_chests_satisfied":
            raise TaskSpecError(
                "trade 要求 completion.success.type=all_personal_chests_satisfied"
            )
        if float(spec.completion["timeout_seconds"]) != TRADE_TIMEOUT_SECONDS:
            raise TaskSpecError(
                f"内置 trade 任务时限必须为 {TRADE_TIMEOUT_SECONDS} 秒"
            )
        if spec.environment.get("world") != "trade":
            raise TaskSpecError("trade 任务必须使用 trade 存档")
        if spec.spawns:
            raise TaskSpecError("trade 不允许通过 spawns 初始化物品或实体")
        params = spec.params or {}
        config_value = params.get("trade_config_file")
        command_value = params.get("command_file")
        try:
            if config_value is None and command_value is None:
                raw_config = params.get("trade_config")
                if not isinstance(raw_config, dict):
                    raise TaskSpecError("runtime trade 需要 params.trade_config")
                config = TradeTaskConfig.from_dict(raw_config)
                verification = find_trade_plans(config)
                if not verification.feasible:
                    reasons = "; ".join(verification.failure_reasons)
                    raise TradeConfigError(f"runtime trade_config 不存在可执行解: {reasons}")
                command_plan = trade_config_to_prepare_plan(config)
            else:
                if not isinstance(config_value, str) or not config_value:
                    raise TaskSpecError("params.trade_config_file 必填")
                if not isinstance(command_value, str) or not command_value:
                    raise TaskSpecError("params.command_file 必填")
                config = load_trade_config(spec.resolve_resource(config_value))
                command_plan = load_command_plan(spec.resolve_resource(command_value))
        except (TradeConfigError, OSError, ValueError) as exc:
            raise TaskSpecError(f"无法加载 trade 资源: {exc}") from exc
        expected_plan = trade_config_to_prepare_plan(config)
        if not batch_plans_equal(expected_plan, command_plan):
            raise TaskSpecError("params.command_file 与 trade_config_file 转换结果不一致")
        if command_plan.task:
            raise TaskSpecError("trade 指令文件只能包含准备阶段命令")
        if [agent.name for agent in config.agents] != expected_names:
            raise TaskSpecError("trade_config Agent 与任务规格不一致")
        generation_seed = params.get("generation_seed")
        if not isinstance(generation_seed, int) or generation_seed != config.generation_seed:
            raise TaskSpecError("params.generation_seed 与 trade_config 不一致")
        self._config = config
        self._prepare_commands = list(command_plan.prepare)

    def prepare(self, server: Any, agents: list[Any], memory: Any) -> None:
        if self.spec is None or self._config is None:
            raise TaskSpecError("任务管理器尚未 configure")
        actual_names = self._agent_names(agents)
        expected_names = self._agent_names([])
        if actual_names != expected_names:
            raise TaskSpecError(
                f"实际 Agent 必须与任务配置一致: expected={expected_names}, actual={actual_names}"
            )
        self._server = server
        super().prepare(server, agents, memory)
        self._apply_commands(server, self._prepare_commands)
        self._verify_crafting_table(server)
        self._verify_initial_inventories(server)
        self._verify_initial_chests(server)
        self._register_chests(agents[0])

    def start(self) -> None:
        super().start()

    def completion_check(self, context: EvaluatorContext) -> TaskOutcome:
        if self.spec is None or self._config is None:
            raise TaskSpecError("任务管理器尚未 configure")
        if not self._started:
            raise TaskSpecError("任务管理器尚未 start")
        with self._state_lock:
            if self._terminal_outcome is not None:
                return self._terminal_outcome
        metrics = self._collect_metrics(context)
        self._last_metrics = metrics
        if not metrics["query_errors"] and metrics["completed_agents"] == 4:
            return self._latch(
                TaskOutcome(
                    TaskStatus.SUCCESS,
                    reason="all_personal_chests_satisfied",
                    detail=metrics,
                )
            )
        if context.elapsed_seconds() >= float(self.spec.completion["timeout_seconds"]):
            return self._latch(
                TaskOutcome(TaskStatus.TIMEOUT, reason="timeout", detail=metrics)
            )
        return TaskOutcome(TaskStatus.RUNNING, detail=metrics)

    def metrics(self, context: EvaluatorContext) -> dict[str, Any]:
        if self._last_metrics is not None:
            metrics = self._last_metrics
            self._last_metrics = None
            return dict(metrics)
        return self._collect_metrics(context)

    def _collect_metrics(self, context: EvaluatorContext) -> dict[str, Any]:
        if self._config is None:
            return {}
        query_errors: dict[str, str] = {}
        agent_metrics: dict[str, Any] = {}
        completed = 0
        for agent in self._config.agents:
            try:
                current_items = self._query_chest(context.server, agent.personal_chest)
            except (OSError, RuntimeError, SnbtParseError, ValueError) as exc:
                query_errors[agent.name] = str(exc)
                current_items = {}
            targets: dict[str, Any] = {}
            agent_complete = agent.name not in query_errors
            target_value = 0
            for target in agent.targets:
                current = int(current_items.get(target.item, 0))
                missing = max(0, target.count - current)
                price = self._config.price_map[target.item]
                target_value += price * target.count
                targets[target.item] = {
                    "required": target.count,
                    "current": current,
                    "missing": missing,
                    "unit_price": price,
                    "required_value": price * target.count,
                }
                if missing:
                    agent_complete = False
            if agent_complete:
                completed += 1
            agent_metrics[agent.name] = {
                "targets": targets,
                "target_total_value": target_value,
                "complete": agent_complete,
            }
        return {
            "agents": agent_metrics,
            "completed_agents": completed,
            "total_agents": len(self._config.agents),
            "query_errors": query_errors,
            "elapsed_seconds": max(0.0, float(context.elapsed_seconds())),
        }

    def _verify_crafting_table(self, server: Any) -> None:
        objective = "acTradeCheck"
        position = self._config.crafting_table if self._config else BlockPosition(1, 0, 0)
        try:
            server.send_command(f"scoreboard objectives remove {objective}")
            server.send_command(f"scoreboard objectives add {objective} dummy")
            server.send_command(f"scoreboard players set #table {objective} 0")
            server.send_command(
                f"execute if block {position.x} {position.y} {position.z} "
                f"minecraft:crafting_table run scoreboard players set #table {objective} 1"
            )
            response = server.send_command(
                f"scoreboard players get #table {objective}"
            )
            match = _SCORE_RE.search(str(response or ""))
            if match is None or int(match.group(1)) != 1:
                raise TaskSpecError("trade 合成台缺失: expected=(1,0,0)")
        finally:
            try:
                server.send_command(f"scoreboard objectives remove {objective}")
            except Exception:  # noqa: BLE001
                pass

    def _verify_initial_chests(self, server: Any) -> None:
        if self._config is None:
            raise TaskSpecError("trade_config 尚未加载")
        for agent in self._config.agents:
            expected = agent.initial_inventory
            try:
                actual = self._query_chest(server, agent.personal_chest)
            except (SnbtParseError, OSError, RuntimeError, ValueError) as exc:
                raise TaskSpecError(f"无法验证 {agent.personal_chest_name}: {exc}") from exc
            if actual != expected:
                raise TaskSpecError(
                    f"{agent.personal_chest_name} 初始化内容不一致: "
                    f"expected={expected}, actual={actual}"
                )
            for position in agent.shop_blocks:
                try:
                    shop_items = self._query_chest(server, position)
                except (SnbtParseError, OSError, RuntimeError, ValueError) as exc:
                    raise TaskSpecError(f"无法验证 {agent.shop_chest_name}: {exc}") from exc
                if shop_items:
                    raise TaskSpecError(f"{agent.shop_chest_name} 初始化后必须为空")

    def _verify_initial_inventories(self, server: Any) -> None:
        if self._config is None:
            raise TaskSpecError("trade_config 尚未加载")
        for agent in self._config.agents:
            expected = {
                self._config.currency_item: agent.initial_currency,
            }
            try:
                actual = self._query_inventory(server, agent.name)
            except (SnbtParseError, OSError, RuntimeError, ValueError) as exc:
                raise TaskSpecError(f"无法验证 {agent.name} 背包: {exc}") from exc
            if actual != expected:
                raise TaskSpecError(
                    f"{agent.name} 背包初始化内容不一致: "
                    f"expected={expected}, actual={actual}"
                )

    def _register_chests(self, agent: Any) -> None:
        if self._config is None:
            raise TaskSpecError("trade_config 尚未加载")
        planner = getattr(agent, "action_planner", None)
        execute_internal = getattr(planner, "execute_internal", None)
        if planner is None or not callable(execute_internal):
            raise TaskSpecError("Alice 不支持可信内部箱子注册")
        names: dict[str, str] = {}
        expected: dict[tuple[int, int, int], str] = {}
        for item in self._config.agents:
            for position, name in (
                (item.personal_chest, item.personal_chest_name),
                (item.shop_chest, item.shop_chest_name),
            ):
                key = f"{position.x},{position.y},{position.z}"
                names[key] = name
                expected[(position.x, position.y, position.z)] = name
        result = execute_internal(
            "container.register_chests",
            {"max_distance": 16, "names": names},
        )
        if not getattr(result, "ok", False):
            raise TaskSpecError(
                f"箱子注册失败: {getattr(result, 'error', None) or getattr(result, 'result', None)}"
            )
        payload = getattr(result, "result", None)
        if not isinstance(payload, dict) or not isinstance(payload.get("chests"), list):
            raise TaskSpecError("箱子注册返回缺少 chests")
        actual: dict[tuple[int, int, int], str | None] = {}
        for chest in payload["chests"]:
            if not isinstance(chest, dict) or not isinstance(chest.get("position"), dict):
                raise TaskSpecError("箱子注册返回结构非法")
            position = chest["position"]
            try:
                key = (int(position["x"]), int(position["y"]), int(position["z"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise TaskSpecError("箱子注册返回坐标非法") from exc
            actual[key] = chest.get("name")
        if actual != expected or int(payload.get("chest_count", -1)) != 8:
            raise TaskSpecError(
                f"必须精确注册中央 8 个逻辑箱子: expected={expected}, actual={actual}"
            )

    @staticmethod
    def _query_chest(server: Any, position: BlockPosition) -> dict[str, int]:
        response = server.send_command(
            f"data get block {position.x} {position.y} {position.z} Items"
        )
        return parse_chest_items(response)

    @staticmethod
    def _query_inventory(server: Any, name: str) -> dict[str, int]:
        response = server.send_command(f"data get entity {name} Inventory")
        return parse_chest_items(response)

    def _latch(self, outcome: TaskOutcome) -> TaskOutcome:
        with self._state_lock:
            if self._terminal_outcome is None:
                self._terminal_outcome = outcome
            return self._terminal_outcome

    def teardown(self) -> None:
        self._reset_runtime_state()
        super().teardown()

    def _reset_runtime_state(self) -> None:
        self._server = None
        self._config = None
        self._prepare_commands = []
        self._terminal_outcome = None
        self._last_metrics = None
