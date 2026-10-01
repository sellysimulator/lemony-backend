"""User profile routes. No route takes a uid: the caller is always the verified token."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.session import get_db
from ...models.game import Game, UserStats
from ...schemas.user import (
    GameListItem,
    GameListResponse,
    UserResponse,
    UserStatsResponse,
    UserUpsertRequest,
)
from ...services.db_service import game_row_summary, load_game_detail
from ...services.user_service import UserService
from ..deps import get_current_firebase_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])

_user_service = UserService()
_INTERNAL_ERROR_DETAIL = "Internal server error."


def _or_claim(value: str | None, claims: dict, key: str) -> str | None:
    if value is not None:
        return value
    claim = claims.get(key)
    return claim if isinstance(claim, str) else None


def _internal_error(what: str) -> HTTPException:
    # The driver's own message names host, port and user: log it, never return it.
    logger.exception(what)
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_INTERNAL_ERROR_DETAIL
    )


@router.post("/upsert", response_model=UserResponse)
def upsert_user(
    payload: UserUpsertRequest,
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> UserResponse:
    try:
        user = _user_service.upsert_user(
            db,
            firebase_uid=claims["uid"],
            display_name=_or_claim(payload.display_name, claims, "name"),
            email=_or_claim(payload.email, claims, "email"),
            photo_url=_or_claim(payload.photo_url, claims, "picture"),
        )
    except Exception:  # noqa: BLE001 — logged, never returned
        raise _internal_error("Failed to upsert a user profile.") from None
    return UserResponse.model_validate(user)


@router.get("/me", response_model=UserResponse)
def get_me(
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> UserResponse:
    user = _user_service.get_by_firebase_uid(db, claims["uid"])
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")
    return UserResponse.model_validate(user)


@router.get("/me/stats", response_model=UserStatsResponse)
def get_my_stats(
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> UserStatsResponse:
    user = _user_service.get_by_firebase_uid(db, claims["uid"])
    stats = db.get(UserStats, user.id) if user else None
    if stats is None:
        return UserStatsResponse()
    best = db.get(Game, stats.best_game_id) if stats.best_game_id else None
    best_public = best.public_id if best else None
    return UserStatsResponse(
        games_played=stats.games_played,
        days_played=stats.days_played,
        total_cups_sold=stats.total_cups_sold,
        total_visitors=stats.total_visitors,
        total_profit=float(stats.total_profit),
        best_profit=float(stats.best_profit) if stats.best_profit is not None else None,
        avg_profit=float(stats.avg_profit) if stats.avg_profit is not None else None,
        best_game_id=best_public,
    )


@router.get("/me/games", response_model=GameListResponse)
def list_my_games(
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> GameListResponse:
    user = _user_service.get_by_firebase_uid(db, claims["uid"])
    if user is None:
        return GameListResponse(items=[], total=0, page=page, page_size=page_size)
    total = db.execute(select(func.count(Game.id)).where(Game.user_id == user.id)).scalar_one()
    games = db.execute(
        select(Game)
        .where(Game.user_id == user.id)
        .order_by(Game.finished_at.desc(), Game.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).scalars()
    return GameListResponse(
        items=[GameListItem(**game_row_summary(g)) for g in games],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/me/games/{public_id}")
def get_my_game(
    public_id: str,
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> dict[str, Any]:
    user = _user_service.get_by_firebase_uid(db, claims["uid"])
    game = db.execute(select(Game).where(Game.public_id == public_id)).scalar_one_or_none()
    if user is None or game is None or game.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Game not found.")
    return load_game_detail(db, game)
