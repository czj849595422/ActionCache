# demo_survival

Demo-only survival movement behavior for `src.runner.demo_env`.

## Commands

- `demo_survival.flee_nearest_hostile`

The skill continuously checks the nearest hostile. If the hostile is closer than `safeDistance`, it sets a pathfinder target away from the hostile while clamping x/z to configured bounds.

This is not the final survival skill. It is only a simple observable demo behavior.
