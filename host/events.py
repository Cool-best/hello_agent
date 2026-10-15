import asyncio

QUEUE_MAX = 200

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue):
    _subscribers.discard(queue)


def publish(event: dict):
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
