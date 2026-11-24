"""One guard so that a handler's failure reaches the person who caused it.

Without it an exception inside a handler propagates into Socket.IO's own
logging and the client is told nothing: the button stays pressed and the
action silently did not happen. Every caught exception is still logged with
`logger.exception`, so nothing is swallowed.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..core.abuse import RATE_LIMITED_MESSAGE, RateLimiter, event_limiter
from .manager import socket_manager

__all__ = ["SERVER_ERROR_MESSAGE", "guarded"]

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]

#: Deliberately vague. The player can act on "try again"; they can do nothing
#: with a driver message, and a driver message must never go on the wire — a
#: connection string in an alert box is a credential leak.
SERVER_ERROR_MESSAGE = "Something went wrong on the server. Please try again."


def guarded(
    per_ip: RateLimiter | None = None,
    per_sid: RateLimiter | None = None,
) -> Callable[[Handler], Handler]:
    """Report an unhandled handler exception to the caller as `error {code, message}`,
    then continue.

    It is also the one place socket events are rate limited (`core/abuse.py`). Past
    `EVENT_LIMIT` events a second a sid's events are dropped unanswered: a real client
    sends one per click. With `per_sid`, the event also counts against that limiter per
    sid, and one over it is dropped unanswered too. With `per_ip`, it counts against
    that limiter per client address, and one over it is answered with `RATE_LIMITED`.

    Apply it *under* `@sio.event`, so Socket.IO registers the wrapper:

        @sio.event
        @guarded()
        async def submit_day(sid, data=None): ...
    """

    def decorate(fn: Handler) -> Handler:
        @functools.wraps(fn)
        async def wrapper(sid: str, *args: Any, **kwargs: Any) -> Any:
            if not event_limiter.hit(sid):
                return None
            if per_sid is not None and not per_sid.hit(sid):
                return None
            if per_ip is not None and not per_ip.hit(socket_manager.ip_of(sid)):
                payload = {"code": "RATE_LIMITED", "message": RATE_LIMITED_MESSAGE}
                await socket_manager.emit_to_sid(sid, "error", payload)
                return None
            try:
                return await fn(sid, *args, **kwargs)
            except Exception:
                # The event name, never the payload: it may carry a `session_token`.
                logger.exception("Handler %r failed for sid %s.", fn.__name__, sid)
                payload = {"code": "SERVER_ERROR", "message": SERVER_ERROR_MESSAGE}
                try:
                    await socket_manager.emit_to_sid(sid, "error", payload)
                except Exception:  # pragma: no cover - the socket is gone
                    logger.exception("Could not report the failure to sid %s.", sid)
                return None

        return wrapper

    return decorate
