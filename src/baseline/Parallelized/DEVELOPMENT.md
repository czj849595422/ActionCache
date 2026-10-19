# PPA（Parallelized Planning-Acting）迁移开发文档

> 目录：`src/baseline/Parallelized/` | 更新：2026-08-13
> 依据论文：`src/baseline/Parallelized/ParallelizedPlanning-Acting.pdf`
> （《Parallelized Planning-Acting for Multi-Agent LLM Systems in Minecraft》，AAMAS 2026，arXiv:2503.03505v2）
> 状态：任务环境集成型、论文 JSON 协议版 `PPARunner` 已实现；下文差距分析与阶段划分保留为历史迁移规划。

## 0. 当前实现（2026-08-11）

- `schemas.py`：`PPAConfig`（含 max_run_seconds、max_llm_calls、max_llm_calls_per_agent）、`PPAProposal`（含 request_id）、`PPARunResult`（含 stop_reason）。
- `text_protocol.py`：论文一致的 JSON 决策协议 `JsonDecisionCodec`
  （`{"skill": "...", "interrupt": true/false, "reason": "..."}`，兼容 ```json 代码块；
  skill 支持简写指令或 `<skill>.<command>`，参数经 manifest schema 与 allowlist 校验）。
- 旧四行文本协议（`FunctionCallTextCodec`/`SimpleInstructionCodec`）已抽离到
  `src/agents/action/text_protocol.py`（返回通用 `TextDecision`），PPA 不再使用，保留供其他方法复用。
- `prompts.py`：任务、身份、动态技能签名、文本观测、团队消息、执行/计划/结果状态的纯文本 prompt。
- prompt 模板：`src/baseline/Parallelized/prompts/system.txt` 与 `user.txt`（可直接修改；
  可放 `system_en.txt`/`user_en.txt` 提供英文版，缺省回退中文；占位符见文件内 `{...}`）。
- 任务描述：各任务文件夹下 `<task_id>.description.txt` 保存多行任务描述文本
  （如 `src/tasks/mining/mining.description.txt`），非 PPA 专属；
  `TaskSpec.description_file` 定位、`load_description_text()` 读取，
  `PPAPromptBuilder` 仅注入该 description 到 system 的【任务】段，不回退任务 YAML 的 description。
- 简单指令层：`src/agents/action/simple_instructions.json` + `SimpleInstructionRegistry` + `JsonDecisionCodec`；LLM 输出简写动作 + 重要参数，模块映射回规范动作并填充默认值；prompt 动作空间渲染为 `- <简写>(<参数>, ...) : #<说明>`（映射表见 `docs/ppa_simple_instructions.md`）。
- 简写动作通用模块：`src/agents/action/simple_instructions.py` 集中提供映射/展开（`SimpleInstructionRegistry.expand`）、动作调用解析（`parse_action_call`）与动作空间渲染（`render_action_space`，支持中英文与 `item, num, ...` 不定长形式），可供所有方法复用；PPA 的 `JsonDecisionCodec` 只是 JSON 决策协议的薄封装。
- `workers.py`：每 Agent 的 Observation/Planning/Acting worker，单槽覆盖、锁存中断、continuous 真 stop、oneshot deferred。
- 被动式通信：动作终态后 `ActingWorker` 把"执行f(...) 完成/中断/失败"反馈写入记忆 `result` 通道，
  并在 `broadcast_results` 开启时经注入的 `broadcast` 回调 RCON 广播到服务器；
  `PlanningWorker` 在本轮规划前检测到新结果时调用 `PPAPromptBuilder.build_passive()`（模板 `prompts/passive_system.txt`/`passive_user.txt`），
  输出纯文本写入 `passive` 通道；通信调用计入 LLM 预算并按 request_id 记录。
- `budget.py`：线程安全 `RunBudget`（团队/每 Agent LLM 调用预算，尝试调用计数）。
- `runner.py`：直接组装 `TaskEnvironmentSession`、`TeamMemory` 与三个 worker，不嵌套 `TeamRunner`；实现启动、轮询、逆序回滚、幂等关闭、watchdog/预算停止与 stop_reason 统一终止路径。
- 统一准备期：`TaskEnvironmentSession` 在 `manager.prepare`（基线+初始状态）之后固定等待
  `prepare_seconds=10`（所有任务统一，LLM 静默等待开始），准备期结束后才调用
  `manager.spawn_initial_entities`（召唤 spec.spawns 初始实体，如 Boss）再 `manager.start()`
  （任务计时与调度开始，monster_clearance 的初始怪物随调度 t=0 召唤）；测试可传 0 关闭。
- 轨迹关联：proposal/action 事件携带 request_id 与 buffer_version；`action.status` 记录每次执行轮询；`cleanup.*` 记录逐项清理审计。
- `preflight.py`：`--preflight` 全链路 live 预检（工具链/文件配置/世界/端口/可写性/服务器+RCON/LLM 协议探测），collect-all 报告、脱敏 artifact、只读世界检查（复用 `TaskWorldWorkspace.check_disposable()`）。
- `script/demo_parallelized.py`：正常运行入口、不启动服务器不调用模型的 `--check`、`--preflight` live 预检，以及 `--max-run-seconds`/`--max-llm-calls`/`--max-llm-calls-per-agent`/`--planning-interval` 运行控制。
- `test/test_ppa_*.py`：codec、prompt、worker、runner 及两 Agent 中断验收测试。
- 感知范围：Agent 观测默认按任务 `environment.observation_range` 半径感知（默认 20 米，`TaskSpec.observation_range`），与容器/处理器操作范围 `use_range=4.5` 相互独立，互不影响。

当前边界：模型输出采用论文 JSON 决策协议，内部仍保留 dataclass/dict/`ActionUnit`；未实现论文递归 `obtainItem`、新 benchmark、PVP 判定或外部玩家聊天接入。真实 Minecraft + LLM 尚未作为本次交付的一部分运行。

---

## 1. 背景与目标

### 1.1 论文方法概要

论文提出一种面向 LLM 多智能体的“并行化规划-执行”框架，核心是：

1. **双线程架构 + 可中断执行**：每个智能体独立运行规划线程（LLM 推理）与执行线程（技能执行），两条线程仅通过一个**单槽动作缓冲区**通信；缓冲区保存最新规划，规划器可通过中断标志（`interrupt`）让执行线程中止当前动作并切换到新动作。
2. **集中式记忆系统**：团队级统一存储观测记录、聊天日志、动作历史，支持被动通信（规划后自动写聊天）与主动通信（执行中通过聊天技能发消息），消除记忆共享延迟。
3. **综合技能库 + 递归任务分解（DAG）**：把物品依赖建模为带权 DAG，高层需求（如 `obtainItem`）由固定机制自动解析为前置任务，LLM 只需对高层任务做一次调用。

### 1.2 迁移目标

- **动作系统替换**：论文的技能库由本项目动作系统（`BaseActionPlanner` + `src/skills/*/skill.json`）替代；LLM 输出直接为 `<skill>.<command>` + params。
- **功能解耦**：通用能力（LLM 客户端、集中式记忆、并发运行时、任务图解析）放在 `src/`，供后续其他方法（串行框架、集中式/去中心化框架、其他基线）复用。
- **方法适配隔离**：PPA 特有的编排（规划循环、执行循环、提示词、任务配置）放在 `src/baseline/parallelized/`。
- **工程约束**：服务器持续运行不暂停；LLM 密钥走环境变量；测试放 `test/`，demo 放 `script/`（遵循 AGENTS.md 约定）。

---

## 2. 现状与差距分析

### 2.1 可复用清单（已存在）

| 能力 | 现有位置 | 说明 |
| --- | --- | --- |
| 服务器管理 | `src/launcher/server.py`（`Server`：start/stop/send_command，RCON 优先、stdin 兜底） | 持续运行，符合论文前提 |
| 单 agent 动作执行 | `src/agents/skill_agent.py`（`SkillAgent`：每 agent 一个 bridge 进程、独占端口）+ `src/agents/action/planner.py`（`BaseActionPlanner`：execute/start/update/stop/interrupt/status/list_skills） | 动作系统主入口 |
| 技能注册表 | `src/agents/action/registry.py`（`SkillRegistry`/`SkillManifest`/`SkillCommandSpec`，读取 `src/skills/*/skill.json`） | manifest 含 modes/locks/switch_group/mutable_params/params_schema/llm_description |
| 冲突管理 | `src/agents/action/conflict.py`（`ConflictManager`：锁、切换组、抢占判定） | 单 agent 内动作互斥 |
| 结果后处理 | `src/agents/action/result_postprocessor.py`（动作结果 → LLM 可读文本） | 供记忆/提示词复用 |
| 协议结构 | `src/interface/schemas.py`（`AgentIdentity`/`PerceptionSnapshot`/`ActionUnit`/`ActionResultEnvelope`/`LLMSemanticUnit` 等） | LLM 语义单元可直接承载 `{skill, params, reason}` |
| 感知 | `src/perception/base_perception.py`（`BasePerception`，桥 `/perception/raw`）+ `src/perception/verbalizer/`（文本化，含 `TeamStatusRule`） | 观测轮询与观测文本化 |
| 子进程管理 | `src/launcher/utils.py`（`ManagedProcess`，threading + ready/fail 事件） | 已有线程化基础设施 |
| 测试约定 | `test/test_*.py`（unittest 风格） | 新增测试沿用 |

现有技能命令（`src/skills/*/skill.json`）：

| 技能 | 命令 |
| --- | --- |
| block | inspect, break, place, inspect_region, break_region, place_region |
| combat | patrol, attack, fight_using_weapon, fight_using_bow, fight, use_food |
| container | inspect_chest, pick_object_from_chest, place_object_to_chest |
| crafting | query_recipe, craft_with_recipe, craft_item |
| equipment | equip_item, unequip_item, auto_equip_combat |
| furnace | find_nearby, open, put_fuel, take_fuel, put_input, take_input, take_output, close |
| inventory | list, inspect, count_item, has_item, equip |
| navigation | goto_position, follow_position, follow_entity, goto_block |
| survival | use_food |
| wait | wait |

### 2.2 缺口清单（需开发）

| 缺口 | 归属层 | 对应论文组件 |
| --- | --- | --- |
| LLM 客户端与输出解析 | `src/llm/` | 规划线程的 LLM 调用 |
| 团队集中式记忆 | `src/memory/` | 集中式记忆系统（CMS） |
| 单槽动作缓冲区 | `src/runtime/buffer.py` | 单槽动作缓冲区 |
| 后台循环 worker | `src/runtime/worker.py` | 规划线程 / 执行线程 |
| 多 agent 生命周期编排 | `src/runtime/team_runner.py` | 多智能体系统启动/清理 |
| 任务 DAG 与递归分解 | `src/tasks/` | 技能库递归任务分解（RTDM） |
| 聊天技能（主动通信） | `src/skills/communication/` | chatMessage 技能 |
| 探索技能 | `src/skills/navigation/`（新增 explore 命令） | mineItem 的 explore 参数 |
| 长动作可中断性 | `src/env/mineflayer/`（bridge/runtime）或技能改造 | 中断机制落地 |
| PPA 编排（提示词/规划循环/执行循环/agent/team/基准） | `src/baseline/parallelized/` | 并行化规划-执行框架（PPA） |

---

## 3. 总体设计

### 3.1 运行流（对齐论文 Algorithm 1）

```
                     团队集中式记忆 src/memory（观测/聊天/动作历史）
                          ^  轮询刷新         ^  读写
                          |                   |
  规划线程(每 agent)      |                   |      执行线程(每 agent)
  ┌────────────────────┐ |                   | ┌────────────────────┐
  │ 感知 → 记忆 → LLM  │ |                   | │ 轮询 buffer.has_new │
  │ → 解析 {skill,      │ ── put(覆盖) ───────┼─▶ 有变更?            │
  │   params, interrupt}│                   │ │   stop(active)     │
  │ → interrupt? 通知   │                   │ │  execute/start      │
  │ → 被动通信写聊天    │                   │ │  status 轮询         │
  └────────────────────┘                   │ └────────┬───────────┘
        LLMClient(src/llm)                 │          │ 动作结果写回记忆
        BaseActionPlanner(已有) ◀───────────┘          ▼
        SkillAgent/bridge(已有) ──▶ Minecraft(不暂停)  ActionResultEnvelope
```

### 3.2 分层原则

- `src/` 下共享模块**不依赖** PPA 或任何具体方法；只依赖 `src/interface` 协议结构与 `src/agents/action` 等既有基础。
- `src/baseline/parallelized/` 只做编排，不重复实现底座；它把共享组件“组装”成论文方法。
- 数据流方向：LLM 输出 → `LLMSemanticUnit`（解析）→ `ActionUnit`（校验）→ `BaseActionPlanner`（执行）→ `ActionResultEnvelope`（回写记忆）。

---

## 4. 共享模块设计（`src/`）

### 4.1 `src/llm/` — LLM 客户端与输出解析

职责：统一的模型调用与 LLM 输出解析，供所有方法使用。

建议文件：

- `config.py`：`LLMConfig`，从环境变量读取 API base/model/key，不落盘、不进日志。
- `client.py`：`LLMClient.complete(messages, *, json_mode, temperature, timeout) -> LLMResponse`；含超时/重试/错误分类。
- `parsing.py`：`parse_llm_action(text) -> LLMSemanticUnit`：提取 JSON、容错修复、强校验 `{skill, params, interrupt, reason}`。
- `prompts.py`：通用 prompt 工具（技能清单渲染、JSON 输出格式约束），PPA 专属模板不放这里。

复用性：串行框架、集中式/去中心化框架、其他基线均需 LLM 调用与 JSON 解析。

### 4.2 `src/memory/` — 团队集中式记忆

职责：线程安全的团队级记忆（观测、聊天、动作历史），对应论文 CMS。

建议文件：

- `memory.py`：`TeamMemory`（`RLock` 保护），聚合三个子存储：
  - `ObservationStore`：每 agent 每秒轮询刷新（复用 `BasePerception.perceive_snapshot()`），只保留最新；
  - `ChatLogStore`：追加式，支持 `recent(n)`（聊天窗口，默认=agent 数，控制 token）；
  - `ActionHistoryStore`：每 agent 动作 + `ActionResultEnvelope`（结果文本用 `result_postprocessor` 生成）。
- `export.py`：JSON 快照导出/导入（对齐论文附录存储形态），供调试与离线复现。
- 身份信息复用 `AgentIdentity`（name/team/teammates）。

复用性：任何多智能体方法都需要共享状态；与 PPA 无关。

### 4.3 `src/runtime/` — 并发运行时底座

职责：可启动/可停止的后台循环与消息通道。

建议文件：

- `buffer.py`：`SingleSlotOverwriteBuffer`：
  - `put(item) -> version`：覆盖旧值，返回自增版本号；
  - `peek()` / `has_new(seen_version)` / `get()`：执行线程据此区分“有新动作”与“同一动作”，避免重复执行；
  - 线程安全（Lock/Condition）。
- `worker.py`：`LoopWorker`：`start()/stop()/join()`、`stop_event`、异常回调、心跳日志；规划循环与执行循环均继承它。沿用项目现有 threading + daemon 线程风格（与 `ManagedProcess` 一致）。
- `team_runner.py`：`TeamRunner`：启动 `Server` → 为每个 agent 创建 `SkillAgent`（独立 `bridge_port`）→ `start_bridge`/`connect` → 启动方法传入的 loops → 等待完成判定 → 清理（disconnect/stop）。

复用性：串行方法可只用 worker 做“规划-执行交替”；去中心化方法可用 buffer/memory 做消息交换。

### 4.4 `src/tasks/` — 任务图与递归分解

职责：把论文“带权 DAG + 递归解析”通用化，输出**本项目动作序列**。

建议文件：

- `dag.py`：加权 DAG（顶点=原子任务 `(item, count, method)`，边=前驱依赖，边权=转换率 `r_ij/n_out`）；实现递归展开 `Ψ(v_i)` 与数量换算。
- `data/items.json`：物品依赖知识（合成配方、前置工具、烧炼原料、采集方式、产出数量），纯数据、可增量维护。
- `resolver.py`：`resolve_obtain(item, count, inventory) -> (plan: list[ActionUnit], missing)`：展开为 `combat.attack`/`block.break`/`furnace.*`/`crafting.craft_item` 等项目动作；背包不足时返回缺失清单。

复用性：串行基线同样需要“高层需求 → 动作序列”；固定机制解析比 LLM 逐步规划更省 token、更少误差传播（论文论证）。

### 4.5 `src/skills/` — 必要的动作补丁

1. `communication/send_message`：封装 `bot.chat()` 的正式技能命令，执行线程调用后把消息写入 `ChatLogStore`（主动通信）。
2. `navigation.explore`：探索指定方向/时限并返回发现（对齐论文 `mineItem` 的 explore 参数与资源定位需求）。
3. **可中断性补丁（关键）**：
   - 现状：`src/env/mineflayer/skill_runtime/runtime.js` 中 oneshot 动作走 `execute` 阻塞等待；`/skill/stop` 只能改状态、**不能真正中止 bot 正在进行的动作**；只有 continuous 技能（有 `start/stop` handler）可被中断。
   - 方案 A（推荐）：把长时动作（采集、战斗追击、探索）设计为 continuous 模式，执行线程用 `start` + `status` 轮询 + `stop` 中断。
   - 方案 B：在 bridge 层增加 abort 机制（如执行上下文取消）。
   - 规划线程应优先输出 continuous 动作，否则中断机制不生效。

### 4.6 测试（`test/`）

新增 unittest（`test_*.py`，沿用现有风格）：buffer 覆盖语义、TeamMemory 并发、LLM 输出解析、DAG resolver、worker 启停。

---

## 5. PPA 适配设计（`src/baseline/parallelized/`）

只做编排，不重复实现底座。

| 文件 | 职责 |
| --- | --- |
| `schemas.py` | `PlannerProposal{skill, params, interrupt, reason}`（序列化复用 `LLMSemanticUnit`）；`PPAConfig`（模型、轮询间隔、聊天窗口、超时等） |
| `prompts.py` | PPA 系统提示词：身份/任务目标 + 技能清单（`SkillRegistry` 渲染 `llm_description` + params 摘要）+ 观测文本（verbalizer）+ 最近聊天 + 当前动作 + JSON 输出格式约束 |
| `planner_thread.py` | 规划循环（`LoopWorker` 子类）：感知 → 读记忆 → `LLMClient.complete` → `parse_llm_action` → 校验（`SkillRegistry.resolve` + params_schema）→ `buffer.put(proposal)` → `interrupt=True` 时通知执行线程 → 被动通信写聊天 |
| `acting_thread.py` | 执行循环：`buffer.has_new()` → 有新动作先 `planner.stop(active_id, reason="interrupt")` → `planner.execute`（oneshot）或 `planner.start` + `status` 轮询（continuous）→ 完成/失败写 `ActionHistoryStore`；只响应规划器中断，不做抢占决策 |
| `agent.py` | `PPAAgent`：聚合现有 `SkillAgent` + 两条 loop + buffer/memory 视图；暴露 start/stop/status |
| `team.py` | `PPATeam`：任务目标、完成判定（物品数量/BOSS 血量/队伍胜负）、实验配置，挂在 `TeamRunner` 上 |
| `benchmark.py` | 资源采集/BOSS/PVP 任务定义与指标（health ratio、progress、成功率，公式对齐论文附录）；驱动脚本放 `script/` |

---

## 6. 关键设计决策

1. **动作替换语义**：LLM 输出直接是 `<skill>.<command>` + params，与 `ActionUnit` 一致；执行侧完全走 `BaseActionPlanner`。递归分解层（`src/tasks`）把 `obtainItem` 一类高层需求展开为项目动作序列，规划线程可选择先行展开或按需解析。
2. **缓冲区版本号**：单槽覆盖队列必须带版本号，否则执行线程无法区分“新动作”和“同一动作”，会重复执行。
3. **中断真实性**：oneshot 动作不可真正中断；长动作改 continuous 或加 bridge abort（见 4.5）。
4. **线程模型**：每 agent 两个 daemon 线程（规划/执行）+ 团队轮询线程；共享记忆加锁；LLM 调用为同步阻塞，放规划线程即可；沿用 threading + HTTP 风格，不引入 asyncio。
5. **token 窗口**：聊天日志只取最近 N 条（论文取 N=agent 数）；输出格式约束保持短输出；观测文本用 verbalizer 压缩。
6. **服务器不暂停**：现有 `Server` 已满足；任务期间服务器持续运行，与论文一致。
7. **安全**：LLM API key 仅通过环境变量注入；日志/记忆不落密钥。

---

## 7. 目录结构预览

```
src/
├── llm/                      # 共享：LLM 客户端/解析/通用 prompt 工具
├── memory/                   # 共享：团队集中式记忆（观测/聊天/动作历史）
├── runtime/                  # 共享：buffer(单槽覆盖队列)、LoopWorker、TeamRunner
├── tasks/                    # 共享：DAG 任务图 + 物品依赖数据 + 动作序列解析器
├── skills/
│   ├── communication/        # 补：send_message（主动通信）
│   └── navigation/           # 补：explore 命令
├── agents/                   # 现有：SkillAgent + BaseActionPlanner（原样复用）
├── perception/  interface/  launcher/  env/ ...   # 现有，原样复用
└── baseline/
    └── Parallelized/         # 论文 PDF + PPA 适配（本目录）
        ├── DEVELOPMENT.md    # 本文档
        ├── schemas.py  prompts.py
        ├── planner_thread.py  acting_thread.py
        ├── agent.py  team.py  benchmark.py
```

> 说明：Windows 文件系统不区分大小写，`src/baseline/parallelized/` 与 `src/baseline/Parallelized/`
> 是同一目录；后续 Python 包建议命名为 `parallelized`（代码内 import 用小写即可），文档与代码同目录存放。

---

## 8. 实施阶段与验收标准

### 阶段 1：共享底座

- 交付：`src/llm/`、`src/runtime/`（buffer/worker）、`src/memory/`。
- 验证：`test/` 新增单元测试（buffer 覆盖语义、TeamMemory 并发、LLM 解析、worker 启停）；用现有 unittest 约定运行。

### 阶段 2：任务知识与动作补丁

- 交付：`src/tasks/`（dag + items.json + resolver）、`src/skills/communication/`、`src/skills/navigation` 的 explore 命令、可中断性改造。
- 验证：resolver 单测（如 diamond_armor 展开为合法动作序列）；`script/demo_skill_bridge.py` 风格冒烟验证新技能注册与执行。

### 阶段 3：PPA 适配

- 交付：`src/baseline/parallelized/` 全部编排文件 + `script/demo_parallelized_*.py`。
- 验证：2-3 个 agent 资源采集 demo（动作全部走项目技能），观察规划/执行并发、中断切换、记忆更新。

### 阶段 4：实验验证

- 交付：`benchmark.py` 任务与指标；消融对照（同套动作、去掉 buffer 与中断的串行版）。
- 验证：BOSS/PVP 场景成功率、health ratio、progress；中断机制有效性；token 窗口与推理时延（对齐论文 Fig.5 分析）。

---

## 9. 待决问题

1. LLM 客户端配置是否对齐 MCAgent 的 `src/models/llm_client.py` 模式（本项目当前无 `.env`/模型配置），以及默认模型选择。
2. 长动作可中断性采用“continuous 化”还是“bridge abort”方案（影响 bridge/runtime 改动范围）。
3. 基准任务的初始资源/世界种子等实验参数（影响可复现性）。

---

## 10. 参考

- 论文：`src/baseline/Parallelized/ParallelizedPlanning-Acting.pdf`（AAMAS 2026，arXiv:2503.03505v2）
- 现有动作系统：`src/agents/action/planner.py`、`src/agents/action/registry.py`、`src/agents/skill_agent.py`
- 技能运行时：`src/env/mineflayer/bridge_skill.js`、`src/env/mineflayer/skill_runtime/runtime.js`
- 感知与文本化：`src/perception/base_perception.py`、`src/perception/verbalizer/`
- 协议结构：`src/interface/schemas.py`
- 服务器与进程：`src/launcher/server.py`、`src/launcher/utils.py`
