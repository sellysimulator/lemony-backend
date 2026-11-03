"""Default game configuration, the limits, and server-side checks for the config screen."""

from typing import Any

from fastapi import APIRouter, HTTPException

from ...core.defaults import (
    INGREDIENT_NAMES,
    PERSON_TYPES,
    WEATHER_TYPES,
    default_config,
)
from ...core.engine import LemonadeGame, PlanError
from ...core.limits import public_limits
from ...schemas.user import (
    ConfigDefaultsResponse,
    ConfigValidateResponse,
    CupCostRequest,
    CupCostResponse,
)
from ...services.game_service import GameError, parse_config

router = APIRouter(prefix="/config", tags=["config"])


@router.get("/defaults", response_model=ConfigDefaultsResponse)
def get_defaults() -> ConfigDefaultsResponse:
    return ConfigDefaultsResponse(
        config=default_config(),
        weather_types=list(WEATHER_TYPES),
        person_types=list(PERSON_TYPES),
        ingredient_names=list(INGREDIENT_NAMES),
        limits=public_limits(),
    )


@router.post("/validate", response_model=ConfigValidateResponse)
def validate_config(config: dict[str, Any]) -> ConfigValidateResponse:
    """The same rules `create_game` applies, so the screen can say why before starting."""
    try:
        parse_config(config)
    except GameError as exc:
        return ConfigValidateResponse(valid=False, message=exc.message)
    return ConfigValidateResponse(valid=True)


@router.post("/cup-cost", response_model=CupCostResponse)
def cup_cost(request: CupCostRequest) -> CupCostResponse:
    """What these packs cost on day 1, and the cups they make with this recipe at what cost
    per cup: the engine's own figures, for the help's examples. Cash is not checked.

    Refusals are 422 with the same player-facing sentence the game gives.
    """
    try:
        config = parse_config(request.config)
    except GameError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from None
    game = LemonadeGame(config, seed=0)  # nothing is drawn: the seed plays no part
    try:
        game.validate_recipe(request.recipe)
        spend, _, lots = game.purchase_cost(request.purchases)
    except PlanError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    cups, cost = game.cup_cost(request.recipe, lots)
    return CupCostResponse(spend=spend, cups_possible=cups, cost_per_cup=cost)
