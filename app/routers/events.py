"""
SSE event stream — replaces Socket.IO for real-time communication.

Publishes: console_output, forum_message, status_update
"""

import asyncio
import json
import time
from queue import Queue, Empty

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse
from loguru import logger

from app.services.event_bus import subscribe as event_bus_subscribe

router = APIRouter(tags=["events"])

# Per-subscriber queue
_subscribers: list[Queue] = []
_subscribers_lock = asyncio.Lock()

# Initialize event bus forwarder
def _event_bus_forwarder(event_type: str, data: dict):
    """Forward event bus events to all SSE subscriber queues."""
    payload = json.dumps({"event": event_type, "data": data}, ensure_ascii=False)
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except Exception:
            pass

event_bus_subscribe(_event_bus_forwarder)


async def _event_generator(request: Request):
    """SSE event generator with keepalive."""
    queue: Queue = Queue()
    _subscribers.append(queue)
    logger.debug("SSE client connected")

    try:
        # Send initial connection event
        yield f"event: connected\ndata: {json.dumps({'status': 'connected'})}\n\n"

        last_event_time = time.time()
        while True:
            if await request.is_disconnected():
                break
            try:
                payload = queue.get(timeout=1)
                yield f"data: {payload}\n\n"
                last_event_time = time.time()
            except Empty:
                # Send keepalive every 15s
                if time.time() - last_event_time > 15:
                    yield f": keepalive\n\n"
                    last_event_time = time.time()
    finally:
        _subscribers.remove(queue)
        logger.debug("SSE client disconnected")


@router.get("/api/events/stream")
async def event_stream(request: Request):
    return StreamingResponse(
        _event_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
