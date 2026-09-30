"""Default game configuration and server-side validation for the config screen."""

from typing import Any

from fastapi import APIRouter

from ...core.defaults import (
    INGREDIENT_NAMES,
    MAX_NUM_DAYS,
    PERSON_TYPES,
    WEATHER_TYPES,
    default_config,
)
from ...schemas.user import ConfigDefaultsResponse, ConfigValidateResponse
from ...services.game_service import GameError, parse_config

router = APIRouter(prefix="/config", tags=["config"])


@router.get("/defaults", response_model=ConfigDefaultsResponse)
def get_defaults() -> ConfigDefaultsResponse:
    return ConfigDefaultsResponse(
        config=default_config(),
        weather_types=list(WEATHER_TYPES),
        person_types=list(PERSON_TYPES),
        ingredient_names=list(INGREDIENT_NAMES),
        max_num_days=MAX_NUM_DAYS,
    )


@router.post("/validate", response_model=ConfigValidateResponse)
def validate_config(config: dict[str, Any]) -> ConfigValidateResponse:
    """The same rules `create_game` applies, so the screen can say why before starting."""
    try:
        parse_config(config)
    except GameError as exc:
        return ConfigValidateResponse(valid=False, message=exc.message)
    return ConfigValidateResponse(valid=True)
