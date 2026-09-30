# Lemony Backend

FastAPI + Socket.IO backend for Lemony, a single-player lemonade-stand simulation. Plumbing follows `../game_stack.md` and Beery: Firebase or guest identity at the Socket.IO handshake, per-game `seq`, a state backend you can switch to Redis, and MySQL (Alembic) for finished games.

## Run

```bash
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env            # fill DB_* (and FIREBASE_SERVICE_ACCOUNT_JSON for Google sign-in)
./venv/bin/alembic upgrade head
./venv/bin/uvicorn app.main:application --reload --port 8080
```

Tests run on SQLite and never read `.env`:

```bash
./venv/bin/pytest -q --cov
./venv/bin/ruff check app tests
```

## Layout

| Path | What it does |
|---|---|
| `app/core/` | The game engine. It is pure: no I/O, and all randomness is seeded. Files: `defaults`, `config_model`, `ingredients`, `people`, `weather`, `demand`, `engine`. |
| `app/services/state_backend.py` | Holds live game state. With `REDIS_ENABLED=false` (the default) it uses the MySQL `live_games` table, so games survive a restart. Only one instance may run in this mode. |
| `app/services/game_service.py` | Creates games, submits and acknowledges days, abandons games, and persists them. Every mutation runs under the game lock. |
| `app/services/db_service.py` | Writes a finished game once, as a single idempotent transaction. It also reads games back for the profile. |
| `app/sockets/handlers/` | `connection` handles the identity handshake. `game` handles the play loop. |
| `app/api/v1/` | `health`, `config` (defaults and validation), `users` (profile, stats, games), `games/claim`. |

## Socket events

| Client → server | Server → client |
|---|---|
| `create_game {config, replace?}` | `game_created {game_id, session_token}`, then `game_state` |
| `resume_game {game_id?, session_token?}` | `game_state`, or `no_active_game` |
| `submit_day {game_id, day, purchases, price, recipe}` | `day_result`. On the last day it is followed by `game_finished` and `game_persisted`. |
| `ack_day {game_id, day}` / `request_state {game_id}` | `game_state` |
| `abandon_game {game_id}` | `game_abandoned` |
| any refusal | `error {code, message}` |

`submit_day` is idempotent. Re-sending a day that has already been played returns the stored result with `duplicate: true`.

## Game rules

- **Spawn.** Each hour, the number of arrivals of each person type is `Poisson(base × (1 + mean(weather_match, hour_kernel, temp_kernel)) × weather_multiplier)`.
- **Buying.** The chance a customer buys is `mean(price, ice, sugar, lemons)` triangle-kernel scores. The price score is symmetric, so a cup that is too cheap is penalised as well.
- **Packs.** Each ingredient is sold in packs, and each pack size has its own discount: `pack price = size × unit_cost × (1 − discount)`, rounded to the cent. Every batch remembers the unit price paid, so the day's `cost_per_cup` (ingredients used ÷ cups sold) and `perished_value` reflect discounts.
- **Money rounding.** Every money value rounds to whole cents, halves away from zero, after trimming float noise (`app/core/money.py`). The frontend's `src/utils/money.ts` uses the same rule, and both test suites run the same cases in `tests/fixtures/rounding_cases.json` (copied in the frontend repo).
- **Perishing.** Each purchase batch has a fresh period followed by a linear ramp: `hazard(age) = 0` while `age ≤ fresh_days`, `1` once `age ≥ max_days`, and `(age − fresh)/(max − fresh)` in between. Units are lost binomially at the end of each day.
- **Weather.** Each day's temperature is drawn uniformly from the configured range. The weather is whichever type's temperature range contains it.
