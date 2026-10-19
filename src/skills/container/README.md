# container

`container` wraps Mineflayer container APIs for chest-like blocks (chest / trapped_chest / barrel).

## Commands

- `container.inspect_chest`: open a chest and return its contents plus inventory snapshot.
- `container.pick_object_from_chest`: withdraw requested items after pre-checking chest quantity and inventory space.
- `container.place_object_to_chest`: deposit requested items after pre-checking inventory quantity and chest space.
- `container.register_chests`: scan chest-like blocks within `max_distance` and assign each a stable numeric `chest_id`.
- `container.list_chests`: list registered chests (`chest_id`, position, type, presence).

## Chest targeting

Every transfer command accepts any one of:

- `chest_id`: registered id returned by `register_chests` (recommended when multiple chests exist).
- `chest_name`: registered unique name returned by `register_chests` (alternative to `chest_id`).
- `chest: {position: {x, y, z}}` or `chest: {x, y, z}`: direct position from perception.
- `position: {x, y, z}`: fallback position.
- nothing: the nearest chest-like block within `max_distance` (default 45).

Example flow:

```json
{"action": "container.register_chests", "params": {"max_distance": 45, "names": {"9,8,5": "storage", "10,8,5": "food"}}}
{"action": "container.list_chests", "params": {}}
{"action": "container.place_object_to_chest", "params": {"chest_id": 2, "items": [{"name": "cobblestone", "count": 16}]}}
{"action": "container.pick_object_from_chest", "params": {"chest_name": "storage", "items": [{"name": "cobblestone", "count": 4}]}}
```

## Notes

- The registry is **file-backed and shared across processes**: all bridge processes (e.g., bot1/bot2) read and write the same JSON file, so chests registered by one agent are visible to every other agent. Default file: `<repo>/runtime/chest_registry.json`; override with the `CHEST_REGISTRY_FILE` environment variable.
- Registration and name uniqueness are enforced globally through the shared file. Concurrent registration from multiple agents is last-writer-wins on the file; the file is written atomically (temp file + rename).
- The registry persists across restarts; re-registering the same position returns the existing id/name. A chest registered by another agent is usable only when it is within the operating agent's `max_distance`.
- Each registered chest has a numeric `chest_id` and an optional unique `chest_name`; both are valid targets in later commands. Names are assigned via `register_chests.names` (keyed by `"x,y,z"` position); duplicate names fail with `chest_name_duplicate`.
- Double chests are normalized: only the lexicographically smaller half-block is registered, so one double chest gets one `chest_id`.
- Opening uses `activateBlock` plus a `windowOpen` wait with `open_timeout_ms` (default 5000); unknown `chest_id` fails with `chest_id_unknown`.
