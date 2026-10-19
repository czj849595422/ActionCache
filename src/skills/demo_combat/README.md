# demo_combat

Demo-only combat behavior for `src.runner.demo_env`.

## Commands

- `demo_combat.lock_nearest_hostile`

The skill selects the nearest hostile once when it starts and keeps the same target id until that entity disappears. It intentionally does not switch targets when another hostile becomes closer.

This is not the final combat skill. It uses Mineflayer native `bot.attack(entity)` plus `mineflayer-pathfinder` for approach behavior.
