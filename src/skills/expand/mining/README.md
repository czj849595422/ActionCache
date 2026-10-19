# mining

Task-specific ore mining skills for the `mining` task, mirroring the
`expand/trade` task-skill pattern.

## Commands

- `mining.mine_lapis` / `mine_iron` / `mine_gold` / `mine_emerald` / `mine_diamond` / `mine_netherite`

Each command is **continuous** (start/update/stop/status) and shares `switch_group: mining`,
so starting a new mining target automatically interrupts the previous one.

## Config

The bridge process must be started with `MINING_ORE_BLOCKS` set to a comma-separated list of
`minecraft:block` names (e.g., `minecraft:iron_block,minecraft:iron_ore`). The skill maps each
block to a mineral command by stripping `minecraft:` and the `_block` / `_ore` / `_debris`
suffix; e.g., `iron_block` and `deepslate_iron_ore` both belong to `mine_iron`.
When unset, execution fails with `mining_config_missing`.

## Params

- `positions` (or single `x/y/z`): candidate ore coordinates.
- `strict`: `true` fails the whole action when the held tool cannot collect a coordinate; `false`
  (default) records it in `tool_required_coords` and continues.
- `max_group_size` (default 16), `max_distance` (default 45), `collect_drops` (default true),
  `per_block_timeout_ms` (default 20000), `max_obstacles_per_block` (default 8), `approach_range` (default 3).

## Behavior

- For each candidate coordinate: skip it (into `skipped_coords`) if the block is not this mineral's
  family; verify the held tool against the block's `harvestTools`; then mine the 6-face connected
  group via `block.mine`.
- Group members are re-checked before digging; members that are no longer the mineral are skipped.
- When a target cannot be dug directly, covering obstacle blocks are broken first
  (`obstacles_broken`).
