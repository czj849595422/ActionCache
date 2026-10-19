# furnace

`furnace` wraps Mineflayer furnace windows with an explicit session and auto-open behavior.

## Commands

- `furnace.find_nearby`: list furnace-like blocks within `radius` (default 20).
- `furnace.open`: open a furnace and establish a session; returns slots, fuel/progress state, and inventory.
- `furnace.put_fuel` / `furnace.put_input`: place fuel/input (count defaults to 1).
- `furnace.take_fuel` / `furnace.take_input` / `furnace.take_output`: take items from a slot.
- `furnace.smelt_item`: integrated smelting (see below).
- `furnace.close`: close the current furnace session (idempotent).

## Auto-open

`put_fuel`, `put_input`, `take_fuel`, `take_input`, `take_output` and `smelt_item`
**auto-open the furnace** when no session exists: you no longer need to call `open` first.
Pass `furnace` / `position` to target a specific furnace, or omit them to use the
nearest furnace within `max_distance` (default 20). Once a session exists it is reused;
call `close` (or `smelt_item`, which closes at the end) to switch furnaces.

## smelt_item

```json
{"action": "furnace.smelt_item", "params": {"item": "iron_ore", "fuel": "coal", "count": 4, "wait": true}}
```

- `item` / `fuel`: input and fuel item names (required).
- `count`: number of items to smelt. Defaults to `1`.
- `wait`: `true` (default) waits for each item to finish smelting (polling the output slot)
  and takes it; `false` only configures fuel/input, then closes the furnace so other
  actions can run while smelting continues server-side.

`smelt_item` always closes the furnace session at the end (success or failure).

## Notes

- Furnace progress and fuel are read from `craft_progress_bar` packets and exposed as
  `furnace_state.fuel_seconds` / `progress_seconds`.
- Slot operations pre-check quantities and inventory space, and recover existing slot
  items before overwriting (`put_fuel` / `put_input`).
