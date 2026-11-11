"""Minimal CQRS buses.

Commands change state and return at most an id or a small result; queries
never change state and return read models (plain dicts ready for JSON).
Handlers are registered per message class in the composition root
(:mod:`autoeventplay.container`). Both sync and async handlers are supported.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("aep.bus")

Handler = Callable[[Any], Any | Awaitable[Any]]


class Command:
    """Marker base class for commands (write side)."""


class Query:
    """Marker base class for queries (read side)."""


class DomainError(Exception):
    """Validation or business rule violation; shown to the user."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class NotFound(DomainError):
    def __init__(self, message: str = "Not found."):
        super().__init__(message, 404)


class _Bus:
    kind = "message"
    base: type = object

    def __init__(self) -> None:
        self._handlers: dict[type, Handler] = {}

    def register(self, message_type: type, handler: Handler) -> None:
        if not issubclass(message_type, self.base):
            raise TypeError(f"{message_type.__name__} is not a {self.base.__name__}")
        if message_type in self._handlers:
            raise ValueError(f"handler for {message_type.__name__} already registered")
        self._handlers[message_type] = handler

    def registered(self) -> list[str]:
        return sorted(t.__name__ for t in self._handlers)

    async def dispatch(self, message: Any) -> Any:
        handler = self._handlers.get(type(message))
        if handler is None:
            raise LookupError(f"no {self.kind} handler for {type(message).__name__}")
        result = handler(message)
        if inspect.isawaitable(result):
            result = await result
        return result


class CommandBus(_Bus):
    kind = "command"
    base = Command

    async def dispatch(self, message: Any) -> Any:
        log.debug("command %s", type(message).__name__)
        return await super().dispatch(message)


class QueryBus(_Bus):
    kind = "query"
    base = Query
