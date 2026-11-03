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

Tests run on SQLite and never read `.env`. CI (`.github/workflows/ci.yml`) runs exactly
these, then builds the Docker image; Render deploys only after it passes:

```bash
./venv/bin/ruff check app tests
./venv/bin/black --check app tests
./venv/bin/mypy app tests
./venv/bin/pytest -q --cov
```

## Layout

| Path                              | What it does                                                                                                                                                                                                                           |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `app/core/`                     | The game engine. It is pure: no I/O, and all randomness is seeded. Files:`defaults`, `config_model`, `limits`, `money`, `ingredients`, `people`, `weather`, `demand`, `engine`, `preview` (the morning plan's figures and what-if curve), `config_preview` (every figure of the config screen and its help), `plans` (the plan screen's first plan and the help's example plans). |
| `app/services/state_backend.py` | Holds live game state. With`REDIS_ENABLED=false` (the default) it uses the MySQL `live_games` table, so games survive a restart. Only one instance may run in this mode.                                                           |
| `app/services/game_service.py`  | Creates games, submits and acknowledges days, abandons games, and persists them. Every mutation runs under the game lock. A new game first retries the save of a finished, unsaved one, and is refused (`UNSAVED_GAME`) while that save fails. |
| `app/services/db_service.py`    | Writes a finished game once, as a single idempotent transaction. It also reads games back for the profile.                                                                                                                             |
| `app/sockets/handlers/`         | `connection` handles the identity handshake. `game` handles the play loop.                                                                                                                                                         |
| `app/api/v1/`                   | `health`, `config` (defaults with every limit; `preview`: the verdict on a draft config and every figure its screen and help show; `validate` and `cup-cost`, kept for older clients), `users` (profile, stats, games), `games/claim`. |

## Socket events

| Client → server                                               | Server → client                                                                                                                                                  |
| -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `create_game {config, replace?}`                             | `game_created {game_id, session_token}`, then `game_state`                                                                                                    |
| `resume_game {game_id?, session_token?}`                     | `game_state`, or `no_active_game`                                                                                                                             |
| `submit_day {game_id, day, purchases, price, recipe}`        | `day_result`. On the last day it is followed by `game_finished`, then `game_persisted` or, if the save failed, `game_persist_failed`.                    |
| `retry_persist {game_id}`                                    | `game_persisted {public_id}`, or `game_persist_failed`                                                                                                          |
| `ack_day {game_id, day}` / `request_state {game_id}`       | `game_state`                                                                                                                                                    |
| `abandon_game {game_id}`                                     | `game_abandoned`                                                                                                                                                |
| `preview_plan {request_id, game_id, day, purchases, recipe, price?}` | `plan_preview {request_id, day, prices, chance, expected_visitors, expected_cups, expected_margin, cups_possible, cost_per_cup, spend, cash_after, units, can_add, price?, price_error?, at_price?}`, or `{request_id, day, error}` |
| any refusal                                                    | `error {code, message}`                                                                                                                                         |

`plan_preview` is a request-scoped reply carrying every figure of the morning plan: the what-if curve (expected values across the price-input grid, for today's weather and popularity), the plan's `cost_per_cup` and `cups_possible`, what its packs cost (`spend`), the cash left (`cash_after`), the units they add (`units`), whether one more of each pack still passes the purchase rules and fits the cash (`can_add`), and, for the `price` sent, the server's verdict on it (`price_error`, null when fine) and the curve at exactly that price with the margin per cup (`at_price`). The client computes none of them. It is the one event without a `seq`; it changes no state, its refusals come inside the reply, and more than 5 requests per second per sid get no answer.

The game state also carries `popularity_factor` (what today's popularity multiplies every buy chance by), `packs` (every pack priced: `ingredients.pack_offers`) and `suggested_plan` (the last day's price and recipe, else `plans.opening_plan`). Every day record the client gets (`day_result.record`, a state's `days`, a summary's `days`) is an `engine.day_view`: the stored record plus `cash_start`, `popularity_factor`, `cups_open`, and per type and per hour `conversion` (and `refused` per hour); none of these is stored. Every event of a played day carries the running `sales`, the `stock` left and the `cups_left` it makes. The summary adds `conversion`, `total_perished_value`, `hour_totals` and `neutral_popularity`; profile games and stats carry `conversion`.

`POST /api/v1/config/preview` takes a draft config and answers `{valid, message, ingredients, help}` (always 200): the verdict `/config/validate` gives, each ingredient's packs and spoilage curves (`config_preview.ingredient_preview`, kept for an ingredient that is valid on its own while another part of the config is not), and, for a valid config, every figure the help shows (`config_preview.config_help`: weather odds, visitor and buy-chance curves, worked examples, cost-per-cup examples), each from the function the engine uses. `tests/fixtures/config_preview.json` is its reply for the defaults, copied byte-identical into the frontend's tests.

A finished game is never left behind unsaved. A failed save sends `game_persist_failed`; the client then offers a retry (`retry_persist`) instead of "Play again", and `resume_game` retries too. `create_game` retries the save of a finished, unsaved active game first and refuses with `UNSAVED_GAME` while it still fails.

Every game state carries `limits` (`app/core/limits.public_limits()`), the same object `GET /api/v1/config/defaults` sends: every bound the client shows or enforces, such as the packs of one size a day and the price step. The client copies none.

`submit_day` is idempotent. Re-sending a day that has already been played returns the stored result with `duplicate: true`.

`game_state` is `{seq, full, state}`. Only a full state (after `create_game`, `resume_game` and `request_state`) carries the history: `days`, `last_day_events` and, once finished, `summary`. The state after `ack_day` and the one inside `day_result` are daily and leave the history out, so the client appends `day_result.record` to the days it holds. The full contract is in the `app/sockets/handlers/game.py` docstring.

## Game rules

- **Spawn.** Each hour, the number of arrivals of each person type is `Poisson(base × (1 + mean(weather_match, hour_kernel, temp_kernel)) × weather_multiplier)`.
- **Buying.** The recipe sets what a customer is willing to pay, and the price is compared against it.
  - Each ingredient scores 1 at the person type's favourite amount and falls linearly to 0 at its own `tolerances.<ingredient>.below` units under it or `.above` units over it. The configured ranges only limit the recipe sliders; they do not change pickiness.
  - `quality = mean(ice, sugar, lemons scores)` and `wtp = budget × (1 + quality_swing × (2 × quality − 1))`, where `budget` is `average_expense`.
  - `P(buy) = 1 / (1 + exp((price − wtp) / spread))` with `spread = tolerances.price.above × budget / ln 19`: half of the type buys at `wtp`, 5 % at `wtp + above × budget`.
  - Under `budget × (1 − tolerances.price.below)` a cup looks suspicious and `P(buy)` is scaled by `price ÷ that floor`.
  - A refusal reads `not_popular` ("Not popular enough") when the visitor's one draw `u` (`buys = u < final P`) is under `P(buy)`: they would have bought at a neutral popularity of 0.5. It only happens while popularity is under 0.5, and it adds no random draw. Otherwise it reads `too_cheap` under that floor, `too_pricey` when the price sits further over the budget (in units of `above × budget`) than the worst ingredient sits under a perfect score, or else names that ingredient. Games stored earlier keep the reasons they were stored with.
- **Popularity.** A 0–1 value carried across the game, fixed for each day, applied on top of `P(buy)`: `final P = min(1, P(buy) × (0.5 + popularity))`, so 0.5 is neutral, 0 halves every chance and 1 raises it by half.
  - A day's success rate is `cups sold ÷ visitors` (the day's `conversion`); visitors who met a sold-out stand count as not sold.
  - `popularity = mean(success rate of every played day that had visitors)`, a plain average where every day counts the same; days with no visitors are skipped. Until the first such day it is the config's `starting_popularity` (default 0.5), which is then dropped: it is not part of the average.
  - So a day raises popularity exactly when its success rate is above the popularity it was played with (0.5 is not the line). The weather changes the mix of person types that come, and so the success rate of the same plan.
  - The summary's success rate (`conversion`: `total_buyers ÷ total_visitors`, pooled) and `final_popularity` (the average of daily rates) can differ when days have different numbers of visitors.
  - Each day record carries the `popularity` applied that day; the game state carries the current one, and the summary its `final_popularity`.
- **Packs.** Each ingredient is sold in packs, and each pack size has its own discount: `pack price = size × unit_cost × (1 − discount)`, rounded to the cent. A plan buys 0 to `MAX_PACKS_PER_SIZE` (100) packs of each size a day; anything else, a negative count included, refuses the whole plan. Every batch remembers the unit price paid, so `cost_per_cup` and `perished_value` reflect discounts.
- **Cost per cup.** One meaning everywhere, computed once (`engine.cup_cost`): the average cost of the cups you can make from your stock. Each ingredient is used in the order cups use it: held stock oldest batch first, at the price paid, then today's packs, one lot per pack size, dearest per unit first (the cheapest stock is kept longest). `plan_preview` sends it for the plan being edited, and the what-if curve's `expected_margin = expected_cups × (price − cost_per_cup)`. The day record's `cost_per_cup` is what the cups sold cost; on a day with no sales, what the cups you could have made would have cost, or null with nothing to make a cup from. Games stored earlier hold the list price on days with no sales.
- **Day result.** A day's `profit` is `revenue − spend` (today's purchases, even stock used on later days), so the days sum exactly to the final score, `final_cash − starting_cash`. Players see it as "Cash result".
- **Plans.** Pack counts, recipe amounts and the price are never negative: each must sit inside its range or the plan is refused with a message, never clamped.
- **Money rounding.** Every money value rounds to whole cents, halves away from zero, after trimming float noise (`app/core/money.py`). Only the server rounds: every money figure it sends is already rounded, and the client prints it with two decimals.
- **Perishing.** Each purchase batch has a fresh period followed by a linear ramp: `hazard(age) = 0` while `age ≤ fresh_days`, `1` once `age ≥ max_days`, and `(age − fresh)/(max − fresh)` in between. Units are lost binomially at the end of each day.
- **Weather.** Each day's temperature is drawn uniformly from the configured range, rounded to 0.5 °C and kept inside the range (`weather.day_temperature`). The weather is whichever type's temperature range contains it, picked at random among overlapping ones; if none does, the nearest range, the first in `sunny, cloudy, rainy, snowy` order on a tie (`weather.weathers_at`). `weather.weather_odds` gives the exact share of days of each weather from those same two steps (the help's odds). The overall range and every weather's range must stay within −30..60 °C (`app/core/limits.py`).
- **Profile stats.** "Best game" is the highest `total_profit` of any one game and "average profit" the mean over games; games of every length and setting are compared side by side (`services/stats_service.py`).
