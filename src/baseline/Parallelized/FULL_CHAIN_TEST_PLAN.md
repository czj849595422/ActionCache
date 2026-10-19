# PPARunner 全链路测试缺口与开发计划

> 目录：`src/baseline/Parallelized/`  
> 更新：2026-08-11  
> 适用实现：论文 JSON 协议版 `PPARunner`

## 1. 结论

当前实现已经具备发起真实运行的最小链路：

```text
TaskSpec
  -> TaskEnvironmentBundle
  -> TaskEnvironmentSession
  -> Minecraft Server / SkillAgent
  -> 文本观测
  -> 共享 LLMClient
  -> JSON 决策协议（skill / interrupt / reason）
  -> BaseActionPlanner / skill bridge
  -> TaskManager outcome + metrics
  -> worker / session / runtime 清理
```

现阶段缺口不在 `PPARunner` 核心组装或基础证据留存。统一 run/episode 记录层、LLM 自动调用记录、PPA 证据归档、安全运行控制、细粒度轨迹关联与 live preflight 已经完成；剩余重点是技能签名文本渲染、任务级 allowlist、短时 smoke fixture 和真实环境自动验收。当前 CLI 已能产生可审计 artifact，但在完成 L1-L4 前仍不能形成真实 baseline 通过结论。

## 2. 当前基础

### 2.1 已实现能力

- `PPARunner.from_bundle()` 直接使用 `TaskEnvironmentSession`，不嵌套 `TeamRunner`。
- 每个 Agent 具有独立 Observation、Planning、Acting worker。
- `TeamMemory` 保存文本观测、主动/被动聊天和动作生命周期。
- `JsonDecisionCodec` 解析论文 JSON 决策协议（`skill`/`interrupt`/`reason`，
  兼容 ```json 代码块；参数经安全 AST/literal 与 manifest schema 校验）。
- action 通过 Agent 自身 `SkillRegistry`、allowlist 和 manifest schema 校验。
- continuous action 支持真实 `stop(reason="planning_interrupt")`。
- oneshot action 的中断明确记录为 deferred，不伪装成真实中断。
- task outcome、最终 metrics、worker 错误和清理错误可进入 `PPARunResult`。
- `script/demo_parallelized.py --check` 可检查任务、世界、技能目录和 JSON 决策协议，不启动服务器或调用模型。
- 通用 `src.recording` 按 `data/<baseline>/<run_id>/<episode_id>` 保存原子、脱敏、带版本的运行证据。
- 所有 `LLMClient` complete/stream、同步/异步调用自动记录来源、输入输出、状态、耗时和 provider token usage。
- PPA 将 result、TeamMemory、生命周期事件、LLM 调用和 server/bridge 日志归档到同一 episode。

### 2.2 已有验证

- codec、prompt、worker、runner focused unittest 已覆盖 JSON 决策协议和并发语义。
- fake 两 Agent 验收已覆盖 continuous 运行、普通 proposal 覆盖、interrupt proposal、只执行最新动作和终态清理。
- 项目全量 unittest 376/376 已通过。
- 两 Agent PPA 使用真实 `LLMClient` 和 fake provider 的集成测试已验证两个 PlanningWorker 的调用进入同一 episode，并保留 task/Agent 来源。
- Minecraft 任务环境、感知和单独 LLM 能力存在历史 live 探测产物。

上述证据不能替代一次真实 `PPARunner + Minecraft + LLM` 联合运行。本实现尚未完成该项 live 验证。

## 3. P0：首次联合测试前必须开发

### 3.1 运行产物与可观测性

**状态：已完成。统一记录基础设施、PPA 主链路和细粒度轨迹关联均已落地。**

- [x] `PPARunResult.to_dict()`：outcome、metrics、planned/executed count、worker/cleanup 错误和时间。
- [x] `TeamMemory` 终态快照：观测、聊天和动作历史。
- [x] LLM 调用：baseline/run/episode、task、Agent、component、operation、request ID、完整 messages、输出、错误、耗时和 provider usage。
- [x] Runner/worker/task/proposal/action/interrupt/cleanup 生命周期事件。
- [x] server 和各 bridge 使用 episode 独立日志目录。
- [x] started 文件先于 provider 请求；终态写失败不返回成功；缺失 token 不估算。
- [x] API key、Authorization、Cookie、Password、Secret、Token 和 header 值脱敏或排除。
- [x] 在 proposal/action 事件中直接保存对应 request ID，使 request ID 与 plan ID 无需按时间推断即可关联。
- [x] 补充 buffer version、每次 action status poll 和逐项 cleanup 审计事件。

当前稳定运行产物结构：

```text
data/<baseline>/<run_id>/
  run.json
  summary.json
  episode-0001/
    episode.json
    result.json
    events.jsonl
    memory.json
    llm/
      calls/<request_id>.json
      summary.json
    logs/
      mc-server.log
      skill-bridge-<agent>-<port>.log
```

格式和配置规则见 `docs/recording_artifacts.md`。内置 TaskSpec 已迁移到 `data`；旧 `output.dir` 暂时兼容并产生弃用警告，`save_llm_calls` 不再能够关闭记录。

### 3.2 安全运行控制

**状态：已完成。**

内置任务包含 4 至 5 个 Agent，任务超时为 180 至 900 秒。PlanningWorker 默认连续调用模型，实机测试需要额外的独立上限：

- [x] `--max-run-seconds`：独立于任务 outcome 的外层 watchdog。
- [x] `--max-llm-calls`：全队调用总量上限。
- [x] `--planning-interval`：从 CLI 暴露已有配置，控制调用速率。
- [x] 每 Agent 调用上限（`--max-llm-calls-per-agent`）；token 上限留待 P1（provider usage 覆盖不稳定）。
- [x] Ctrl+C、watchdog、worker error 和 task terminal 必须进入同一清理路径。
- [x] 到达预算后应产生明确 runner stop reason，而不是静默退出。

这些限制主要服务于测试安全、费用控制和故障收敛，不应改变正常 PPA 的 buffer 与中断语义。

### 3.3 Live preflight

**状态：已完成。`--preflight` 提供完整 live 预检；离线 `--check` 保持无副作用。**

现有 `--check` 保持离线、无副作用。建议另增显式 live preflight，检查：

- [x] Java、Node 和必要 npm 依赖可用。
- [x] `server.jar`、任务世界和 `server.properties` 存在且可读。
- [x] Minecraft、RCON 和全部 bridge 端口未被占用。
- [x] RCON 已启用且可在启动后执行一次无害查询。
- [x] disposable world 目标没有锁文件或遗留运行进程。
- [x] 日志目录、运行目录和世界目录可写。
- [x] LLM 环境变量完整，模型端点可连通。
- [x] 使用真实 codec 做一次最小 LLM 协议探测，并记录但不执行 action。

离线 `--check` 不应因为 live preflight 的加入而开始启动服务器或访问模型。

### 3.4 技能签名文本完整性

**状态：已完成（以简单指令动作空间实现）。**

prompt 中的动作空间改为 `- <简写动作>(<重要参数>, ...) : #<说明>`；LLM 只需给出简写指令
与重要参数，未给出的参数由 `SimpleInstructionRegistry` 自动填充 manifest 默认值，必要参数
与 schema 仍严格校验（`SimpleInstructionCodec`）。映射表见 `docs/ppa_simple_instructions.md`，
未收录动作回退规范名 `<skill>.<command>`，能力不降级。

当前 prompt 的函数签名主要呈现顶层参数类型、required 和 default。真实 manifest 还包含嵌套 object/array、`items`、`enum`、`minimum`、`minLength`、`minItems`、`anyOf` 和 `oneOf`。

codec 会严格执行这些约束。如果 prompt 没有完整说明，模型可能连续输出无效参数并达到 worker 错误上限。因此需要实现与 codec 约束一致的文本 schema renderer：

- 展开嵌套字段，但控制最大深度和总长度。
- 显示 enum、数值/长度下限、数组 item 类型。
- 用文本描述 `anyOf`/`oneOf` 的可选结构。
- 保持默认值只用于说明，不在 parser 内自动写入默认值。
- 对复杂 target、position、item 列表等常见参数提供准确文本签名。

### 3.5 任务级技能 allowlist

**状态：已完成。**

各任务的最小动作集已落地到 `src/tasks/environment_factory.py` 的 `TASK_ACTION_SPACES` 静态常量，
按任务 ID/任务前缀解析后传给 Agent；`--unsafe-actions` 仍放行全部普通技能
（排除 internal action）。最终集合见 `docs/task_action_allowlists.md`。

需要调整某个任务的动作空间时，只修改 `TASK_ACTION_SPACES` 中对应任务的集合；debug 使用 `None`
放行全部普通动作。动作权限使用规范动作名，prompt 中的简写由简写动作映射负责渲染。

### 3.6 短时受控 smoke fixture

**状态：已完成。**

`script/smoke_ppa_live.py` + `src/baseline/Parallelized/smoke.py` 提供可重复的短时 live smoke：
1-2 个 Agent、`survive_until` 自动终止、外层 watchdog（下限 = timeout+15s，覆盖服务器启动）、
scripted LLM 确定性触发 oneshot/continuous/interrupt，结束后自动断言动作链与
worker/服务器/端口清理并输出报告。已在真实 Minecraft 上运行一次通过（exit_code=0）。

现有内置任务规模和超时不适合作为第一次联合调试。需要增加一个测试专用的短时 fixture，或由 live harness 在内存中构造等价 session：

- 1 至 2 个 Agent。
- 30 至 60 秒外层上限。
- 使用现有世界和现有 skill，不引入新 benchmark 语义。
- 能确定性触发一个 oneshot、一个 continuous 和一次 interrupt。
- terminal 条件简单、可自动判定。

该 fixture 只用于验证运行链路，不作为论文 benchmark 或成功率实验数据。

## 4. P1：提高可重复性和诊断能力

### 4.1 协议合规统计

**状态：未开始。**

为每个 Agent 统计：

- LLM 总调用数和成功解析数。
- Markdown、额外文本、未知 action、禁用 action 和 schema 失败次数。
- 连续错误峰值和错误恢复次数。
- 首次有效 proposal 延迟。

PlanningWorker 已能把上次错误写回下一轮 prompt；统计层不应放宽固定协议或自动执行未通过校验的文本。

### 4.2 并发与时延证据

**状态：部分完成。LLM 调用和 Observation/Planning/Acting 事件已有时间戳，两 Agent 并发与归档已有自动化测试；跨组件时序断言尚未形成 live 验收器。**

记录 Observation、Planning 和 Acting 的开始/结束时间，以自动证明：

- 至少两个 Agent 的 LLM 调用发生过时间重叠。
- Planning 没有直接调用 skill runtime。
- Acting 在旧动作运行时只覆盖普通 proposal。
- interrupt event 在被 Acting 消费前保持锁存。
- 清理开始后没有新的 proposal 写入 buffer。

### 4.3 Prompt 与 token 预算

**状态：部分完成。provider 返回的 token usage 和字段覆盖率已记录；prompt 分区长度、估算 token、预算裁剪和裁剪证据尚未实现。**

按任务类别记录以下文本长度和估算 token：

- system 技能目录。
- task/identity/task metrics。
- 最新观测。
- 聊天窗口。
- 当前动作、缓冲计划和最近结果。

超过预算时采用确定性裁剪，并在 artifact 中记录被裁剪区域。不能静默丢弃任务目标、输出协议或 action schema。

### 4.4 清理审计

**状态：部分完成。fake 集成测试已验证终态后无 PPA worker 存活，runner 会保存 worker/cleanup error；真实进程、端口、世界和 runtime 的自动审计尚未实现。**

测试结束后自动验证：

- 所有 PPA worker 已退出。
- 所有 SkillAgent bridge 已停止。
- Minecraft server 已按配置停止。
- bridge、Minecraft 和 RCON 端口已释放。
- TaskManager 已 teardown。
- disposable world 已清理。
- bundle runtime 目录已关闭。
- 没有残留 Java/Node 子进程。

清理失败必须使本次测试失败，即使任务 outcome 为 success。

## 5. 仅需运维准备的条件

以下项目原则上不是 PPARunner 新功能，但在 live 测试前必须确认：

- 使用 mcagent Python 环境。
- Java 版本与 Minecraft 1.19 server 兼容。
- Node 依赖完整。
- `src/minecraft/server.jar` 存在。
- `server.properties` 的 server、RCON 端口与 CLI/Agent 配置一致。
- offline 登录模式符合本地 Mineflayer Agent 配置。
- 任务世界资源完整，且没有运行中的服务器占用目标世界。
- `LLM_API_URL`、`LLM_API_KEY`、`LLM_MODEL` 通过环境变量或指定 env 文件提供。
- 模型端点支持项目使用的 OpenAI 兼容 chat completions 接口。

这些检查应由 preflight 报告，不应把密钥值打印到终端或 artifact。

## 6. 分层 Live 验收路线

### L1：Minecraft + scripted decisions

**状态：已完成（以 smoke 脚本为 scripted-decisions 载体，已运行一次通过）。**

`script/smoke_ppa_live.py` 使用真实 TaskEnvironmentSession、scripted LLMClient 与真实
`BaseActionPlanner`/bridge，覆盖 continuous 启动、普通 proposal 覆盖、interrupt 真实 stop、
只执行最新动作、终态清理与端口释放断言。

目的：隔离验证真实环境和动作链，不调用真实 LLM。

流程：

1. 启动真实 TaskEnvironmentSession。
2. 使用可注入的 scripted/fake LLMClient 返回合法四行文本。
3. 先返回 continuous action。
4. 连续返回普通 proposal，验证它们只覆盖 buffer。
5. 返回 interrupt proposal，验证真实 bridge stop。
6. 验证只执行 buffer 最新动作。
7. 进入终态或 watchdog，完成全量清理。

这一层必须验证真实 `BaseActionPlanner` 和 bridge，而不是 fake planner。

### L2：真实 LLM + 无 Minecraft

**状态：未运行。当前完成的是 fake provider + 真实 `LLMClient` 集成测试。**

目的：隔离验证 prompt、协议和 manifest 参数质量。

流程：

1. 使用真实 `PPAPromptBuilder`、任务 TaskSpec 和 SkillRegistry。
2. 注入保存的文本观测、聊天和动作结果。
3. 对每个任务类别执行多次真实模型调用。
4. 统计协议合规率、schema 失败和 token usage。
5. 不启动 server，不执行 action。

### L3：2 Agent 短时联合 smoke

**状态：未运行。**

目的：首次打通真实 Minecraft + 真实 LLM。

必须覆盖：

- 两个 Agent 均获得文本观测。
- 两个 PlanningWorker 均产生有效 proposal。
- 至少一个真实 action 完成并回写结果。
- 至少一次团队消息进入 TeamMemory。
- 任务终态或 watchdog 后无残留 worker/process/port。

### L4：内置完整任务

**状态：未运行。**

建议顺序：

1. `mining` 或 `monster_clearance`。
2. `boss_combat`。
3. `trade`。

完整任务用于验证 baseline 行为，不用于替代前三级故障隔离。trade 超时最长，不适合作为首次联合测试。

## 7. 自动验收标准

一次 L3/L4 测试只有同时满足以下条件，才能标记为全链路通过：

### 环境与观测

- Server 启动并达到 ready 状态。
- 预期数量的 Agent 全部登录。
- 每个 Agent 在初始观测超时内获得非空文本观测。
- TaskManager prepare/start 成功。

### 模型与协议

- 每个 Agent 至少完成一次真实 LLM 调用。
- 每个 Agent 至少产生一个通过 codec、registry、allowlist 和 schema 的 proposal。
- 保存的 prompt 与响应能够通过 request ID/plan ID 关联。
- 未向模型启用 JSON response format。

### 规划与执行

- planned count 和 executed count 均大于零。
- 动作结果进入 TeamMemory，且 prompt 可读取文本描述。
- 若场景要求 interrupt，必须观测到真实 `planning_interrupt` stop。
- oneshot interrupt 只能标记 deferred。
- 过期普通 proposal 不得抢占正在运行的动作。
- 任意时刻同一 Agent 不得出现不受 planner 管理的动作重叠。

### 任务与清理

- 得到明确 TaskOutcome，或由测试 watchdog 产生明确失败原因。
- 最终 metrics 已保存。
- worker error 和 cleanup error 均为空时才允许 exit code 为 0。
- 所有 worker、Agent、server、世界副本和 runtime 目录按顺序清理。
- 无残留进程和占用端口。
- artifact 不包含敏感信息。

## 8. 推荐实施顺序

1. [x] 增加统一 run/episode recorder、LLM 自动调用记录和 PPA 最终结果输出。
2. [x] 增加 request ID/plan ID 直接关联、buffer version 和细粒度 cleanup 事件。
3. [x] 增加 watchdog、LLM 调用预算和 CLI planning interval。
4. [x] 增加 live preflight，保持现有离线 `--check` 不变。
5. [ ] 完善 manifest 文本 schema renderer 和任务级 allowlist。
6. [ ] 建立 L1 scripted live harness，验证真实 action/interrupt/cleanup。
7. [ ] 建立 L2 LLM-only 协议测试，修正 prompt 和模型配置。
8. [ ] 运行 L3 两 Agent 短时联合 smoke。
9. [ ] 在 L1 至 L3 稳定后运行 L4 内置完整任务。

## 9. 完成定义

满足以下条件后，可以声明“文本版 PPARunner 全链路测试能力已完成”：

- L1、L2、L3 均有可重复脚本和自动断言。
- [x] 记录基础设施能够为每次 PPA 运行生成独立、脱敏的 run/episode artifact。
- [x] request ID、plan ID、action ID 和 cleanup step 达到无需时间推断的完整关联。
- [x] watchdog、模型预算和异常路径均能可靠清理环境（fake 级已验证，真实环境验证依赖 L3/L4）。
- 至少一次 L3 使用真实 Minecraft 和真实 LLM 通过。
- 至少一次 L4 内置任务产生完整 outcome、metrics 和动作/模型轨迹。
- 失败运行能够仅依靠 artifact 定位到环境、观测、模型、协议、动作、任务判定或清理中的具体阶段。

在实际完成上述 live 运行前，只能声明代码级和 fake 集成测试通过，不能声明真实 baseline 已通过。
