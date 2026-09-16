"""In-process publish/subscribe bus used to drive the real-time dashboard.

When data is ingested, validated or approved, a small JSON event is published
here. The dashboard holds an SSE connection to ``/api/v1/events/stream`` and
re-fetches whatever panels the event touched, which is what makes the dashboard
update "automatically when new state data is ingested".

A monotonically increasing ``data_version`` is also maintained so clients that
cannot use SSE (or that reconnect) can cheaply poll for "did anything change?".
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from app.core.logging_config import get_logger

logger = get_logger(__name__)

MAX_QUEUE = 100


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()
        self._version = 0
        self._recent: list[dict[str, Any]] = []

    @property
    def data_version(self) -> int:
        return self._version

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._recent[-limit:][::-1]

    async def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=MAX_QUEUE)
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Publish an event. Safe to call from sync code and from worker threads."""
        self._version += 1
        event = {
            "type": event_type,
            "version": self._version,
            "at": datetime.now(timezone.utc).isoformat(),
            "payload": payload or {},
        }
        self._recent.append(event)
        if len(self._recent) > 200:
            self._recent = self._recent[-200:]

        message = json.dumps(event, default=str)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (CLI scripts, tests): the version bump is enough.
            return
        loop.call_soon_threadsafe(self._fanout, message)

    def _fanout(self, message: str) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("dropping dashboard event for slow subscriber")


event_bus = EventBus()
