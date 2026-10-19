# wait

`wait.wait` is a minimal lifecycle skill used to verify planner and bridge skill runtime behavior.

## Commands

- `wait.wait`

## Params

- `seconds`: number of seconds to wait.

## Modes

- `oneshot`: resolves after `seconds`.
- `continuous`: starts a timer, supports `update` with a new `seconds` value, and supports `stop`.

## Output

Returns elapsed/completion metadata:

```json
{"seconds": 1, "completed": true}
```
