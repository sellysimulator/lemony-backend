"""Stored games by public id: the open results page, and moving a guest's finished games
onto the signed-in account."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import update
from sqlalchemy.orm import Session

from ...db.session import get_db
from ...models.game import Game
from ...schemas.user import ClaimRequest, ClaimResponse
from ...services.db_service import find_game_by_public_id, public_results
from ...services.stats_service import refresh_user_stats
from ...services.user_service import UserService
from ...sockets.handlers.connection import GUEST_ID_RE
from ..deps import get_current_firebase_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/games", tags=["games"])
_users = UserService()
_INTERNAL_ERROR_DETAIL = "Internal server error."
_NOT_FOUND_DETAIL = "Game not found."


@router.get("/id/{public_id}/results")
def get_game_results(public_id: str, db: Annotated[Session, Depends(get_db)]) -> dict[str, Any]:
    """A finished game's results, for anyone holding its public id: the link the end
    screen shares (`/results/:publicId`). No sign-in, no secret, no expiry.

    `db_service.public_results`: the totals and day records only, naming no player.
    An unknown or malformed id is a 404. Counted against the REST middleware's heavy
    limit (`abuse.is_heavy_path`), as it reads every row of the game.
    """
    try:
        game = find_game_by_public_id(db, public_id)
        if game is not None:
            return public_results(db, game)
    except Exception:
        logger.exception("Failed to load a game's results.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_INTERNAL_ERROR_DETAIL
        ) from None
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)


@router.post("/claim", response_model=ClaimResponse)
def claim_guest_games(
    payload: ClaimRequest,
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> ClaimResponse:
    """Reattribute the finished games of a guest id the caller holds (from its localStorage).

    The claim commits on its own; the stats are recomputed after it, in their own
    transaction, so a stats failure never undoes the claim (reading the stats repairs them).
    """
    if not GUEST_ID_RE.match(payload.guest_identity):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid guest id."
        )
    try:
        user = _users.ensure_user(db, claims["uid"])
        user_id = user.id
        result = db.execute(
            update(Game)
            .where(Game.guest_identity == payload.guest_identity, Game.user_id.is_(None))
            .values(user_id=user_id, guest_identity=None)
        )
        claimed = int(result.rowcount or 0)  # type: ignore[attr-defined]
        db.commit()
    except Exception:
        logger.exception("Failed to claim guest games.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_INTERNAL_ERROR_DETAIL
        ) from None
    if claimed:
        refresh_user_stats(user_id)
    return ClaimResponse(claimed=claimed)
