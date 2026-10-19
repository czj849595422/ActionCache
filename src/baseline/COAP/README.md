# COAP（CACHE-OPTIMIZED ASYNCHRONOUS PLANNING）

## 概述

COAP 是从 `D:\MC\MCAgent\framework\arc`（Action Reasoning Cache）抽取的动作
缓存驱动双路快速规划方法，结合 PPA 的线程架构与记忆模型。已实现：
动作缓存模块（第一阶段）与四条并行线路的 runner 组装（第二阶段）。
不包含指导规划、全局记忆、事件指标与 Sandwich 环境适配。

## 本阶段迁移内容（四项能力）

1. **缓存初始化**：`CacheBootService`（empty / llm_init / reuse_old 三种模式、
   动作空间过滤、去重、score floor）与 `ModelCacheInitializer`（同步 LLM
   端口），协议为 `{"entries":[{index,previous_action,next_action,reason,initial_score}]}`。
2. **普通动作规划暂存**：`CandidateBuffer`（追加/去重/阈值或时间触发 flush）
   与 `CandidateAggregator`（按动作转移对聚合 success/failure/uncertain 与
   reason 采样）。
3. **提取与内容更新**：`CacheRetriever`（按上一动作检索 top-k、分数排序、
   scope/owner 过滤）、`CacheContentUpdater`（聚合候选 → LLM 抽象 reason →
   创建/更新条目，env 源优先）、`ModelCacheContentGenerator`；存储为同步
   线程安全 `InMemoryCacheRepository`。
4. **缓存动作规划协议**：`protocol.py` 提供 standard_action / cache_action
   两段式输出协议（JSON 动作 + `<ARC_ACTION_READY>` marker + 可选
   communication）的格式指令与解析，支持 STRICT/ADVISORY 候选策略。

## 已实现：四条并行线路组装（第二阶段）

- `scoring.py`：`ScoreUpdater` 周期评分递推（CONFIDENCE_ADJUSTED /
  EXPONENTIAL_SMOOTHING / INCREMENTAL_FEEDBACK / BOUNDED_ADDITIVE /
  FREQUENCY_AWARE）。
- `consistency.py`：`ImportantParameterActionConsistency`（动作名 + 重要参数
  对比，用于双路一致性、候选状态与缓存强化）。
- `adapters.py`：ActionUnit ↔ Action 互转、`exec_check`、动作空间渲染
  （chat 屏蔽）、被动通信反馈文本。
- `codec.py`：`COAPDecisionCodec`（standard_action 含 `interrupt`、cache_action
  不含；简写展开、chat 拒绝、schema 校验）。
- `workers.py`：`COAPActingWorker`（执行/中断/反馈/候选记录）、
  `COAPPlanningWorker`（定时 5s 普通规划 + 竞速）、`COAPCachePlanningWorker`
  （检索 top-k → 缓存规划竞速）、`COAPCacheWorker`（flush 5 条/30s → 聚合 →
  内容更新；评分反馈实时递推）。
- `runner.py`：`COAPRunner` 装配 TeamMemory/CacheService/一致性/四线路，
  竞速 claim 锁、生命周期与清理。
- `script/demo_coap.py`：CLI 入口（`--check`、`--planning-interval`、
  `--cache-boot-mode`、`--enable-chat-actions` 等）。COAP 消融开关包括
  `--disable-cache-planning`、`--disable-standard-verification`、
  `--disable-timed-planning`、`--disable-cache-reasoning-optimization` 和
  `--disable-score-update`；启用后会按 `_woCP`、`_woSP`、`_woPP`、`_woRO`、
  `_woSU` 顺序追加到 recording root，`reuse_previous` 和 `empty` 分别追加
  `_reuse`、`_woInit`。多个开关会叠加后缀。

关键约定：普通规划协议含 `interrupt` 字段；冷启动初始化分数上限 0.5；
主动通信随动作执行成功提交，被动通信为动作反馈广播（不调用 LLM）；
chat 动作在动作空间渲染与 codec 双重屏蔽。

## 运行时形态与组装约定

- 缓存模块为**同步、线程安全**组件（`threading.RLock`），与 PPA 的规划/执行
  worker 并行运行。
- LLM 初始化与内容生成通过 `LLMClientPort`（同步 `complete(messages, *, metadata)`
  回调）注入；组装时将 PPA 的 LLM client 包装为该端口即可。
- 动作表示为环境无关的 `Action(name, parameters)`；与 PPA `ActionUnit` 的
  桥接适配器在组装阶段实现。

## 目录结构

```text
src/baseline/COAP/
  schemas.py      数据契约（Action/CacheEntry/CandidateRecord/...）
  config.py       缓存配置与枚举
  ports.py        同步结构化端口
  repository.py   内存缓存仓库（线程安全）
  cache.py        缓存生命周期服务（Boot/Retriever/Buffer/Aggregator/ContentUpdater/Service）
  prompts.py      prompt 构建（system/user 结构对齐 PPA）
  prompts/        模板文件（system/user/cache_boot/cache_content）
  protocol.py     动作规划两段式输出协议（含 interrupt）
  planning.py     同步 LLM 端口与缓存响应解析
  scoring.py      评分周期递推
  consistency.py  重要参数对比
  adapters.py     PPA↔COAP 桥接
  codec.py        协议 → COAPProposal
  request_builder.py 规划请求组装
  workers.py      四条线路 LoopWorker
  runner.py       COAPRunner 装配与生命周期
  COAP_METHOD.md  方法介绍
```

## 范围边界（尚未实现）

- `CacheEvictor`（驱逐）：**设计上不需要**——检索按评分降序取 top-k，
  低分条目不会被缓存动作规划选中，无需显式淘汰。
- JSON 持久化（`CacheSourceLoaderPort` 仅声明）。
- 指导规划、全局记忆、事件、指标与 Sandwich 环境适配。

## 后续工作

- 实机验证（`script/demo_coap.py` 短清剿/空任务）与调参。
- 缓存检索增加上下文硬条件、评分区分"合法成功"与"任务进展"（避免 WAIT
  误强化）、普通规划结果时效性守门。

## 测试

```powershell
python -B -m unittest discover -s test -p "test_coap_cache.py" -v
python -B -m unittest discover -s test -p "test_coap_workers.py" -v
python -B -m unittest discover -s test -p "test_*.py"
```

当前全量 560 个用例通过（COAP 缓存模块 42 个 + worker/runner 18 个）。
