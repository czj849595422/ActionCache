# inventory

`inventory` wraps Mineflayer native inventory APIs. It does not require extra Mineflayer plugins.

## Commands

- `inventory.list`: return current inventory items, held item, and known equipment slots.
- `inventory.count_item`: count items matching a name.
- `inventory.has_item`: check whether the agent has at least a required count.
- `inventory.equip`: equip a matching item to a destination, defaulting to `hand`.

## Params

### `inventory.count_item`

- `name`: item name. `minecraft:` prefixes are accepted and normalized.

### `inventory.has_item`

- `name`: item name.
- `count`: required count. Defaults to `1`.

### `inventory.equip`

- `name`: item name.
- `destination`: Mineflayer equipment destination. Defaults to `hand`.

## Output

All commands return JSON-safe item records with fields such as `name`, `displayName`, `count`, `slot`, `type`, `metadata`, and durability fields when available.

## Notes

Read-only commands do not take planner locks. `inventory.equip` takes `inventory` and `item_use` locks because it mutates the agent's held/equipped item state.
