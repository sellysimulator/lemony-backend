"""Socket.IO server setup and the per-process connection manager."""

import logging
from typing import Any

from socketio import AsyncRedisManager, AsyncServer

from ..config import settings

logger = logging.getLogger(__name__)

sio = AsyncServer(
    async_mode="asgi",
    # `None` selects the in-process manager. Cross-instance fan-out needs Redis,
    # so this moves together with `REDIS_ENABLED`.
    client_manager=(AsyncRedisManager(settings.REDIS_URL) if settings.REDIS_ENABLED else None),
    cors_allowed_origins=settings.CORS_ORIGINS,
    # Never hardcoded to True: engine.io logs every packet's full payload at INFO.
    logger=settings.DEBUG,
    engineio_logger=settings.DEBUG,
    ping_timeout=120,
    ping_interval=25,
    max_http_buffer_size=5 * 1024 * 1024,
)


class SocketManager:
    """Tracks each connected sid's verified identity and sends events to sids."""

    def __init__(self) -> None:
        # Verified identity (Firebase uid or guest_<uuid4>), established once at
        # the handshake and never afterwards accepted from the client.
        self.sid_to_identity: dict[str, str] = {}

    async def connect(self, sid: str, environ: dict) -> None:
        logger.info("Client connected: %s", sid)

    async def disconnect(self, sid: str) -> None:
        self.sid_to_identity.pop(sid, None)
        logger.info("Client disconnected: %s", sid)

    async def emit_to_sid(self, sid: str, event: str, data: Any) -> None:
        try:
            await sio.emit(event, data, room=sid)
        except Exception:
            logger.exception("Failed to emit %r to sid %s.", event, sid)


socket_manager = SocketManager()
