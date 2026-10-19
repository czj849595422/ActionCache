# block

`block` wraps Mineflayer native block inspection, digging, and placement APIs for one target coordinate.

## Commands

- `block.inspect`: inspect the block at `x, y, z`.
- `block.break`: move near `x, y, z` and dig the target block.
- `block.place`: move near `x, y, z`, equip `item`, and place it into the target air block.
- `block.inspect_region`: inspect block counts and block records in a cuboid region.
- `block.break_region`: break matching blocks in a cuboid region.
- `block.place_region`: place one block item into eligible positions in a cuboid region.

## Params

All commands accept either top-level `x`, `y`, `z` numbers or `position: {x, y, z}`. Coordinates are floored to block coordinates.

### `block.inspect`

- `x`, `y`, `z`: target block coordinate.
- `position`: optional target block coordinate object.
- `max_distance`: maximum allowed distance from the agent. Defaults to `45`.
- `distance_check_enabled`: whether to enforce `max_distance`. Defaults to `false` (no distance limit).

### `block.break`

- `x`, `y`, `z`: target block coordinate.
- `position`: optional target block coordinate object.
- `max_distance`: maximum allowed distance from the agent. Defaults to `45`.
- `approach_range`: pathfinder range for approaching the block. Defaults to `3`.
- `timeout_ms`: navigation/dig timeout in milliseconds. Defaults to `20000`.
- `collect_drops`: whether to collect dropped items after digging. Defaults to `true` (the bot walks to each dropped item and picks it up; the result reports `collected_count` and `collection_supported: true`).
- `distance_check_enabled`: whether to enforce `max_distance`. Defaults to `false` (no distance limit).

### `block.place`

- `item`: inventory item to place, such as `stone` or `crafting_table`. `minecraft:` prefixes are accepted.
- `x`, `y`, `z`: target air block coordinate.
- `position`: optional target block coordinate object.
- `max_distance`: maximum allowed distance from the agent. Defaults to `45`.
- `approach_range`: pathfinder range for approaching the target. Defaults to `3`.
- `timeout_ms`: navigation/equip/place timeout in milliseconds. Defaults to `20000`.
- `distance_check_enabled`: whether to enforce `max_distance`. Defaults to `false` (no distance limit).

### Region Coordinates

Region commands accept:

- `from`: one corner coordinate object, such as `{"x": 0, "y": 64, "z": 0}`.
- `to`: the opposite corner coordinate object.
- `max_blocks`: maximum cuboid volume allowed for this operation. Defaults to `256` for inspection and `128` for mutation.
- `max_distance`: per-position distance limit from the agent. Defaults to `45`.

Coordinates are normalized into inclusive `min` / `max` bounds.

### `block.inspect_region`

- `from`, `to`: cuboid region corners.
- `include_air`: whether to include air blocks in the returned `blocks` list. Defaults to `false`.
- `max_distance`: positions farther than this are skipped.
- `distance_check_enabled`: whether to enforce `max_distance` per position. Defaults to `false` (no distance limit).
- `max_blocks`: maximum region volume. Defaults to `256`.

### `block.break_region`

- `from`, `to`: cuboid region corners.
- `include_blocks`: optional allow-list. When provided, only these block names are broken.
- `exclude_blocks`: optional deny-list.
- `allow_protected`: whether to allow breaking protected block types. Defaults to `false`.
- `order`: one of `nearest`, `natural`, `bottom_up`, or `top_down`. Defaults to `nearest`.
- `strict`: if `true`, stop after the first execution failure. Defaults to `false`.
- `collect_drops`: whether to collect dropped items after each break. Defaults to `true`.
- `distance_check_enabled`: whether to enforce `max_distance` per position. Defaults to `false` (no distance limit).
- `timeout_ms`: total intended timeout budget recorded in params. Defaults to `120000`.
- `per_block_timeout_ms`: timeout passed to each single-block break. Defaults to `20000`.

Protected blocks currently include bedrock, barrier, chest-like containers, furnace-like blocks, and crafting tables.

### `block.place_region`

- `item`: inventory block item to place.
- `from`, `to`: cuboid region corners.
- `replace_policy`: `air_only`, `replace_matching`, or `replace_any`. Defaults to `air_only`.
- `replace_blocks`: required when `replace_policy` is `replace_matching`.
- `allow_protected`: whether replacement may break protected block types. Defaults to `false`.
- `order`: one of `nearest`, `natural`, `bottom_up`, or `top_down`. Defaults to `bottom_up`.
- `strict`: if `true`, stop after the first execution failure. Defaults to `false`.
- `collect_drops`: whether to collect drops from blocks broken during replacement. Defaults to `true`.
- `distance_check_enabled`: whether to enforce `max_distance` per position. Defaults to `false` (no distance limit).
- `timeout_ms`: total intended timeout budget recorded in params. Defaults to `120000`.
- `per_block_timeout_ms`: timeout passed to each single-block break/place. Defaults to `20000`.

`place_region` checks that the inventory contains enough items for the planned placement count before executing.

## Output

All commands return JSON-safe block records with fields such as `name`, `displayName`, `position`, `distance`, `boundingBox`, `diggable`, and `hardness`. Mutating commands also return an inventory snapshot.

## Notes

`block.place` only places into air-like target blocks and requires at least one non-air neighboring reference block. It does not replace existing blocks automatically.
`block.place_region` can replace existing blocks only when `replace_policy` is not `air_only`; replacement is implemented as a normal Mineflayer dig followed by normal placement, not by server commands.
