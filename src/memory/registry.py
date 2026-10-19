"""记忆实现注册表：按 MemoryKind 注册/创建。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .base import MemoryStore
from .schemas import (
    MemoryConflictError,
    MemoryError,
    MemoryKind,
    _coerce_enum,
)

MemoryFactory = Callable[..., MemoryStore]


@dataclass
class _MemoryRegistration:
    kind: MemoryKind
    factory: MemoryFactory
    capabilities: tuple[type, ...] = ()


class MemoryRegistry:
    """按 MemoryKind 注册记忆实现工厂并创建实例。"""

    def __init__(self) -> None:
        self._registrations: dict[MemoryKind, _MemoryRegistration] = {}

    def register(
        self,
        kind: MemoryKind | str,
        factory: MemoryFactory,
        *,
        capabilities: tuple[type, ...] = (),
        overwrite: bool = False,
    ) -> None:
        kind = _coerce_enum(kind, MemoryKind, "kind")
        if kind in self._registrations and not overwrite:
            raise MemoryConflictError(
                f"Memory kind already registered: {kind.value}",
                operation="register",
            )
        if not callable(factory):
            raise MemoryError("factory must be callable", operation="register")
        self._registrations[kind] = _MemoryRegistration(
            kind=kind,
            factory=factory,
            capabilities=tuple(capabilities),
        )

    def create(self, kind: MemoryKind | str, **kwargs: Any) -> MemoryStore:
        kind = _coerce_enum(kind, MemoryKind, "kind")
        registration = self._registrations.get(kind)
        if registration is None:
            raise MemoryError(
                f"No memory implementation registered for kind: {kind.value}",
                operation="create",
            )
        return registration.factory(**kwargs)

    def available_kinds(self) -> list[str]:
        return [kind.value for kind in sorted(self._registrations)]

    def supports(
        self,
        kind: MemoryKind | str,
        capability: type,
    ) -> bool:
        try:
            kind = _coerce_enum(kind, MemoryKind, "kind")
        except ValueError:
            return False
        registration = self._registrations.get(kind)
        if registration is None:
            return False
        return any(_is_capability(capability, registered) for registered in registration.capabilities)


def _is_capability(requested: type, registered: type) -> bool:
    if requested is registered:
        return True
    try:
        return issubclass(registered, requested)
    except TypeError:
        return False
