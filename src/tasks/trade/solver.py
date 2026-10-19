"""贸易配置的离线交易与合成可解性验证。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.tasks.recipe_dag import RecipeCraftStep, RecipeDAG

from .config import TradeAgentConfig, TradeTaskConfig


@dataclass(frozen=True)
class TradeTransaction:
    """一笔不可拆分、且直接来自卖方初始库存的交易。"""

    seller: str
    buyer: str
    item: str
    count: int
    unit_price: int
    total_price: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "seller": self.seller,
            "buyer": self.buyer,
            "item": self.item,
            "count": self.count,
            "unit_price": self.unit_price,
            "total_price": self.total_price,
        }


@dataclass(frozen=True)
class TradeCraftStep:
    """一名 Agent 为自己的目标执行的一组同类合成批次。"""

    agent: str
    output: str
    batches: int
    output_count: int
    ingredients: tuple[tuple[str, int], ...]
    depth: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "output": self.output,
            "batches": self.batches,
            "output_count": self.output_count,
            "ingredients": dict(self.ingredients),
            "depth": self.depth,
        }


@dataclass(frozen=True)
class TradeDependency:
    """交易间的必要依赖；当前只记录货币依赖。"""

    before: str
    after: str
    reason: str = "currency"

    def to_dict(self) -> dict[str, str]:
        return {"before": self.before, "after": self.after, "reason": self.reason}


@dataclass(frozen=True)
class TradePlan:
    """一条不含容器转移动作的交易与合成计划。"""

    transactions: tuple[TradeTransaction, ...]
    crafts: tuple[TradeCraftStep, ...]
    dependencies: tuple[TradeDependency, ...]

    @property
    def signature(self) -> tuple[Any, ...]:
        """忽略独立动作顺序后的稳定解标识。"""
        transactions = tuple(
            (
                item.seller,
                item.buyer,
                item.item,
                item.count,
                item.unit_price,
                item.total_price,
            )
            for item in self.transactions
        )
        crafts = tuple(
            (
                item.agent,
                item.output,
                item.batches,
                item.output_count,
                item.ingredients,
                item.depth,
            )
            for item in self.crafts
        )
        dependencies = tuple(
            (item.before, item.after, item.reason) for item in self.dependencies
        )
        return transactions, crafts, dependencies

    def to_dict(self) -> dict[str, Any]:
        return {
            "transactions": [item.to_dict() for item in self.transactions],
            "crafts": [item.to_dict() for item in self.crafts],
            "dependencies": [item.to_dict() for item in self.dependencies],
        }


@dataclass(frozen=True)
class TradeVerificationResult:
    """贸易配置的求解结果，最多包含三条规范化计划。"""

    feasible: bool
    plans: tuple[TradePlan, ...]
    failure_reasons: tuple[str, ...]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "plans": [plan.to_dict() for plan in self.plans],
            "failure_reasons": list(self.failure_reasons),
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True)
class _Candidate:
    seller_index: int
    buyer_index: int
    item: str
    count: int
    unit_price: int


class _TradeSolver:
    def __init__(self, config: TradeTaskConfig, max_solutions: int) -> None:
        self.config = config
        self.agents = tuple(config.agents)
        self.names = tuple(agent.name for agent in self.agents)
        self.max_solutions = min(3, max_solutions)
        recipes = config.recipe_map
        base_items = set(config.base_price_map)
        self.dag = RecipeDAG(recipes, base_items)
        self.tradeable_items = tuple(
            sorted(
                item
                for item in config.price_map
                if item != config.currency_item
                and any(item in agent.initial_inventory for agent in self.agents)
            )
        )
        self.useful_caps = tuple(
            self._useful_item_caps(agent) for agent in self.agents
        )
        self.initial_inventories = tuple(
            dict(agent.initial_inventory) for agent in self.agents
        )
        self.initial_balances = tuple(agent.initial_currency for agent in self.agents)
        self.plans: list[TradePlan] = []
        self.signatures: set[tuple[Any, ...]] = set()
        self.visited: set[tuple[Any, ...]] = set()
        self.states_explored = 0
        self.search_truncated = False
        self.max_states = 100_000

    def run(self) -> TradeVerificationResult:
        sold = tuple({} for _ in self.agents)
        self._search(
            inventories=tuple(dict(items) for items in self.initial_inventories),
            balances=self.initial_balances,
            sold=sold,
            transactions=(),
            used_edges=frozenset(),
        )
        diagnostics = self._diagnostics()
        if self.plans:
            return TradeVerificationResult(
                feasible=True,
                plans=tuple(self.plans),
                failure_reasons=(),
                diagnostics=diagnostics,
            )
        reasons = self._failure_reasons()
        if self.search_truncated:
            reasons.append("搜索状态超过上限，未能在验证预算内证明存在解")
        return TradeVerificationResult(
            feasible=False,
            plans=(),
            failure_reasons=tuple(dict.fromkeys(reasons)),
            diagnostics=diagnostics,
        )

    def _search(
        self,
        *,
        inventories: tuple[dict[str, int], ...],
        balances: tuple[int, ...],
        sold: tuple[dict[str, int], ...],
        transactions: tuple[TradeTransaction, ...],
        used_edges: frozenset[tuple[int, int, str]],
    ) -> None:
        if len(self.plans) >= self.max_solutions:
            return
        self.states_explored += 1
        if self.states_explored > self.max_states:
            self.search_truncated = True
            return

        state_key = self._state_key(inventories, balances, sold, used_edges)
        if state_key in self.visited:
            return
        self.visited.add(state_key)

        simulations = tuple(
            self.dag.simulate(agent_inventory, agent.targets)
            for agent_inventory, agent in zip(inventories, self.agents)
        )
        if all(result.feasible for result in simulations):
            plan = self._build_plan(inventories, transactions, simulations)
            if self._is_minimal_plan(plan, inventories):
                signature = plan.signature
                if signature not in self.signatures:
                    self.signatures.add(signature)
                    self.plans.append(plan)
            return

        candidates: list[_Candidate] = []
        for buyer_index, simulation in enumerate(simulations):
            if simulation.feasible:
                continue
            candidates.extend(
                self._candidates_for_buyer(
                    buyer_index,
                    inventories,
                    balances,
                    sold,
                    used_edges,
                    simulation,
                )
            )

        # A transaction must be executable at the point where it is selected.
        # Trying every buyer here also permits a sale to fund a later purchase.
        candidates.sort(key=self._candidate_sort_key)
        for candidate in candidates:
            if len(self.plans) >= self.max_solutions:
                return
            edge = (candidate.seller_index, candidate.buyer_index, candidate.item)
            seller_stock = (
                self.initial_inventories[candidate.seller_index].get(candidate.item, 0)
                - sold[candidate.seller_index].get(candidate.item, 0)
            )
            if seller_stock < candidate.count:
                continue
            total_price = candidate.count * candidate.unit_price
            if balances[candidate.buyer_index] < total_price:
                continue

            next_inventories = [dict(items) for items in inventories]
            next_inventories[candidate.seller_index][candidate.item] = (
                next_inventories[candidate.seller_index].get(candidate.item, 0)
                - candidate.count
            )
            next_inventories[candidate.buyer_index][candidate.item] = (
                next_inventories[candidate.buyer_index].get(candidate.item, 0)
                + candidate.count
            )
            next_balances = list(balances)
            next_balances[candidate.buyer_index] -= total_price
            next_balances[candidate.seller_index] += total_price
            next_sold = [dict(items) for items in sold]
            next_sold[candidate.seller_index][candidate.item] = (
                next_sold[candidate.seller_index].get(candidate.item, 0)
                + candidate.count
            )
            next_transaction = TradeTransaction(
                seller=self.names[candidate.seller_index],
                buyer=self.names[candidate.buyer_index],
                item=candidate.item,
                count=candidate.count,
                unit_price=candidate.unit_price,
                total_price=total_price,
            )
            self._search(
                inventories=tuple(next_inventories),
                balances=tuple(next_balances),
                sold=tuple(next_sold),
                transactions=transactions + (next_transaction,),
                used_edges=used_edges | {edge},
            )

    def _candidates_for_buyer(
        self,
        buyer_index: int,
        inventories: tuple[dict[str, int], ...],
        balances: tuple[int, ...],
        sold: tuple[dict[str, int], ...],
        used_edges: frozenset[tuple[int, int, str]],
        simulation: Any,
    ) -> list[_Candidate]:
        buyer_inventory = inventories[buyer_index]
        before_missing = simulation.missing_item_map
        candidates: list[_Candidate] = []
        for item in self.tradeable_items:
            if self.useful_caps[buyer_index].get(item, 0) <= 0:
                continue
            for seller_index in range(len(self.agents)):
                if seller_index == buyer_index:
                    continue
                edge = (seller_index, buyer_index, item)
                if edge in used_edges:
                    continue
                available = (
                    self.initial_inventories[seller_index].get(item, 0)
                    - sold[seller_index].get(item, 0)
                )
                if available <= 0:
                    continue
                max_count = min(self.useful_caps[buyer_index][item], available)
                if max_count <= 0:
                    continue
                for count in range(1, max_count + 1):
                    after_inventory = dict(buyer_inventory)
                    after_inventory[item] = after_inventory.get(item, 0) + count
                    after = self.dag.simulate(
                        after_inventory, self.agents[buyer_index].targets
                    )
                    if not self._makes_progress(before_missing, after):
                        continue
                    price = self.config.price_map[item]
                    candidates.append(
                        _Candidate(
                            seller_index=seller_index,
                            buyer_index=buyer_index,
                            item=item,
                            count=count,
                            unit_price=price,
                        )
                    )
        return candidates

    @staticmethod
    def _makes_progress(before: Mapping[str, int], after: Any) -> bool:
        if after.feasible:
            return True
        after_missing = after.missing_item_map
        if after_missing == dict(before):
            return False
        return _missing_score(after_missing) <= _missing_score(before)

    def _build_plan(
        self,
        inventories: tuple[dict[str, int], ...],
        transactions: tuple[TradeTransaction, ...],
        simulations: tuple[Any, ...],
    ) -> TradePlan:
        merged: dict[tuple[str, str, str], TradeTransaction] = {}
        for transaction in transactions:
            key = (transaction.seller, transaction.buyer, transaction.item)
            previous = merged.get(key)
            if previous is None:
                merged[key] = transaction
            else:
                count = previous.count + transaction.count
                merged[key] = TradeTransaction(
                    seller=previous.seller,
                    buyer=previous.buyer,
                    item=previous.item,
                    count=count,
                    unit_price=previous.unit_price,
                    total_price=count * previous.unit_price,
                )
        normalized_transactions = tuple(
            sorted(
                merged.values(),
                key=lambda item: (item.seller, item.buyer, item.item),
            )
        )
        crafts: list[TradeCraftStep] = []
        for agent, simulation in zip(self.agents, simulations):
            combined: dict[str, TradeCraftStep] = {}
            for step in simulation.steps:
                current = combined.get(step.output)
                if current is None:
                    combined[step.output] = self._craft_step(agent.name, step)
                else:
                    combined[step.output] = TradeCraftStep(
                        agent=current.agent,
                        output=current.output,
                        batches=current.batches + step.batches,
                        output_count=current.output_count,
                        ingredients=current.ingredients,
                        depth=current.depth,
                    )
            crafts.extend(combined.values())
        normalized_crafts = tuple(
            sorted(crafts, key=lambda item: (item.agent, item.depth, item.output))
        )
        dependencies = self._currency_dependencies(normalized_transactions)
        return TradePlan(normalized_transactions, normalized_crafts, dependencies)

    @staticmethod
    def _craft_step(agent: str, step: RecipeCraftStep) -> TradeCraftStep:
        return TradeCraftStep(
            agent=agent,
            output=step.output,
            batches=step.batches,
            output_count=step.output_count,
            ingredients=step.ingredients,
            depth=step.depth,
        )

    def _currency_dependencies(
        self, transactions: tuple[TradeTransaction, ...]
    ) -> tuple[TradeDependency, ...]:
        """Derive one deterministic executable order and its funding edges."""
        index_by_name = {name: index for index, name in enumerate(self.names)}
        funds: list[list[tuple[str | None, int]]] = [
            [(None, balance)] for balance in self.initial_balances
        ]
        pending = list(transactions)
        ordered: list[TradeTransaction] = []
        dependencies: list[TradeDependency] = []
        while pending:
            selected_index = next(
                (
                    index
                    for index, transaction in enumerate(pending)
                    if sum(amount for _, amount in funds[index_by_name[transaction.buyer]])
                    >= transaction.total_price
                ),
                None,
            )
            if selected_index is None:
                # The search only accepts executable transitions, so this is
                # defensive. It keeps malformed plans from being emitted.
                return ()
            transaction = pending.pop(selected_index)
            buyer_index = index_by_name[transaction.buyer]
            seller_index = index_by_name[transaction.seller]
            remaining_payment = transaction.total_price
            consumed: list[str] = []
            buyer_funds = funds[buyer_index]
            while remaining_payment > 0:
                source, amount = buyer_funds[0]
                consumed_amount = min(amount, remaining_payment)
                remaining_payment -= consumed_amount
                if source is not None:
                    consumed.append(source)
                if consumed_amount == amount:
                    buyer_funds.pop(0)
                else:
                    buyer_funds[0] = (source, amount - consumed_amount)
            dependencies.extend(
                TradeDependency(
                    before=source,
                    after=self._transaction_id(transaction),
                )
                for source in consumed
            )
            funds[seller_index].append(
                (self._transaction_id(transaction), transaction.total_price)
            )
            ordered.append(transaction)
        return tuple(sorted(set(dependencies), key=lambda item: (item.before, item.after)))

    @staticmethod
    def _transaction_id(transaction: TradeTransaction) -> str:
        return f"{transaction.seller}->{transaction.buyer}:{transaction.item}"

    def _is_minimal_plan(
        self, plan: TradePlan, inventories: tuple[dict[str, int], ...]
    ) -> bool:
        """Reject purchases that can be reduced without losing completion."""
        for transaction in plan.transactions:
            reduced = list(inventories)
            buyer_index = self.names.index(transaction.buyer)
            seller_index = self.names.index(transaction.seller)
            reduced[seller_index] = dict(reduced[seller_index])
            reduced[buyer_index] = dict(reduced[buyer_index])
            # ``inventories`` is the post-transaction state. Returning one unit
            # to the seller and removing it from the buyer models a smaller
            # aggregate transaction without replaying container operations.
            reduced[buyer_index][transaction.item] -= 1
            reduced[seller_index][transaction.item] += 1
            if all(
                self.dag.can_produce(items, agent.targets)
                for items, agent in zip(reduced, self.agents)
            ):
                return False
        return True

    def _useful_item_caps(self, agent: TradeAgentConfig) -> dict[str, int]:
        caps: dict[str, int] = {}

        def expand(item: str, amount: int) -> None:
            caps[item] = caps.get(item, 0) + amount
            recipe = self.config.recipe_map.get(item)
            if recipe is None:
                return
            batches = (amount + recipe.output_count - 1) // recipe.output_count
            for ingredient, count in recipe.ingredients:
                expand(ingredient, count * batches)

        for target in agent.targets:
            expand(target.item, target.count)
        return caps

    @staticmethod
    def _candidate_sort_key(candidate: _Candidate) -> tuple[Any, ...]:
        return (
            candidate.buyer_index,
            candidate.item,
            -candidate.count,
            candidate.seller_index,
        )

    @staticmethod
    def _state_key(
        inventories: tuple[dict[str, int], ...],
        balances: tuple[int, ...],
        sold: tuple[dict[str, int], ...],
        used_edges: frozenset[tuple[int, int, str]],
    ) -> tuple[Any, ...]:
        return (
            tuple(tuple(sorted(items.items())) for items in inventories),
            balances,
            tuple(tuple(sorted(items.items())) for items in sold),
            tuple(sorted(used_edges)),
        )

    def _diagnostics(self) -> dict[str, Any]:
        return {
            "states_explored": self.states_explored,
            "visited_states": len(self.visited),
            "search_truncated": self.search_truncated,
            "max_solutions": self.max_solutions,
            "tradeable_items": list(self.tradeable_items),
            "initial_balances": dict(zip(self.names, self.initial_balances)),
            "initial_supply": {
                item: sum(inventory.get(item, 0) for inventory in self.initial_inventories)
                for item in self.tradeable_items
            },
        }

    def _failure_reasons(self) -> list[str]:
        reasons: list[str] = []
        for agent, inventory in zip(self.agents, self.initial_inventories):
            simulation = self.dag.simulate(inventory, agent.targets)
            if simulation.feasible:
                continue
            for item, count in simulation.missing_items:
                supply = sum(
                    initial.get(item, 0)
                    for index, initial in enumerate(self.initial_inventories)
                    if self.names[index] != agent.name
                )
                if supply < count:
                    reasons.append(
                        f"{agent.name} 缺少 {item} {count}，其他 Agent 初始库存仅有 {supply}"
                    )
            reasons.append(f"{agent.name} 的初始库存不能直接完成个人目标")
        if not reasons:
            reasons.append("不存在满足交易、货币和个人合成约束的计划")
        return reasons


def find_trade_plans(
    config: TradeTaskConfig,
    max_solutions: int = 3,
) -> TradeVerificationResult:
    """查找最多三条不同的贸易与合成解。"""
    if not isinstance(max_solutions, int) or isinstance(max_solutions, bool):
        raise TypeError("max_solutions 必须是整数")
    if max_solutions < 1:
        raise ValueError("max_solutions 必须大于 0")
    return _TradeSolver(config, min(3, max_solutions)).run()


def _missing_score(value: Mapping[str, int]) -> tuple[int, int]:
    return sum(value.values()), len(value)


__all__ = [
    "TradeCraftStep",
    "TradeDependency",
    "TradePlan",
    "TradeTransaction",
    "TradeVerificationResult",
    "find_trade_plans",
]
