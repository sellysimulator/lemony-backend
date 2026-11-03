"""Default game configuration, the limits, and server-side checks for the config screen.

`POST /config/preview` is what the config screen calls (debounced) on every edit: the
verdict of `/config/validate` plus every figure the screen and its help show for the
draft (`core/config_preview.py`). `/config/validate` and `/config/cup-cost` stay for
clients from before it.
"""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from ...core.config_model import IngredientConfig
from ...core.config_preview import config_help, ingredient_preview
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
    ConfigPreviewResponse,
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


def _ingredients_alone(config: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Each ingredient's figures from its own settings, for a config that fails as a whole."""
    raw = config.get("ingredients")
    out: dict[str, dict[str, Any] | None] = {}
    for name in INGREDIENT_NAMES:
        try:
            cfg = IngredientConfig.model_validate(raw.get(name) if isinstance(raw, dict) else None)
        except ValidationError:
            out[name] = None
        else:
            out[name] = ingredient_preview(name, cfg)
    return out


@router.post("/preview", response_model=ConfigPreviewResponse)
def preview_config(config: dict[str, Any]) -> ConfigPreviewResponse:
    """The verdict on a draft config and every figure the config screen and help show.

    Always 200: an invalid draft answers `valid: false` with the player-facing reason,
    the figures of each ingredient that is valid on its own, and no `help`.
    """
    try:
        parsed = parse_config(config)
    except GameError as exc:
        return ConfigPreviewResponse(
            valid=False, message=exc.message, ingredients=_ingredients_alone(config)
        )
    return ConfigPreviewResponse(
        valid=True,
        ingredients={
            name: ingredient_preview(name, parsed.ingredients[name]) for name in INGREDIENT_NAMES
        },
        help=config_help(parsed),
    )


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
