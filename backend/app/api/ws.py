"""WebSocket Action Drawer: streams bid blocker updates from Redis pub/sub to the workspace."""
from __future__ import annotations

import asyncio
import json
import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import get_settings
from ..core.events import bid_channel, tenant_channel
from ..db import tenant_session
from ..models import BidPackage
from ..security import decode_token

router = APIRouter()


@router.websocket("/ws/bids/{bid_id}")
async def bid_stream(ws: WebSocket, bid_id: uuid.UUID) -> None:
    # Browsers cannot set Authorization headers on WebSockets: the token arrives as the first message.
    await ws.accept()
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        principal = decode_token(first.get("token", ""))
    except Exception:  # noqa: BLE001
        await ws.close(code=4401)
        return
    with tenant_session(principal.tenant_id) as s:
        bid = s.get(BidPackage, bid_id)
        if bid is None:  # RLS: another tenant's bid is invisible
            await ws.close(code=4404)
            return
        await ws.send_json({"type": "bid.blockers", "payload": {
            "bid_id": str(bid.id), "readiness_score_pct": float(bid.readiness_score_pct or 0), "blocking_issues": bid.blocking_issues,
            "briefing_gate_status": bid.briefing_gate_status.value, "status": bid.status}})

    r = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe(bid_channel(principal.tenant_id, bid_id), tenant_channel(principal.tenant_id))

    async def pump() -> None:
        async for msg in pubsub.listen():
            if msg.get("type") == "message":
                await ws.send_text(msg["data"])

    task = asyncio.create_task(pump())
    try:
        while True:
            await ws.receive_text()  # keep-alive pings from the client
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        await pubsub.unsubscribe()
        await r.aclose()
