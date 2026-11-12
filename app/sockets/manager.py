"""Socket.IO server setup and the per-process connection manager."""

import logging
from typing import Any

from socketio import AsyncRedisManager, AsyncServer  # type: ignore[import-untyped]

from ..config import settings
from ..core.abuse import (
    MAX_SOCKETS_PER_IP,
    SID_LIMITERS,
    UNKNOWN_IP,
    client_ip,
    connect_limiter,
)

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
    # The largest legitimate message, a full config, is about 3 KB.
    max_http_buffer_size=64 * 1024,
)


class SocketManager:
    """Tracks each connected sid's verified identity and sends events to sids."""

    def __init__(self) -> None:
        # Verified identity (Firebase uid or guest_<uuid4>), established once at
        # the handshake and never afterwards accepted from the client.
        self.sid_to_identity: dict[str, str] = {}
        # Each open sid's client address (`core.abuse.client_ip`), and how many
        # sockets each address holds open. Never logged.
        self.sid_to_ip: dict[str, str] = {}
        self.sockets_per_ip: dict[str, int] = {}

    async def connect(self, sid: str, environ: dict) -> bool:
        """Admit a socket; False when its address has too many open or connects too fast."""
        ip = client_ip(environ.get("asgi.scope") or {})
        if self.sockets_per_ip.get(ip, 0) >= MAX_SOCKETS_PER_IP or not connect_limiter.hit(ip):
            logger.warning("Refusing sid %s: too many connections from one address.", sid)
            return False
        self.sid_to_ip[sid] = ip
        self.sockets_per_ip[ip] = self.sockets_per_ip.get(ip, 0) + 1
        logger.info("Client connected: %s", sid)
        return True

    async def disconnect(self, sid: str) -> None:
        self.sid_to_identity.pop(sid, None)
        for limiter in SID_LIMITERS:
            limiter.forget(sid)
        ip = self.sid_to_ip.pop(sid, None)
        if ip is not None:
            left = self.sockets_per_ip.get(ip, 1) - 1
            if left > 0:
                self.sockets_per_ip[ip] = left
            else:
                self.sockets_per_ip.pop(ip, None)
        logger.info("Client disconnected: %s", sid)

    def ip_of(self, sid: str) -> str:
        return self.sid_to_ip.get(sid, UNKNOWN_IP)

    async def emit_to_sid(self, sid: str, event: str, data: Any) -> None:
        try:
            await sio.emit(event, data, room=sid)
        except Exception:
            logger.exception("Failed to emit %r to sid %s.", event, sid)


socket_manager = SocketManager()
