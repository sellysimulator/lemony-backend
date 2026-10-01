"""Moving a guest's finished games onto the signed-in account."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import update
from sqlalchemy.orm import Session

from ...db.session import get_db
from ...models.game import Game
from ...schemas.user import ClaimRequest, ClaimResponse
from ...services.stats_service import recompute_user_stats
from ...services.user_service import UserService
from ...sockets.handlers.connection import GUEST_ID_RE
from ..deps import get_current_firebase_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/games", tags=["games"])
_users = UserService()


@router.post("/claim", response_model=ClaimResponse)
def claim_guest_games(
    payload: ClaimRequest,
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> ClaimResponse:
    """Reattribute the finished games of a guest id the caller holds (from its localStorage)."""
    if not GUEST_ID_RE.match(payload.guest_identity):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid guest id."
        )
    try:
        user = _users.ensure_user(db, claims["uid"])
        result = db.execute(
            update(Game)
            .where(Game.guest_identity == payload.guest_identity, Game.user_id.is_(None))
            .values(user_id=user.id, guest_identity=None)
        )
        recompute_user_stats(db, user.id)
        db.commit()
    except Exception:
        logger.exception("Failed to claim guest games.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error."
        ) from None
    return ClaimResponse(claimed=int(result.rowcount or 0))  # type: ignore[attr-defined]
