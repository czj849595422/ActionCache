"""Prompt-facing filters for raw perception payloads."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


DEFAULT_OBSERVER_USERNAME_PREFIXES = ("observer",)


def filter_observer_players(
    data: Any,
    *,
    prefixes: Iterable[str] = DEFAULT_OBSERVER_USERNAME_PREFIXES,
) -> Any:
    """Remove observer and spectator accounts from player-facing perception data."""
    if not isinstance(data, dict):
        return data
    active_prefixes = _normalized_prefixes(prefixes)

    self_username = _username_from_mapping(data.get("self"))
    players = data.get("players")
    entities = data.get("entities")

    filtered_players = players
    removed_player_entity_ids: set[Any] = set()
    removed_player_uuids: set[Any] = set()
    if isinstance(players, list):
        filtered_players = []
        for player in players:
            if not isinstance(player, dict):
                filtered_players.append(player)
                continue
            username = _normalized_username(player.get("username"))
            if (
                username != self_username
                and (
                    (username is not None and _matches_prefix(username, active_prefixes))
                    or _is_spectator_player(player)
                )
            ):
                entity = (
                    player.get("entity")
                    if isinstance(player.get("entity"), dict)
                    else {}
                )
                _remember_entity_identity(
                    entity,
                    removed_player_entity_ids,
                    removed_player_uuids,
                )
                continue
            filtered_players.append(player)

    filtered_entities = entities
    if isinstance(entities, list):
        filtered_entities = [
            entity
            for entity in entities
            if not _is_filtered_entity(
                entity,
                self_username,
                active_prefixes,
                removed_player_entity_ids,
                removed_player_uuids,
            )
        ]

    if filtered_players is players and filtered_entities is entities:
        return data
    if filtered_players == players and filtered_entities == entities:
        return data

    result = dict(data)
    if isinstance(filtered_players, list):
        result["players"] = filtered_players
    if isinstance(filtered_entities, list):
        result["entities"] = filtered_entities
    metadata = data.get("metadata")
    if isinstance(metadata, Mapping):
        result["metadata"] = _metadata_with_counts(
            metadata,
            players_count=(
                len(filtered_players) if isinstance(filtered_players, list) else None
            ),
            entities_count=(
                len(filtered_entities) if isinstance(filtered_entities, list) else None
            ),
        )
    return result


def _normalized_prefixes(prefixes: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        value
        for prefix in prefixes
        if (value := _normalized_username(prefix))
    )


def _normalized_username(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _matches_prefix(username: str, prefixes: tuple[str, ...]) -> bool:
    return any(username.startswith(prefix) for prefix in prefixes)


def _username_from_mapping(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    return _normalized_username(value.get("username"))


def _remember_entity_identity(
    entity: Mapping[str, Any],
    entity_ids: set[Any],
    uuids: set[Any],
) -> None:
    for key in ("id", "entityId"):
        value = entity.get(key)
        if value is not None:
            entity_ids.add(value)
    uuid = entity.get("uuid")
    if uuid is not None:
        uuids.add(uuid)


def _is_filtered_entity(
    entity: Any,
    self_username: str | None,
    prefixes: tuple[str, ...],
    removed_player_entity_ids: set[Any],
    removed_player_uuids: set[Any],
) -> bool:
    if not isinstance(entity, dict):
        return False
    username = _normalized_username(entity.get("username"))
    if username and username != self_username and _matches_prefix(username, prefixes):
        return True
    if username and username != self_username and _is_spectator_mapping(entity):
        return True
    entity_id = entity.get("id", entity.get("entityId"))
    if entity_id is not None and entity_id in removed_player_entity_ids:
        return True
    uuid = entity.get("uuid")
    return uuid is not None and uuid in removed_player_uuids


def _is_spectator_player(player: Mapping[str, Any]) -> bool:
    if _is_spectator_mapping(player):
        return True
    entity = player.get("entity")
    return isinstance(entity, Mapping) and _is_spectator_mapping(entity)


def _is_spectator_mapping(value: Mapping[str, Any]) -> bool:
    for key in ("gamemode", "gameMode", "game_mode"):
        mode = value.get(key)
        if mode == 3:
            return True
        if isinstance(mode, str) and mode.strip().lower() in {"3", "spectator"}:
            return True
    return False


def _metadata_with_counts(
    metadata: Any,
    *,
    players_count: int | None,
    entities_count: int | None,
) -> Any:
    if not isinstance(metadata, Mapping):
        return metadata
    result = dict(metadata)
    counts = metadata.get("counts")
    if isinstance(counts, Mapping):
        result_counts = dict(counts)
        if players_count is not None:
            result_counts["players"] = players_count
        if entities_count is not None:
            result_counts["entities"] = entities_count
        result["counts"] = result_counts
    return result


__all__ = [
    "DEFAULT_OBSERVER_USERNAME_PREFIXES",
    "filter_observer_players",
]
