# AutoGen-Style Hierarchical A2A Baseline

This baseline implements the AutoGen idea as an isolated project-native
method. It does not import the official AutoGen package.

## Roles

- `Commander`: an LLM-only planner with no Minecraft entity, bridge, or action
  capability.
- Executors: existing `SkillAgent` instances such as Alice and Bob. They own
  Minecraft bodies and execute actions through `BaseActionPlanner`.

## Communication

- Commander assignment and executor terminal reports are private A2A messages.
- Commander can read the full A2A history.
- Each executor can read only its own Commander thread.
- Executor action results are public task context and can be read by all
  executors in later planning turns.

## Task Descriptions

- Commander uses AutoGen-specific descriptions from
  `src/baseline/AutoGen/descriptions/<task_id>.commander.description.txt`.
- Executors use the normal task-folder description resolved by
  `TaskSpec.load_description_text()`.
- YAML `description` fields are not used for prompt injection.

## One-Call Executor Rule

Each executor planning turn uses one LLM call. The response must decide whether
the current subtask is done. If it is not done, the same response must provide
the next executable action. Terminal subtask reports are private to Commander.

## Execution Boundary

Commander never emits Minecraft actions. Executor actions are parsed into the
existing `COAPProposal` / `ActionUnit` path and executed by `BasicActingWorker`
and `BaseActionPlanner`, preserving allowlists, conflict handling, recording,
and task-manager evaluation.
