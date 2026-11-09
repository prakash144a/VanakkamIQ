"""In-process pub/sub so the admin dashboard can show live calls and orders (via SSE)."""

import asyncio
import logging
from collections import defaultdict

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self, max_queue: int = 200) -> None:
        self._subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._max_queue = max_queue

    def subscribe(self, business_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers[business_id].add(queue)
        return queue

    def unsubscribe(self, business_id: int, queue: asyncio.Queue) -> None:
        self._subscribers[business_id].discard(queue)

    def publish(self, business_id: int, event: dict) -> None:
        for queue in list(self._subscribers.get(business_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("Dropping dashboard event for slow subscriber (business %s)", business_id)
