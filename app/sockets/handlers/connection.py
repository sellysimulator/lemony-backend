"""The Socket.IO handshake: identity, established exactly once.

`identity` is server-only — a verified Firebase uid, or a `guest_<uuid4>` — and
is never on the wire. Every later handler reads
`socket_manager.sid_to_identity[sid]` rather than anything the client says.
"""

import logging
import re
import uuid
from typing import Any

import anyio.to_thread
import socketio  # type: ignore[import-untyped]

from ...api.deps import verify_firebase_id_token
from ..manager import sio, socket_manager

logger = logging.getLogger(__name__)

# A Firebase uid can never satisfy this, so a guest can never claim one.
GUEST_ID_RE = re.compile(r"^guest_[0-9a-f-]{36}$")

# Shown by the client's `connect_error`. Deliberately free of auth words: being
# over the limit is no reason to sign in again.
TOO_MANY_CONNECTIONS_MESSAGE = "Too many connections from your network. Try again in a minute."


def _new_guest_identity() -> str:
    return f"guest_{uuid.uuid4()}"


def resolve_identity(auth: dict | None) -> str | None:
    """The identity for a handshake payload, or None to reject it.

    None means an ID token was presented and did not verify. That is never a
    reason to fall back to an accompanying guest id.
    """
    if not isinstance(auth, dict):
        return _new_guest_identity()
    id_token = auth.get("idToken")
    if id_token:
        claims = verify_firebase_id_token(id_token)
        if not claims:
            return None
        uid = claims.get("uid")
        if not uid or not isinstance(uid, str):
            return None
        return uid
    guest_id = auth.get("guestId")
    if isinstance(guest_id, str) and GUEST_ID_RE.match(guest_id):
        return guest_id
    return _new_guest_identity()


@sio.event
async def connect(sid: str, environ: dict, auth: Any = None) -> Any:
    # The per-address caps first: they are cheap, and token verification is not.
    if not await socket_manager.connect(sid, environ):
        raise socketio.exceptions.ConnectionRefusedError(TOO_MANY_CONNECTIONS_MESSAGE)
    # Token verification is synchronous (and may fetch Google's keys): off the event loop.
    identity = await anyio.to_thread.run_sync(resolve_identity, auth)
    if identity is None:
        logger.warning("Rejecting handshake for sid %s: the ID token failed.", sid)
        await socket_manager.disconnect(sid)  # a refused connect gets no disconnect event
        return False
    socket_manager.sid_to_identity[sid] = identity
    return None


@sio.event
async def disconnect(sid: str, *args: Any) -> None:
    # Single-player: the game simply waits in the state store for a resume.
    await socket_manager.disconnect(sid)
