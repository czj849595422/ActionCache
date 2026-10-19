# COAP 算法流程伪代码

> 本文档描述当前 `src/baseline/COAP/` 代码的实际运行流程，供实现与消融对照使用。
> 方法层面的设计说明见 `COAP_METHOD.md`。

## 1. 总体结构

COAP（Cache-Optimized Asynchronous Planning）在每名 Agent 上运行四条并行线路：

```text
线路① 动作执行（ActingWorker）      —— 派发、轮询、中断、结果反馈
线路② 定时普通规划（PlanningWorker） —— 每 planning_interval 秒一次；动作结束轮参与双路竞速
线路③ 缓存动作规划（CachePlanningWorker）—— 仅在竞速事件触发时运行一次
线路④ 缓存维护（CacheWorker，全局唯一）  —— flush→聚合→内容更新→评分更新
```

四条线路通过以下共享状态连接：

- `SingleSlotOverwriteBuffer`：提案缓冲（单槽，后写覆盖）。
- `COAPDecisionCoordinator`：竞速协调器（claim 锁、动作结束事件、强制双路标志）。
- `TeamMemory`：观测、动作历史、聊天（result / active 通道）。
- `CacheService`：候选缓冲、聚合器、检索器、内容更新器、评分器。

```text
┌────────────────────────── COAPRunner ──────────────────────────┐
│  TeamMemory / CacheService / ImportantParameterActionConsistency │
│  per-agent: [ActingWorker] ←缓冲/中断事件→ [PlanningWorker]       │
│                    │                                   │         │
│                    └── 动作结束事件 ──→ [CachePlanningWorker]      │
│                    │                                   │ 检索     │
│                    结果/候选记录                        ▼         │
│                    ▼                              CacheService   │
│              TeamMemory(result/active)                     │    │
│                    └────────────→ [CacheWorker] ──────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

## 2. 全局流程

```python
def run_coap(spec, env, llm_client, config, recorder):
    memory        = new TeamMemory()
    cache_service = new CacheService(config.cache_boot, config.buffer_update,
                                     config.retrieval, config.representation)
    consistency   = new ImportantParameterActionConsistency(config.important_params)
    score_updater = new ScoreUpdater(config.score)
    coordinator   = new COAPDecisionCoordinator(cache_service, task_scope=spec.task_id)

    # 1) 缓存冷启动（empty / llm_init / reuse_old）
    #    冷启动条目分数限制在 (0, 0.5]，未经环境验证；后期由动作反馈强化
    cache_service.boot(task_context, action_space,
                       task_scope=spec.task_id,
                       old_entries=optional_old_entries)

    # 2) 启动 worker：观测 → 动作执行 → 定时规划 → 缓存规划 → 缓存维护
    for agent in env.agents:
        start ObservationWorker(agent)
    wait_for_initial_observations(all agents)
    for agent in env.agents:
        start ActingWorker(agent)          # 线路①
        start PlanningWorker(agent)        # 线路②
        start CachePlanningWorker(agent)   # 线路③
    start CacheWorker()                    # 线路④（全局）

    # 3) 主循环：轮询任务状态直至终止
    while not stopped:
        if fatal_event or max_run_seconds reached or llm budget exhausted:
            stop
        outcome, metrics = env.poll()
        if outcome != RUNNING:
            stop(task_outcome)
        sleep(task_poll_interval)

    # 4) 收尾：停止全部 worker、关闭服务器、finalize 记录
    stop_all_workers()
    cleanup()
```

## 3. 竞速协调器（DecisionCoordinator 状态机）

```python
class DecisionCoordinator:
    last_action: Action | None          # 上一个实际执行的动作
    pending_interrupt_prev: Action|None # 被中断动作（用于候选记录）
    action_active: bool                 # 当前是否有动作在执行
    buffer_has_candidate: bool          # 缓冲中是否已有候选提案
    race_active: bool                   # 是否处于双路竞速
    force_dual_next: bool               # 上一轮重规划失败，下一轮强制双路
    race_claim: Lock                    # 竞速唯一席位（先得先写）
    action_finished: Event              # 动作结束事件
    cache_race_event: Event             # 触发缓存规划的事件

    def on_action_started():
        action_active = True
        buffer_has_candidate = False
        release(race_claim); race_active = False

    def on_action_finished(last):
        last_action = last
        action_active = False
        set(action_finished)

    def begin_race():
        release(race_claim)     # 清残留锁
        race_active = True

    def try_claim() -> bool:
        # 非竞速（定时规划）直接放行；竞速中抢占唯一席位
        if not race_active: return True
        return acquire(race_claim, blocking=False)

    def end_race_if_open():
        if race_active:
            race_active = False
            release(race_claim)
```

## 4. 线路①：动作执行（ActingWorker）

```python
def acting_worker_loop():
    while not stop:
        if active_proposal is not None:
            advance_active()          # 轮询/中断 continuous 动作
            continue
        proposal = buffer.peek_new()  # 新版本提案
        if proposal is None: continue
        interrupt_event.clear()
        coordinator.on_action_started()   # 清竞速状态与候选标志
        dispatch(proposal)

def dispatch(proposal):
    active_proposal = proposal
    if mode == CONTINUOUS:
        result = planner.start_unit(proposal.action_unit)
        active_action_id = result.action_id
        if failed or already terminal:
            finish_active(result)
    else:  # oneshot
        result = planner.execute_unit(proposal.action_unit)
        finish_active(result)

def advance_active():
    if mode != CONTINUOUS: return
    if interrupt_event.is_set():           # 新规划要求中断当前动作
        result = planner.stop(active_action_id, reason="planning_interrupt")
        coordinator.pending_interrupt_prev = action(active_proposal)
        interrupted_previous_flag = True
        finish_active(result, status="interrupted")
        interrupt_event.clear()
        return
    result = planner.status(active_action_id)
    if result.status in TERMINAL:
        finish_active(result)

def finish_active(result, status=None, error=None):
    processed = postprocess(result)
    success   = processed.ok and status != "failed"

    # 1) 记忆：把 planned 动作标记为终态
    memory.complete_action(proposal.memory_entry_id, result=processed,
                           status=status, error=error)
    # 2) 被动反馈：动作结果广播（result 通道 + 可选 RCON say，不调用 LLM）
    broadcast_action_result(proposal, status, error)
    # 3) 主动通信：若成功且有 communication，提交到 active 通道并广播
    if success: commit_communication(proposal)
    # 4) 候选记录：(上一动作, 本动作) → 候选缓冲（success / failure）
    previous = pending_interrupt_prev or last_action or START
    cache_service.buffer.add(CandidateRecord(
        previous_action=previous, next_action=action(proposal),
        status=SUCCESS if success else FAILURE, reasoning=proposal.reasoning))
    pending_interrupt_prev = None
    # 5) 缓存评分反馈：检索等价条目并 record_feedback(success)
    record_cache_feedback(proposal, success)
    # 6) 执行记录表 action_executions.csv 写入一行
    execution_table.write_row(proposal, status, error, processed)
    # 7) 触发下一轮决策
    coordinator.on_action_finished(action(proposal))
    active_proposal = None
```

## 5. 线路②：定时普通规划（PlanningWorker）

```python
def planning_worker_loop():
    next_plan_at = now()
    while not stop:
        if no observation: continue
        handle_action_finished()
        if now() >= next_plan_at and not planning_in_flight:
            next_plan_at = now() + config.planning_interval   # 默认 5s
            start_standard_plan()

def handle_action_finished():
    if not coordinator.action_finished.is_set(): return
    coordinator.action_finished.clear()
    next_plan_at = now() + config.planning_interval            # 刷新计时
    if coordinator.force_dual_next:
        # 上一轮重规划失败：忽略缓冲候选，强制从双路开始
        coordinator.force_dual_next = False
        coordinator.begin_race()
        coordinator.cache_race_event.set()
        if not planning_in_flight: start_standard_plan()
        return
    if coordinator.buffer_has_candidate:
        return                        # 情形 A：定时规划已有候选，直接采用
    # 情形 B/C：动作结束且无候选 → 双路竞速
    coordinator.begin_race()
    coordinator.cache_race_event.set()
    if not planning_in_flight: start_standard_plan()

def start_standard_plan():
    planning_in_flight = True
    if coordinator.race_active:
        run_race_standard_plan()      # 竞速中的普通路径（可重规划）
    else:
        run_plan_once(operation="timed_standard_action",  # 定时并行规划
                      cache_candidates=(), feedbacks=())
    planning_in_flight = False

def run_race_standard_plan():
    # 双路竞速普通路径：首次尝试 + maximum_replans 次重规划
    feedbacks = []
    for attempt in range(1 + config.maximum_replans):        # 默认 3 次重试
        proposal, feedback = run_plan_once(
            operation="standard_action",
            cache_candidates=(), feedbacks=feedbacks)
        if proposal is not None or stop: return              # 成功即收尾
        if feedback: feedbacks.append(feedback)              # 累积失败原因
        emit("planning.replanned", attempt, feedback)
    # 重规划耗尽仍失败：结束竞速、清空反馈、标记下一轮强制双路
    coordinator.end_race_if_open()
    last_plan_error = None
    coordinator.force_dual_next = True
    emit("race.failed")

def run_plan_once(operation, cache_candidates, feedbacks):
    if not budget.try_acquire(agent): return None, "预算用尽"
    request = build_planning_request(
        memory=memory, spec=spec, registry=registry,
        operation=operation, cache_candidates=cache_candidates,
        last_error=last_plan_error, feedbacks=feedbacks)      # 重规划注入先前反馈
    proposal, feedback = invoke_plan(request, operation,      # 流式优先，超时终止
                                     cache_candidates)         # standard 60s / cache 15s
    if proposal is None: return None, feedback
    publish(proposal)
    return proposal, None
```

## 6. 线路③：缓存动作规划（CachePlanningWorker）

```python
def cache_planning_worker_loop():
    while not stop:
        if not coordinator.cache_race_event.is_set(): continue
        coordinator.cache_race_event.clear()
        if no observation: continue
        run_cache_plan()

def run_cache_plan():
    previous = coordinator.last_action or START
    lookup = cache_service.retrieve(previous, task_scope, agent_id)
    if lookup.entries is empty: return                  # 无候选则静默退出
    score_updater.record_hits([entry.id for entry in lookup.entries])
    if not budget.try_acquire(agent): return
    request = build_planning_request(operation="cache_action",
                                     cache_candidates=lookup.entries)
    proposal, feedback = invoke_plan(request, operation="cache_action",
                                     cache_candidates=lookup.entries)
    if proposal is None: emit("cache_planning.failed", feedback); return
    publish(proposal)                                    # 单次，不重试
```

## 7. 提案发布（publish）与竞速裁决

```python
def publish(proposal):     # 普通路径与缓存路径共用
    if not coordinator.try_claim():
        # 竞速败方：记录 uncertain 候选（供缓存学习），不写缓冲
        cache_service.buffer.add(CandidateRecord(
            previous_action=coordinator.last_action,
            next_action=action(proposal),
            status=UNCERTAIN, reasoning=proposal.reasoning))
        return
    entry = memory.record_action(agent, proposal.action, params,
                                 reason=proposal.reasoning,
                                 interrupt=(proposal.interrupt),
                                 status="planned",
                                 metadata={plan_id, request_id, path, mode})
    proposal.memory_entry_id = entry.entry_id
    buffer.put(proposal)                      # 覆盖旧候选（单槽）
    coordinator.mark_candidate_written()
    if proposal.interrupt:
        interrupt_event.set()                 # 要求中断当前 continuous 动作
```

两条路径的协议格式：

```text
定时并行规划 timed_standard_action:
    <reason>推理</reason><interrupt>true/false</interrupt>
    <action>简写动作与参数</action><chat>可选通信</chat>

普通规划 standard_action（竞速中）:
    <reason>推理</reason><action>简写动作与参数</action><chat>可选通信</chat>

缓存规划 cache_action:
    <action>简写动作与参数</action><chat>可选通信</chat>
```

## 8. 线路④：缓存维护（CacheWorker）

```python
def cache_worker_loop():                       # 全局唯一，1s 周期
    while not stop:
        flush_candidates()
        update_scores()
        sleep(1s)

def flush_candidates():
    if not cache_service.buffer.should_flush(): return
    # flush 条件：候选条数 >= buffer_size_threshold（5）或
    #            距上次 flush >= update_interval_seconds（30）
    records = cache_service.buffer.snapshot()
    for owner, scoped in partition_candidates(records):   # shared / per_agent
        aggregated = aggregator.aggregate(scoped)         # 按(prev,next)分组统计
        for batch in chunks(aggregated, size=3):
            report = content_updater.apply(batch, task_scope, owner)
            # LLM 抽象 reason；env 源优先于 llm 源；
            # 新条目创建 / 已有条目更新（环境证据可把分数提到 1.0）
            emit("cache.updated", created, updated, llm_calls)
    buffer.acknowledge(processed_ids)

def update_scores():
    if not score_updater.should_update(): return
    for owner in maintenance_owners(agent_ids):
        score_updater.update_scores(task_scope, owner,
                                    match_owner=(owner is not None))
    # 评分策略默认 CONFIDENCE_ADJUSTED：置信度系数 + 指数平滑，
    # 含命中奖励、成功/失败反馈惩罚与频率衰减
```

## 9. 关键参数（COAPConfig 默认值）

| 参数 | 默认值 | 作用 |
|---|---|---|
| `planning_interval` | 5.0 s | 定时普通规划周期 |
| `standard_action_timeout_seconds` | 60.0 s | 普通规划单次超时 |
| `cache_action_timeout_seconds` | 15.0 s | 缓存规划单次超时 |
| `maximum_replans` | 3 | 竞速中普通路径的重规划次数（首次之外） |
| `buffer_size_threshold` | 5 条 | 候选缓冲 flush 条数阈值 |
| `update_interval_seconds` | 30.0 s | 候选缓冲 flush 时间阈值 |
| `broadcast_results` | True | 动作结果是否经 RCON 广播 |
| `enable_chat_actions` | False | 是否把 chat 动作暴露给模型（COAP 默认屏蔽，通信走 `<chat>` 段） |
| 缓存冷启动分数 | (0, 0.5] | 未经环境验证的初始化分数上限 |

## 10. 一轮完整决策的时间线

```text
t0  动作 A 结束
    └ ActingWorker: 反馈广播 → 候选记录(prev=A) → on_action_finished
t1  PlanningWorker 检测到动作结束
    ├ 若缓冲已有候选（情形 A）→ 直接采用，不竞速
    └ 否则（情形 B/C）→ begin_race() + 触发缓存规划
        ├ 普通路径：standard_action 首次尝试 + 至多 3 次重规划（每次最新观测+反馈）
        ├ 缓存路径：cache_action 单次（检索 top-k 候选）
        └ 先通过校验者 try_claim() 写缓冲；后到者记 uncertain
t2  ActingWorker 取到新提案并派发
    └ 若提案 interrupt=true 且当前有 continuous 动作 → 中断后执行新动作
t3  新动作结束 → 回到 t0

重规划耗尽仍失败：
    end_race_if_open()；force_dual_next=True
    → 下一轮动作结束时忽略缓冲候选，强制从双路竞速开始（循环直至成功）
```

## 11. 数据通道与记忆

- 观测：`ObservationWorker` 定期写入 `TeamMemory`，作为规划输入的观察部分。
- 动作历史：`memory.record_action`（planned）→ `complete_action`（终态）。
- 主动通信：动作成功时提交 `proposal.communication` 到 `active` 通道并广播。
- 被动通信：动作结束后由结果文本生成广播（`result` 通道，不调用 LLM）。
- 候选记录：`(上一动作, 执行动作)` 成功/失败/不确定进入候选缓冲；中断时补记
  `(被中断动作, 中断动作)`。
- 事件记录：`planning.replanned`、`planning.timeout`、`race.failed`、
  `race.forced_dual`、`cache.updated` 等写入 episode 事件流。

## 12. 论文版 LaTeX 伪代码（单算法）

以下为可直接放入论文的单个 `algorithm` 环境（`\ForAll` 表示并行执行），
涵盖主流程、动作执行与反馈、缓存维护与评分，省略内部实现细节。

```latex
% 导言区：\usepackage{algorithm} \usepackage{algpseudocode} \usepackage{amsmath}

\begin{algorithm}[t]
\caption{COAP (Cache-Optimized Asynchronous Planning)}
\label{alg:coap}
\begin{algorithmic}[1]
\Function{COAP}{$spec$, $env$, $LLM$, $config$}
  \State $M \gets \text{new TeamMemory}()$
  \State $B \gets \text{new single-slot buffer}()$
  \State $C \gets \text{CacheService.Boot}(action\_space,\ spec)$
  \Comment{empty / llm\_init / reuse\_old; cold scores $\le 0.5$}
  \State $coord \gets \text{new DecisionCoordinator}(C,\ spec)$
  \ForAll{$a \in env.Agents$}
    \State start \textsc{ObservationWorker}$(a)$
  \EndFor
  \State wait until every agent has an initial observation
  \ForAll{$a \in env.Agents$} \Comment{four parallel lines per agent}
    \State start \textsc{ActingWorker}$(a)$ \Comment{$\ell_1$: execution}
    \State start \textsc{PlanningWorker}$(a)$ \Comment{$\ell_2$: timed planning}
    \State start \textsc{CachePlanningWorker}$(a)$ \Comment{$\ell_3$: cache planning}
  \EndFor
  \State start \textsc{CacheWorker}() \Comment{$\ell_4$: cache maintenance}
  \While{$env$ not finished \textbf{and} budget $> 0$ \textbf{and} no fatal error}
    \ForAll{$a \in env.Agents$}
      \If{$a$'s action just finished}
        \State \Call{HandleActionFinished}{$a$}
      \EndIf
      \If{$now \ge nextPlan(a)$ \textbf{and} not planning$(a)$}
        \State $nextPlan(a) \gets now + \Delta t$
        \State \Call{StartPlan}{$a$} \Comment{plan while acting}
      \EndIf
    \EndFor
    \State $outcome \gets env.poll()$; sleep($poll\_interval$)
  \EndWhile
  \State stop all workers; cleanup environment
\EndFunction
\Statex
\Function{HandleActionFinished}{$a$}
  \State reset $nextPlan(a)$
  \If{$coord.forceDual(a)$} \Comment{previous race exhausted replans}
    \State $coord.forceDual(a) \gets false$
    \State $coord.beginRace()$; wake $\ell_3$; \Call{StartRacePlan}{$a$}
    \State \Return
  \EndIf
  \If{$B$ has candidate} \State \Return \Comment{Case A: adopt timed plan}
  \EndIf
  \State $coord.beginRace()$; wake $\ell_3$; \Call{StartRacePlan}{$a$}
\EndFunction
\Statex
\Function{StartPlan}{$a$} \Comment{timed planning during execution}
  \If{$coord.raceActive$} \State \Call{StartRacePlan}{$a$} \EndIf
  \State $p \gets LLM(\text{timed standard prompt},\ latest\ observation)$
  \Comment{$\langle reason\rangle\langle interrupt\rangle\langle action\rangle\langle chat\rangle$}
  \If{$p \ne \bot$} \State \Call{Publish}{$p$} \EndIf
\EndFunction
\Statex
\Function{StartRacePlan}{$a$} \Comment{dual-path race: standard vs.\ cache}
  \State $fb \gets []$
  \For{$i \gets 1$ \textbf{to} $1+\rho$} \Comment{$\rho$ replans}
    \State $p \gets LLM(\text{standard prompt},\ latest\ observation,\ fb)$
    \Comment{$\langle reason\rangle\langle action\rangle\langle chat\rangle$; inject prior failures}
    \If{$p \ne \bot$} \State \Call{Publish}{$p$}; \Return \EndIf
    \State append failure reason to $fb$
  \EndFor
  \State $coord.endRace()$; $coord.forceDual(a) \gets true$
  \Comment{next round restarts from the dual path}
\EndFunction
\Statex
\Function{ActingLoop}{$a$}
  \While{not stopped}
    \If{active is \textbf{null} \textbf{and} $B$ has new proposal $p$}
      \State \Call{Dispatch}{$p$}
    \ElsIf{active action is running}
      \If{interrupt signaled}
        \State stop action; \Call{Finish}{$p$, result, interrupted}
      \Else
        \State poll status; \If{terminal} \Call{Finish}{$p$, result, status} \EndIf
      \EndIf
    \EndIf
  \EndWhile
\EndFunction
\Statex
\Function{Dispatch}{$p$}
  \State $coord.onActionStarted()$ \Comment{clear race state; mark buffer empty}
  \If{$p$ continuous} \State start and track action id
  \Else \State execute once
  \EndIf
  \State \Call{Finish}{$p$, result, status}
\EndFunction
\Statex
\Function{Finish}{$p$, result, status}
  \State $r \gets$ postprocess(result) \Comment{ok / error / counts}
  \State $M.\text{completeAction}(p,\ r,\ status)$
  \State broadcast result text \Comment{passive communication, no LLM}
  \If{$success$ \textbf{and} $p.comm \ne \emptyset$}
    \State commit $p.comm$ to active channel \Comment{active communication}
  \EndIf
  \State $prev \gets p.interruptedPrev \lor coord.lastAction \lor START$
  \State $C.buffer.\text{add}(prev,\ action(p),\ status,\ reasoning)$
  \Comment{success / failure / uncertain candidates}
  \State \Call{ScoreFeedback}{$p$, $success$} \Comment{update matched entry}
  \State $coord.onActionFinished(action(p))$ \Comment{trigger next decision}
\EndFunction
\Statex
\Function{CacheLoop}{}
  \While{not stopped}
    \State \Call{Flush}{}; \Call{UpdateScores}{}; sleep($1\,\mathrm{s}$)
  \EndWhile
\EndFunction
\Statex
\Function{Flush}{}
  \If{$|C.buffer| < \theta$ \textbf{and} $now - lastFlush < \tau$} \State \Return \EndIf
  \State $recs \gets C.buffer.snapshot()$
  \ForAll{$owner \in$ partition($recs$)} \Comment{shared / per-agent}
    \State $agg \gets$ aggregate by $(prev, next)$
    \For{$batch \in$ chunks($agg$, size $=3$)}
      \State $C.contentUpdater.apply(batch)$ \Comment{LLM abstraction; env $>$ LLM}
    \EndFor
  \EndFor
  \State $C.buffer.acknowledge(processed)$
\EndFunction
\Statex
\Function{UpdateScores}{}
  \ForAll{$entry$ with pending feedback}
    \State $r \gets success\_count / attempt\_count$
    \State $\alpha \gets \lambda / (\lambda + attempt\_count)$
    \State $entry.score \gets (1-\alpha)\cdot entry.score + \alpha \cdot r$
    \Comment{confidence-adjusted smoothing}
  \EndFor
\EndFunction
\Statex
\Function{CachePlanning}{$a$} \Comment{$\ell_3$, triggered by race}
  \State $R \gets C.\text{retrieve}(coord.lastAction,\ top\text{-}k\ \text{by score})$
  \If{$R = \emptyset$} \State \Return \EndIf
  \State $C.\text{recordHits}(R)$
  \State $p \gets LLM(\text{cache-action prompt},\ R)$ \Comment{$\langle action\rangle\langle chat\rangle$}
  \If{$p \ne \bot$} \State \Call{Publish}{$p$} \EndIf
\EndFunction
\Statex
\Function{Publish}{$p$}
  \If{$coord.tryClaim()$} \Comment{race winner writes the buffer}
    \State $B.put(p)$; \If{$p.interrupt$} signal interrupt \EndIf
  \Else
    \State $C.buffer.\text{add}(uncertain)$ \Comment{race loser feeds the cache}
  \EndIf
\EndFunction
\end{algorithmic}
\end{algorithm}
```
