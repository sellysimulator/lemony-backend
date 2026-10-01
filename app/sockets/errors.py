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

from .manager import socket_manager

__all__ = ["SERVER_ERROR_MESSAGE", "guarded"]

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]

#: Deliberately vague. The player can act on "try again"; they can do nothing
#: with a driver message, and a driver message must never go on the wire — a
#: connection string in an alert box is a credential leak.
SERVER_ERROR_MESSAGE = "Something went wrong on the server. Please try again."


def guarded(event: str = "error") -> Callable[[Handler], Handler]:
    """Report an unhandled handler exception to the caller, then continue.

    `event` is `"error"` (payload `{code, message}`), which every Lemony
    handler uses. Any other event name gets `{message}` only — a template
    leftover for lobby-style `join_error` events; Lemony has no lobby.

    Apply it *under* `@sio.event`, so Socket.IO registers the wrapper:

        @sio.event
        @guarded()
        async def submit_day(sid, data=None): ...
    """

    def decorate(fn: Handler) -> Handler:
        @functools.wraps(fn)
        async def wrapper(sid: str, *args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(sid, *args, **kwargs)
            except Exception:
                # The event name, never the payload: it may carry a `session_token`.
                logger.exception("Handler %r failed for sid %s.", fn.__name__, sid)
                payload: dict[str, str] = {"message": SERVER_ERROR_MESSAGE}
                if event == "error":
                    payload["code"] = "SERVER_ERROR"
                try:
                    await socket_manager.emit_to_sid(sid, event, payload)
                except Exception:  # pragma: no cover - the socket is gone
                    logger.exception("Could not report the failure to sid %s.", sid)
                return None

        return wrapper

    return decorate
