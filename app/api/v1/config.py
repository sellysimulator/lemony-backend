"""Default game configuration, the limits, and server-side checks for the config screen.

`POST /config/preview` is what the config screen calls (debounced) on every edit: the
verdict `create_game` would give plus every figure the screen and its help show for the
draft (`core/config_preview.py`, and the busiest day's customers,
`demand.max_expected_customers`).
"""

from typing import Any

from fastapi import APIRouter
from pydantic import ValidationError

from ...core.config_model import GameConfig, IngredientConfig
from ...core.config_preview import config_help, ingredient_preview
from ...core.defaults import (
    INGREDIENT_NAMES,
    PERSON_TYPES,
    WEATHER_TYPES,
    default_config,
)
from ...core.demand import max_expected_customers
from ...core.limits import public_limits
from ...schemas.user import ConfigDefaultsResponse, ConfigPreviewResponse
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


def _customers_if_capped(config: dict[str, Any]) -> int | None:
    """The busiest day's customers of a refused config that is valid apart from the cap
    on them (so the screen can show how far over it is); None when anything else is
    wrong.
    """
    try:
        parsed = GameConfig.validate_uncapped(config)
    except ValidationError:
        return None
    return max_expected_customers(parsed)


def _ingredients_alone(config: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Each ingredient's figures from its own settings, for a config that fails as a
    whole."""
    raw = config.get("ingredients")
    out: dict[str, dict[str, Any] | None] = {}
    for name in INGREDIENT_NAMES:
        try:
            cfg = IngredientConfig.model_validate(
                raw.get(name) if isinstance(raw, dict) else None
            )
        except ValidationError:
            out[name] = None
        else:
            out[name] = ingredient_preview(name, cfg)
    return out


@router.post("/preview", response_model=ConfigPreviewResponse)
def preview_config(config: dict[str, Any]) -> ConfigPreviewResponse:
    """The verdict on a draft config and every figure the config screen and help show.

    Always 200: an invalid draft answers `valid: false` with the player-facing reason,
    the figures of each ingredient that is valid on its own, and no `help`;
    `customers_per_day` only when the cap on it is what refused the draft.
    """
    try:
        parsed = parse_config(config)
    except GameError as exc:
        return ConfigPreviewResponse(
            valid=False,
            message=exc.message,
            customers_per_day=_customers_if_capped(config),
            ingredients=_ingredients_alone(config),
        )
    return ConfigPreviewResponse(
        valid=True,
        customers_per_day=max_expected_customers(parsed),
        ingredients={
            name: ingredient_preview(name, parsed.ingredients[name])
            for name in INGREDIENT_NAMES
        },
        help=config_help(parsed),
    )
