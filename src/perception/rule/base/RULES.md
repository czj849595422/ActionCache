# Base Perception Rules

This folder contains parameterized, reusable rule factories. Base rules should not hard-code task-specific entity, item, or block enumerations.

## Conventions

- A rule is callable as `rule(data, context)`.
- Path rules preserve the original path shape. For example, `pick_fields("entities", ["id"])` returns `{"entities": [...]}`.
- Rules are designed to work inside `ChainRule` and aggregate-style `ParallelRule`.
- When adding, removing, or changing a base rule, update this document in the same change.

## `select.py`

- `select_path(path)`: keep only the value at `path`.
- `pick_fields(path, fields)`: keep selected fields in a dict or every dict in a list.
- `drop_fields(path, fields)`: remove selected fields in a dict or every dict in a list.
- `rename_fields(path, mapping)`: rename fields in a dict or every dict in a list.

Example:

```python
pick_fields("entities", ["id", "name", "position", "distance"])
```

## `filter.py`

- `filter_items(path, predicate)`: filter a list with `predicate(item)` or `predicate(item, context)`.
- `filter_by_value(path, field, value)`: keep records where a field equals `value`.
- `filter_by_contains(path, field, text, case_sensitive=False)`: keep records where text appears in a field.
- `filter_by_range(path, field, min_value=None, max_value=None)`: keep numeric field values inside a range.
- `filter_not_none(path, field)`: keep records where a field exists and is not `None`.

Example:

```python
filter_by_range("entities", "distance", max_value=16)
```

## `sort.py`

- `sort_by(path, key, reverse=False)`: sort a list by field path or key function.
- `top_k(path, k, key=None, reverse=False)`: keep first `k` records, optionally sorted first.

Example:

```python
top_k("items", 5, key="distance")
```

## `spatial.py`

- `within_distance(path, max_distance, distance_field="distance")`: keep records inside a distance.
- `outside_distance(path, min_distance, distance_field="distance")`: keep records outside a distance.
- `within_box(path, center_path="self.position", half_size=(x, y, z))`: keep records inside an axis-aligned box.
- `in_view(path, fov_degrees=90)`: keep records inside the current self yaw/pitch field of view.

Example:

```python
in_view("entities", fov_degrees=90)
```

## `entity.py`

- `entities()`: select all entities.
- `entities_by_type(type_name)`: filter entities by `type`.
- `entities_by_name_contains(text)`: filter entities by `name` substring.
- `entities_with_field(field)`: keep entities containing a field.
- `living_entities()`: keep entities with `health`.
- `nearest_entities(k=10)`: nearest entities by `distance`.

Example:

```python
entities_by_type("hostile")
```

## `item.py`

- `items()`: select all dropped item views.
- `items_by_name_contains(text)`: filter by `item.name` substring.
- `items_by_count(min_count=None, max_count=None)`: filter by `item.count`.
- `nearest_items(k=10)`: nearest dropped items by `distance`.

Example:

```python
items_by_name_contains("diamond")
```

## `block.py`

- `blocks()`: select all blocks.
- `blocks_by_name_contains(text)`: filter by block name substring.
- `blocks_by_material(material)`: filter by exact `material`.
- `blocks_with_property(key, value=None)`: keep blocks with a property, optionally matching a value.
- `nearest_blocks(k=20)`: nearest blocks by `distance`.

Example:

```python
blocks_with_property("facing")
```
