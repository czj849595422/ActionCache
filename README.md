# ActionCache

ActionCache 是一个面向 Minecraft 多智能体任务的实验项目，提供任务配置、环境桥接、LLM 调用、动作执行和多种 baseline。项目的核心目录如下：

- `assert/`：可复用的基准环境与任务资源。
- `script/`：演示、校验和运行入口；可直接从仓库根目录执行。
- `src/`：环境、任务、LLM、记录器和 baseline 的可复用实现。

实验结果、临时探测脚本和本地凭据不应提交到仓库；相关目录和文件已在 `.gitignore` 中配置。

## 环境配置

### 1. Python

建议使用 Python 3.12 或更高版本，并在项目根目录创建虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

本项目也可以使用已有的 Conda 环境，例如：

```powershell
conda create -n actioncache python=3.12
conda activate actioncache
python -m pip install -r requirements.txt
```

### 2. Minecraft、Java 和 Mineflayer

运行真实环境任务还需要：

- 可执行的 Java，并确保 `java` 在 `PATH` 中。
- Minecraft 1.19 服务端。默认目录是 `src/minecraft/`，其中应存在 `server.jar`、`eula.txt` 和 `server.properties`。
- Node.js 与 npm，并在 Mineflayer 目录安装 JavaScript 依赖：

```powershell
cd src\env\mineflayer
npm install
cd ..\..\..
```

服务器操作由 `src/launcher/mcrcon.py` 统一通过 RCON 发送；请在 `src/minecraft/server.properties` 中确认 RCON 已配置，并保证端口没有被其他进程占用。不要把 RCON 密码或其他凭据写入代码、日志或提交记录。

### 3. LLM 配置

运行需要 LLM 的 baseline 前，在当前 PowerShell 会话中设置环境变量，或在项目根目录创建本地 `.env` 文件。`.env` 文件会被忽略，不能提交：

```powershell
$env:LLM_API_URL = "https://your-openai-compatible-endpoint/v1"
$env:LLM_API_KEY = "<your-api-key>"
$env:LLM_MODEL = "<your-model-name>"
```

可选配置包括 `LLM_TIMEOUT`、`LLM_MAX_RETRIES`、`LLM_THINKING_ENABLED`、`LLM_EXTRA_HEADERS` 和 `LLM_EXTRA_BODY`。`script/demo_*.py` 默认读取项目根目录的 `.env`；也可以通过 `--env-file` 指定其他本地配置文件。

`requirements.txt` 覆盖当前 baseline 和主流程直接使用的 Python 包。仓库中的旧版 `src/env/` 兼容层还引用外部 `teamcraft` 包，但该包没有随本仓库提供，也没有可验证的安装来源；COAP 及 `script/demo_*.py` 主流程不使用这条旧路径。

## 任务

以下是当前文档覆盖的非 trade 任务：

- `mining`：多智能体协作挖掘随机出现的矿石，按矿石价值和工具能力争取更高收益。
- `monster_clearance`：在限定区域内巡查并消灭至少 10 只怪物，同时避免队员死亡。
- `pvp`：两支队伍在限定场地内进行对战，消灭敌方全部成员；需要分配装备并协同作战。
- `boss_combat`：四名队员协作击败 Wither，处理补给、远程输出、治疗和撤离。
- `debug`：无正式任务目标的动作与环境调试任务，不用于正式实验。

任务规格和初始化配置位于 `src/tasks/specs/`、`src/tasks/<task>/config/` 及 `src/tasks/<task>/*.description.txt`。可以先列出脚本支持的任务：

```powershell
python script\demo_coap.py --list-tasks
```

## 支持的 baseline

当前 `script/` 中有运行入口的 baseline 包括：

- `BasicAsync`：普通异步规划与执行。
- `DirectAction`：直接生成动作，不单独进行规划阶段。
- `Parallelized`：并行规划-执行（PPA）baseline。
- `AgenticCache`：使用动作缓存辅助规划。
- `AutoGen`：层级式 commander/executor 协作 baseline。
- `COAP`：以动作缓存为中心，结合缓存规划、验证、定时规划和评分更新。

对应的主要入口为 `script/demo_basic_async.py`、`script/demo_direct_action.py`、`script/demo_parallelized.py`、`script/demo_agentic_cache.py`、`script/demo_autogen.py` 和 `script/demo_coap.py`。trade 任务及其批量脚本不在本 README 的任务说明范围内。

## COAP 运行示例

以下命令均从项目根目录执行。建议先运行离线检查，确认任务配置和 Python 导入正常：

```powershell
python script\demo_coap.py --task mining --check
```

确认 Minecraft 服务端、Mineflayer、RCON 和 LLM 配置均已就绪后，运行少量 episode：

```powershell
python script\demo_coap.py `
  --task mining `
  --task-seed 0 `
  --episode-count 3 `
  --record-dir main_result
```

也可以运行其他任务；例如：

```powershell
python script\demo_coap.py --task monster_clearance --episode-count 3 --record-dir main_result
python script\demo_coap.py --task boss_combat --episode-count 3 --record-dir main_result
```

PVP 需要使用专用入口，并分别配置两支队伍的方法。例如让队伍 A 使用 COAP、队伍 B 使用 DirectAction：

```powershell
python script\demo_pvp.py `
  --task pvp `
  --task-seed 0 `
  --team-a-method coap `
  --team-b-method direct_action `
  --team-size 4 `
  --episode-count 3 `
  --record-dir main_result
```

其中 `--team-a-method` 和 `--team-b-method` 分别指定两支队伍的 baseline；可用方法包括 `basic_async`、`direct_action`、`parallelized`、`agentic_cache`、`autogen` 和 `coap`。`--team-size 4` 与默认 PVP 任务的每队四名 agent 对应。

常用 COAP 控制选项包括：

- `--cache-boot-mode llm_init|reuse_previous|empty`：选择缓存初始化方式。
- `--cache-source <path>`：从已有缓存快照启动。
- `--disable-timed-planning`：关闭定时普通规划。
- `--disable-cache-planning`：关闭缓存规划。
- `--disable-standard-verification`：关闭动作结束后的普通规划验证。
- `--disable-cache-reasoning-optimization`：关闭缓存候选的内容推理更新。
- `--disable-score-update`：关闭缓存评分更新。

项目中已有按任务运行全部 baseline 的 Windows 示例，例如：

```powershell
cmd /c script\run_mining_all_methods.cmd
```

这些命令会写入实验记录目录；请使用被 `.gitignore` 忽略的输出目录，不要将实验数据、日志或缓存快照提交到仓库。

## 测试与静态检查

不启动 Minecraft 和不调用 LLM 时，可以运行本地单元测试：

```powershell
python -m unittest discover -s test -p "test_*.py" -v
```

`--check` 和单元测试只验证配置、导入或离线逻辑；它们不能替代真实服务器、RCON、Mineflayer 和 LLM 的端到端验证。
