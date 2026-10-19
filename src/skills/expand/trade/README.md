# Trade Task Skill

`trade` 是 Trade 任务专用 Skill。Agent 只通过 `trade.*` 访问任务箱子；
`container.*` 和通用 `crafting.*` 不应出现在 Trade Agent 的动作清单中。

## 环境变量

- `TRADE_CONFIG_FILE`: 本局 `trade_config.yaml` 的绝对路径，只读。
- `TRADE_RUNTIME_DIR`: 本局共享运行目录，用于商铺锁和 `trade_transactions.jsonl`。
- `CHEST_REGISTRY_FILE`: 本局共享箱子注册表，由任务 Manager 在准备阶段建立。

## 动作

- `trade.inspect_own_containers`: 回到出生区域并查看自己的私人箱、商铺和背包。
- `trade.inspect_shop`: 按注册商铺名查看在售商品；金粒货款不计入在售列表。
- `trade.transfer_owned_items`: 在背包与自有私人箱/商铺之间批量取放物品。
- `trade.buy_from_shop`: 从他人商铺购买一种商品，数量默认 1。
- `trade.craft_target`: 按配置 DAG 和 Mineflayer 实际配方合成自己的目标物品。

`transfer_owned_items.items` 支持物品名、`{"name": "...", "count": 2}` 或它们的列表。
省略 `count` 时会转移来源中的全部同名物品。批量操作先完整预检，执行失败时尝试回滚。

购买在每商铺跨进程锁内执行，顺序固定为取货、付款、审计提交。付款或审计失败会先退款并退货；
若回滚不完整，返回 `purchase_inconsistent`，调用方不得把该事务视为成功。

价格、目标和商铺名称由任务 Prompt 从同一份 `trade_config.yaml` 注入；Skill 不提供查询合同动作，
但每次执行都会重新依据该配置校验 Agent、所有权、价格、目标和配方。
