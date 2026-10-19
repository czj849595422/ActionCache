# navigation

`navigation` wraps `mineflayer-pathfinder` instead of reimplementing keyboard movement.

## Commands

- `navigation.goto_position`: interruptible pathfinding to a target position or target entity.
- `navigation.follow_position`: continuous dynamic coordinate goal that can be updated or stopped.
- `navigation.follow_entity`: continuous entity following with dynamic re-targeting and optional range constraint.
- `navigation.goto_block`: find the nearest block by name and move next to it.
- `navigation.explore`: move segment by segment along a direction while scanning nearby blocks (targeted search or general exploration).

## Navigation Semantics

- Static coordinate navigation: `navigation.goto_position` starts a continuous pathfinder action toward a fixed coordinate. It completes on arrival and can be stopped while moving; the target is not hot-updatable.
- Entity navigation: `navigation.goto_position` also accepts `entity_id` / `entity_uuid` / `entity_name` (mutually exclusive with coordinates) and follows the entity until the bot is within `range`, or fails on timeout / entity lost. It can also be stopped while moving.
- Coordinate-following navigation: `navigation.follow_position` keeps a continuous pathfinder goal and can update `x`, `y`, `z`, and `range` while running.
- Entity-following navigation: `navigation.follow_entity` tracks a Mineflayer entity directly. Each tick re-sets a `GoalFollow` goal to keep up with movement (dynamic correction); if the entity disappears it auto-stops with `entity_lost`, and if `range_enabled` + `max_range` are set and the entity moves beyond the range it auto-stops with `entity_out_of_range`.
- Block navigation: `navigation.goto_block` finds the nearest block whose name matches `block_name` within `search_max_distance` and walks to it, mirroring the old `goto(locationName)` behavior.

The full-chain test prepares a short walkable strip in the benchmark world and verifies that `goto_position` changes the agent coordinates and ends close to the requested target.

## Params

- `x`, `y`, `z`: target position.
- `range`: acceptable distance to the target. Defaults to `1`.
- `timeout`: optional timeout in seconds for `goto_position`; `0` disables the timeout.
- `entity_id` / `entity_uuid` / `entity_name`: target entity reference for `goto_position` / `follow_entity`. `entity_id` corresponds to `entities[].id` in raw perception. `goto_position` continues toward a known entity regardless of travel distance.
- `follow_range`: desired follow distance for `follow_entity`. Defaults to `2.5`.
- `range_enabled` / `max_range`: optional range constraint for `follow_entity`; it does not limit `goto_position`.
- `block_name`: block name for `goto_block` (corresponds to `blocks[].name`, `minecraft:` prefix accepted).
- `search_max_distance`: search radius for `goto_block`. Defaults to `48`.

### `navigation.explore`

- `direction`: `north` / `south` / `east` / `west` / `up` / `down` or `{x, y, z}` vector. Defaults to a random horizontal direction; underground ore-like targets default to `down`.
- `target`: block name or list of names (e.g., `diamond_ore`). When provided the action performs a targeted search; without it, it performs general exploration and records discoveries.
- `duration` (seconds, default 60) / `duration_ticks`: exploration time limit; the action auto-stops when reached.
- `type`: `first` (default) stops and reports on the first discovery; `record` keeps exploring until the stop condition and returns deduplicated discoveries.
- `scan_interval`: move N blocks between scans (default 4).
- `max_distance`: maximum distance from the start point (default 128, capped at 512); prevents getting lost.

The action moves in segments, scans nearby blocks at each segment (`findBlocks`), rotates direction when stuck, and reports `reason` (`target_found` / `duration_reached` / `max_distance_reached` / `stuck`) plus `found` / `discoveries` in the result.

## Runtime

Requires `mineflayer-pathfinder`. The bridge plugin manager loads the plugin and exposes capability status through `/skill/list`.
