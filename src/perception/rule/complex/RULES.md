# Complex Perception Rules

This folder contains task-oriented rule groups composed from `src/perception/rule/base/` rules. Complex rules should reuse base rules and return `RuleGroup` objects where practical.

When adding, removing, or changing a complex rule, update this document in the same change.

Snapshot/overview rules preserve the full `self` object from raw perception. Entity, item, and block lists may still be compacted by task-specific field selection.

## `combat.py`

- `nearest_hostiles(k=8, radius=None)`: nearest hostile entities with compact combat fields.
- `combat_snapshot(k=8, radius=None)`: full self state plus nearest hostiles.
- `threat_summary(k=8, radius=None)`: hostile count and nearest hostile distance.

Example:

```python
rule = combat_snapshot(k=5, radius=20)
result = rule.apply(raw, context)
```

## `pickup.py`

- `nearest_pickups(k=10, radius=None)`: nearest dropped item views.
- `valuable_pickups(names=None, k=10, radius=None)`: nearest dropped items whose names contain any token in `names`.
- `pickup_snapshot(k=10, radius=None)`: full self state plus nearest dropped items.

Example:

```python
rule = valuable_pickups(names=["diamond", "iron"], k=5)
```

## `navigation.py`

- `nearby_obstacles(k=30, radius=None)`: nearby non-transparent blocks for obstacle awareness.
- `navigation_snapshot(block_k=50, radius=None)`: full self state plus obstacle-oriented block data.
- `walkable_surface_snapshot(k=30, radius=None)`: nearby solid blocks as first-pass walkable candidates.

Example:

```python
rule = navigation_snapshot(block_k=30, radius=12)
```

## `environment.py`

- `count_by_path(source_path, field, output_path, top_n=None)`: generic count summary helper.
- `entity_type_counts(top_n=None)`: count entities by `type`.
- `block_name_counts(top_n=None)`: count blocks by `name`.
- `item_name_counts(top_n=None)`: count dropped items by `item.name`.
- `environment_counts(top_n=None)`: entity, item, and block count summaries.
- `nearby_overview(entity_k=10, block_k=20, item_k=10)`: full self state, nearest records, and summary counts.

Example:

```python
rule = nearby_overview(entity_k=8, block_k=20, item_k=5)
```

## Excluded For Now

No `llm.py` complex rule is defined here. LLM-specific compaction will be developed with the later LLM/session modules.
