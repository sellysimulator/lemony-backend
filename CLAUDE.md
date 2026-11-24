# Lemony_Backend — agent orientation

## What this app is

**Lemony** is a **single-player** lemonade-stand business sim. A player (signed in with
Google via Firebase, or a guest) configures a game, then plays it one **day** at a time:
each day they plan (buy ingredient packs, set a recipe and a price), the server simulates
the day (weather, customer arrivals by hour and person type, buy/refuse decisions, stock
perishing), and the player reviews the result. After `num_days` the game is finished and
persisted. The metric is **profit** (`final_cash - starting_cash`); there is no other score.

This repo is the **backend**: FastAPI + Socket.IO on Python 3.11, PostgreSQL (SQLAlchemy +
psycopg 3 + Alembic) for finished games and user stats, and a switchable live-state store
(PostgreSQL `live_games` table by default, Redis optional). The server computes every number; the
client sends only a plan.

### Where it sits

```
Novus/
├── game_stack.md            shared template + security invariants (canonical copy)
├── Beery/, Selly/, Pulky/   sibling games built from the same template
├── Supply/                  the game catalog (branded Selly)
└── Lemony/
    ├── game_stack.md        generated copy — never edit in place
    ├── Lemony_Backend/      ← this repo (github.com/sellysimulator/lemony-backend) → Render (Docker)
    └── Lemony_Frontend/     React/Vite (github.com/sellysimulator/lemony-frontend) → Firebase Hosting
```

`Lemony/` itself is not a git repo; each half is its own repo. The frontend talks to this
service over REST `/api/v1/*` (Bearer Firebase ID token) and Socket.IO at `/socket.io`. In
dev, Vite proxies both to `localhost:8080`, which is why this service runs on 8080.

### Deliberate divergences from the template

Lemony is single-player, so the template's multiplayer pieces do not exist: **no rooms,
no lobby, no host, no `host_secret`, no aliases, no broadcasts.** Every outbound event goes
to the caller's sid only. Everything else in the template is kept on purpose — Firebase/
guest identity at the handshake, refresh-resumes-the-game, PostgreSQL persistence of finished
games for stats. Do not propose dropping auth, the DB or Socket.IO; simplifying anything
else from the template needs the user's explicit OK.

## Read these instead of re-deriving

- **`README.md`** — run commands, layout, socket event summary, and the **"Game rules"**
  section. There is no separate design doc for Lemony; that README section is the de facto
  spec. Keep it in sync when rules change.
- **`app/sockets/handlers/game.py` module docstring** — the socket contract table. It is
  the source of truth for event names and payloads.
- **`app/core/defaults.py` docstring** — deliberate deviations from the original config
  (e.g. Adult `preferred_hour` 18→17, `packs` replacing `pack_sizes`).
- **`../game_stack.md`** — §0 security invariants, §4 what changes per game vs never
  changes, §5 deploy checklist. Wins over this repo on plumbing questions.
- **`../Lemony_Frontend/src/components/config/docs/entries.tsx`** — player-facing
  explanation of every config knob; useful for the "why" behind a rule.

## Architecture in one pass

- **`app/core/` engine modules are pure** (`engine`, `demand`, `people`, `weather`,
  `ingredients`, `defaults`, `config_model`, `money`, `limits`, `preview`, `config_preview`,
  `plans`): stdlib + pydantic only, no settings,
  services, DB or I/O. `core/firebase.py` and `core/checks/` (they read settings) and
  `core/abuse.py` (ASGI rate limiting) are *not* pure — the "pure" rule applies to the
  engine modules, not the package.
- **`LemonadeGame`** (`core/engine.py`) is rebuilt from its JSON payload on every service
  call and serialised back. Phases: `planning → played → (ack) → planning … → finished`.
- **`services/game_service.py` is the only code that mutates a live game document.** It
  owns locking, ownership checks, `GameError(code, message)` refusals, and end-of-game
  persistence (`db_service.persist_finished_game`, once, idempotent on `public_id`).
- **Persistence** is one transaction with one executemany per child table; a failure is
  logged and returns `None`, never raising into the game. Every field of the day record is
  stored, and `engine.summarize()` builds both the live summary and the stored one;
  `tests/test_services/test_persistence.py` checks that the two match.
- **Player stats never cost a save.** `user_stats` is a cache of the player's games.
  `persist_finished_game` commits the game first; only then does
  `stats_service.refresh_user_stats` recompute the stats, in a session of its own (also
  when the game was already stored: a retry, or the `IntegrityError` of another writer
  that stored it first). A stats failure is logged (user id and game public id) and the
  public id is still returned, so `game_persisted` is still sent. `POST /games/claim` also
  commits the claim before it recomputes. `recompute_user_stats` rebuilds every figure from
  all the player's games, and `GET /users/me/stats` (`current_user_stats`) recomputes before
  answering when the stored `games_played` is not the count of the player's games. Games
  are only ever added to a player (save, claim), so that count is the staleness check:
  no timestamps (`updated_at` moves only when a value changes) and no migration.
  Tests: `tests/test_services/test_stats.py`.
- **`app/core/limits.py` holds every hard limit**, apart from `config_model` so code that
  needs only a limit doesn't import the whole config. Every player-supplied number has an
  upper bound there; `tests/test_core/test_limits.py` sets each one far out of range
  (±1e12, ±inf, NaN) and expects a refusal, and checks each served bound is the one
  enforced. `public_limits()` is what the client reads (`GET /config/defaults` `limits`
  and every game state's `limits`): the frontend hand-copies no bound.
- **Customers a day are capped** (`MAX_CUSTOMERS_PER_DAY` = 5,000; served as
  `limits.customers_per_day`): every traffic limit is fine alone, but together they made
  one `run_day` ~83,000 customers. `demand.max_expected_customers(config)` is the one
  figure (a safe upper bound on the busiest day's expected arrivals, from `expected_spawn`
  over `weather.temperatures_by_weather`; 228 on the defaults). The last `GameConfig`
  cross-field rule refuses a config over it (never clamps); the check runs on
  `model_validate` only. `LemonadeGame.from_payload` reads stored configs with
  `GameConfig.validate_uncapped`, so games stored before the cap still load and play.
  `POST /config/preview` sends the figure as `customers_per_day`, also beside the cap's
  refusal. Tests: `tests/test_core/test_customers_cap.py`.
- **The server computes every figure the client shows; the client copies no formula.**
  Each figure comes from the function the engine itself uses (never a second
  implementation), and a new one goes in an existing payload or a server function next
  to the engine's:
  - Plan screen: `plan_preview` (spend, cash after, units, `can_add` per pack, cost and
    margin per cup, the price verdict, the curve) and the state's `packs`,
    `popularity_factor`, `suggested_plan`.
  - Records: `engine.day_view` (cash at the start, popularity factor, success rate per
    type and hour, refusals per hour, cups the opening stock made) is derived on the way
    out and never stored; `summarize` adds the game's success rate, spoilage value,
    traffic per hour and neutral popularity. Events carry the running sales and stock.
  - Config screen and help: `POST /config/preview` (`core/config_preview.py`).
  The frontend's `noClientFormulas` test fails if one of the removed copies comes back.
- **Cost per cup has one meaning, computed once:** `engine.cup_cost` — the average cost of
  the cups the stock can make (held stock oldest first at the price paid, then today's
  packs dearest per unit first). The preview sends it, the day record keeps it (what the
  cups sold cost; with no sales, what the cups you could have made would have cost, or
  null), and `POST /config/preview` gives the help its day-1 examples
  (`POST /config/cup-cost` stays for older clients). The client computes none.
- **`core/config_preview.py`** answers `POST /config/preview` (called, debounced, on every
  config edit): `ingredient_preview` (packs via `ingredients.pack_offers`, spoilage via
  `Ingredient.hazard`) needs only that ingredient's settings, so it is answered even
  while the rest is invalid; `config_help` (weather odds via `weather.weather_odds`,
  the demand curves, worked examples, cost-per-cup examples via `purchase_cost` /
  `cup_cost`, example plans from `plans.py`) needs the whole config. It picks its own
  x-axes (`grids`). The route adds `customers_per_day` (`demand.max_expected_customers`)
  whenever the config is valid apart from that cap, null otherwise.
  `tests/fixtures/config_preview.json` is its reply for the defaults.
- **History goes out only in full states.** `game_state` is `{seq, full, state}`: full on
  create, resume and `request_state`, daily (no `days` or `summary`) after `ack_day` and
  inside `day_result`. The client appends `day_result.record` to the days it holds.
  Details in the `game.py` docstring.
- **A day's customers are sent once and never stored.** `run_day` returns `events` (one
  per visitor) and `day_result` carries them, for the playback; the engine payload keeps
  no copy (no `last_day_events`), so a saved game no longer grows with the crowd (at the
  5,000 cap a day's list is ~1.2 MB). A duplicate `day_result` sends `events: null` ("not
  available"), and a full state never carries them: the client opens that day's report
  instead of replaying. `tests/test_services/test_daily_state.py` and `test_engine.py`
  check it.
- **`core/preview.py` `plan_preview(game, plan, price)`** feeds the morning plan: expected
  chance, visitors, cups and margin across the price-input grid, the plan's
  `cups_possible` and `cost_per_cup`, `spend`, `cash_after`, `units` and `can_add`, and
  for the player's `price` the verdict (`price_error`, from `validate_price`) and the
  curve at exactly that price (`at_price`, with the margin per cup). Built from the
  `demand.py` functions `run_day` uses, `engine.cup_cost`, `purchase_cost`, `affordable`
  and `cash_after`, with no RNG beyond today's weather. There is no best price. The
  `preview_plan` handler answers with `plan_preview` (no `seq`, no lock, no save, refusals
  inside the reply; a bad price is a `price_error`, not a refusal) and drops more than
  `PREVIEW_LIMIT` (5) requests per second per sid (`core/abuse.py` `preview_limiter`,
  through `@guarded(per_sid=...)`).
- **`services/state_service.py` + `state_backend.py`**: keys `game:<id>`, `active:<identity>`,
  locks `lock:game:<id>` / `lock:active:<identity>`, TTL `GAME_TTL_SECONDS` (a guest's: at most
  `GUEST_GAME_TTL_SECONDS`, 2 days). Expired `live_games` rows go when read, at startup, and
  in a sweep `create_game` triggers at most every `PURGE_EVERY_SECONDS` (10 min). Process locks
  are weakly held (gone once released), and waiting for one gives up after
  `LOCK_TIMEOUT_SECONDS` (10 s) with `TimeoutError`, which `guarded()` answers as
  `SERVER_ERROR` (with Redis the same `timeout` is the lock's expiry instead), and a client `game_id` that is not 32 lowercase hex
  is refused as `NO_GAME` (`game_service.checked_game_id`) before any lock is taken
  (`require_owned`, which `preview_plan` uses, checks it before the store is read).
  **Lock order is identity → game**: `create_game` takes the old active game's lock inside
  `identity_lock` to read it and delete it (so a `submit_day` holding it saves first and
  can't save a deleted game back); nothing takes the game lock and then the identity lock
  (`abandon_game` releases one before taking the other). Neither lock is reentrant, so
  `persist_if_finished` (which takes the game lock) runs after that block, still under
  `identity_lock`. `bump_seq` never saves a game that is gone (abandoned, replaced,
  expired): it returns None and the handler sends nothing more for it.
- **The open results page.** `GET /games/id/{public_id}/results` (`api/v1/games.py`) is a
  saved game's results for anyone holding its public id: no auth, no secret, no expiry. It
  reuses the owner's serialisation (`db_service.public_results` = `load_game_detail` minus
  `config` and `started_at`, which the results screen doesn't show) and names no player:
  no uid, email, display name, user id or guest identity (the end screen shows no name
  either). `find_game_by_public_id` answers None for a malformed id without a query, so
  unknown and malformed ids are the same 404 `Game not found.`; `GET /users/me/games/{id}`
  uses it too. It counts against the heavy REST limit (`abuse.HEAVY_PATH_PATTERNS`).
  `tests/test_services/test_results.py` sweeps the reply's keys and values.
- **Auto-discovery everywhere.** A new REST router (module exporting `router` in
  `app/api/v1/`), socket handler module (`app/sockets/handlers/`), startup check
  (`app/core/checks/`, `register_check`) or model module (`app/models/`) is picked up by
  dropping in the file. Don't hand-register in `main.py`.

## Commands

```bash
python3.11 -m venv venv && ./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
./venv/bin/uvicorn app.main:application --reload --port 8080   # `application`, NOT `app`
./venv/bin/ruff check app tests
./venv/bin/black --check app tests  # line length 100, matches ruff (pyproject)
./venv/bin/mypy app tests           # default strictness
./venv/bin/pytest -q -m "not dbschema" --cov   # SQLite; coverage floor 75 (pyproject)
./venv/bin/pytest -q -m dbschema    # PostgreSQL 18 via Testcontainers (needs Docker)
./venv/bin/alembic upgrade head
./venv/bin/alembic revision --autogenerate -m "..."
python3 ../../scripts/db/gen_ddl.py --backend .   # rewrite db/schema.postgres.sql
```

`app.main:app` is REST-only; the Socket.IO ASGI wrapper is `application`. The checks
above are exactly what CI runs (the four lint/type/unit checks plus `docker build` in one
job, `pytest -m dbschema` in a second); all must stay green.
`# type: ignore`s are targeted: `import-untyped` on `socketio`/`firebase_admin` (no
stubs), `attr-defined` on DML `rowcount` (SQLAlchemy types `execute()` as `Result`), and
`return-value` on the Redis client posing as `StateBackend`.

## Contracts that span both repos

Change these together, backend first, and ship both halves in the same window. The
exception: when the backend stops sending a field the live frontend reads (as when round
events dropped the history, or states dropped `last_day_events` and a duplicate
`day_result` got `events: null`), ship the frontend first, or an open tab on the old frontend
crashes until it is reloaded.

| What | Backend | Frontend |
|---|---|---|
| Socket events & payloads (incl. `retry_persist` → `game_persisted` / `game_persist_failed`, `day_result.events` null when not available) | `app/sockets/handlers/game.py` (+ `connection.py` handshake) | `src/api/socketHandlers.ts`, `src/types/game.ts`, `src/store/gameStore.ts` |
| Plan preview (`preview_plan` with `price` → `plan_preview`: curve, cost/margin per cup, spend, cash after, units, `can_add`, `price_error`, `at_price`) | `app/core/preview.py`, `app/sockets/handlers/game.py` | `src/api/socketHandlers.ts`, `src/types/game.ts`, `src/store/gameStore.ts`, `src/components/plan/` |
| Config preview (`POST /config/preview`: verdict, `customers_per_day`, `ingredients`, `help`) | `app/core/config_preview.py`, `plans.py`, `weather.weather_odds`, `ingredients.pack_offers`, `demand.max_expected_customers`, `app/api/v1/config.py` | `src/api/rest.ts`, `src/types/game.ts` (`ConfigPreview`), `pages/ConfigPage.tsx`, `components/config/PacksEditor.tsx`, `components/config/docs/` |
| Config preview fixture | `tests/fixtures/config_preview.json` (= its reply for the defaults) | `src/__tests__/fixtures/config_preview.json` (byte-identical) |
| Derived record and summary figures (`day_view`, `summarize`, event `sales`/`stock`/`cups_left`, state `packs`/`popularity_factor`/`suggested_plan`, profile `conversion`) | `app/core/engine.py`, `app/services/db_service.py`, `app/api/v1/users.py` | `src/types/game.ts`; shown only: `PlanDay`, `DayReport`, `GameOver`, `charts/endgame/`, `day/Hud`, `day/InventoryPanel`, `ProfilePage` |
| Limits (every bound, the price step, the cap on customers a day) | `core/limits.py` `public_limits()` → `/config/defaults` `limits` and game state `limits` | `src/types/game.ts` `Limits`; read by `ConfigPage`, `PacksEditor`, `PlanDay`, the help |
| Limits fixture | `tests/fixtures/limits.json` (= `public_limits()`) | `src/__tests__/fixtures/limits.json` (byte-identical) |
| Cost per cup | `engine.cup_cost` (preview, day record, config preview's `cup_cost`) | shown only: `PlanDay`, `DayReport`, the help |
| REST shapes | `app/schemas/user.py`, `app/api/v1/*` | `src/api/rest.ts` |
| Open results (`GET /games/id/{public_id}/results`: the owner's detail without `config`/`started_at`, 404 `Game not found.`) | `app/api/v1/games.py`, `db_service.public_results` | `src/api/rest.ts` `getGameResults`, `pages/ResultsPage.tsx` (`/results/:publicId`), `components/report/GameOver.tsx` |
| Timestamps (`created_at`, a stored game's `started_at` / `finished_at`): ISO-8601 UTC ending in `Z` | `app/schemas/types.py` (`utc_z`, `UtcDateTime`), `db_service.game_row_summary` / `load_game_detail` | `new Date(value)` as is (never append a zone): `pages/ProfilePage.tsx`, `pages/ProfileGamePage.tsx` |
| Engine output shapes (`view()`, day record, `summary()`) | `app/core/engine.py` | `src/types/game.ts` (hand-written, no codegen) |
| Vocabulary constants (incl. refusal reasons) | `core/defaults.py`, `core/demand.py` | `src/types/game.ts`, `src/utils/format.ts` `REASON_TEXT` |

**Rounding rule:** only the server rounds money (`core/money.py` `round_cents`: trim
`value*100` to 12 significant digits, round half away from zero, `/100`, normalise `-0`).
Every money figure it sends is already rounded; the client's `money()` only prints two
decimals. Every other quantity rounded to a step (a Poisson count, a temperature to 0.5 °C,
a help example's units) uses `money.half_up` (halves up); never Python's `round()` (halves
to even) for a game quantity. `round(x, n)` stays only to trim a ratio or float noise.

## Invariants

- **Identity is server-only.** Resolved once at `connect` from `auth.idToken` (verified via
  the single path `api/deps.verify_firebase_id_token`) or `auth.guestId`
  (`^guest_<uuid>$`; a malformed one gets a fresh server-minted guest id). Read afterwards
  from `socket_manager.sid_to_identity[sid]`, never from a payload. A bad `idToken` refuses
  the connection; it never falls back to the guest id. A handshake with neither (no
  `auth`, or `{idToken: null, guestId: null}`, what a signed-in client sends when it could
  not fetch its token) is refused too, never handed a fresh guest: the client retries.
- **Never send** `identity`, the engine seed / raw engine payload, or `session_token`
  (except the single `game_created` emit to the creating sid). Clients get
  `engine.view()` / `summary()` only.
- **Ownership reads as absence.** Someone else's game returns "no such game", never a
  distinguishable error.
- **Determinism.** Seed from `SystemRandom` at create; per-operation RNG is
  `random.Random(f"{seed}:{day}:{purpose}")`. No global `random`
  (`tests/test_core/test_no_global_random.py` scans `app/` for it). The order of RNG draws in
  `run_day` (arrivals → shuffle → one draw per visitor) and the Poisson method are part of
  replay behaviour — changing them changes every stored game's outcome. The refusal
  reason reuses the visitor's one draw (`not_popular` when it is under the neutral-popularity
  chance) and draws nothing; `test_engine.py::test_seeded_replays_are_unchanged` pins a
  recorded replay. Figures added for the client (event `sales`/`stock`/`cups_left`,
  `inventory_open`, the views) read the engine's state and draw nothing;
  `weather.roll_day_weather` is `day_temperature` + `weathers_at` with the same two draws.
- **Idempotency.** Every outbound event carries a per-game monotonic `seq` (the client drops
  stale ones), except `plan_preview`, a request-scoped reply matched by `request_id`. `submit_day` replays return the stored record with `duplicate=True` (and `events: None`);
  `persist_if_finished` is safe to repeat; a failure is reported as `game_persist_failed`
  and retried in the background (`game.py` `PERSIST_RETRY_DELAYS` = 2, 10, 30 s, one task
  per game in `_persist_retries`, no lock held while it sleeps or writes; it answers the sid
  that last asked, in `_persist_sids`). A game is in `_persisting` from before its first
  write until its last retry is done, and leaves it before the final answer (so a rejoin
  during that answer saves again, and a failing hand retry then gets background retries);
  `retry_persist` retries at once, and `resume_game` / `request_state` do only when no save
  is in flight. Tests run with no background
  retries (autouse `no_background_saves`); `test_save.py` gates them with `retry_gate`. `create_game` never abandons a finished,
  unsaved game: it retries the save and refuses with `UNSAVED_GAME` while it fails.
- **Game formulas belong to the user.** Keep existing formulas; if one looks wrong, flag it
  as an explicit before/after proposal rather than rewriting it inside another change.

## Gotchas

- **SQL live-state backend = single instance.** With `REDIS_ENABLED=false` (the default),
  locks are process-local `asyncio.Lock`s. Run exactly one instance, no `--workers`. Scaling
  out requires `REDIS_ENABLED=true` + `REDIS_URL`. The startup check warns about this.
  (Unlike Beery, Redis-off here means the PostgreSQL `live_games` table, which survives
  restarts, not an in-process dict.)
- **Stored-game compatibility.** Live docs carry `schema_version` (`SCHEMA_VERSION=2`); a
  mismatch is **deleted on read**, never migrated. `from_payload` re-validates config with
  `GameConfig.validate_uncapped` (every rule but the customers-a-day cap), so tightening
  any other rule can make in-flight games unloadable. Legacy paths
  (`pack_sizes`, `Batch.unit_cost=None`) exist for old docs — don't remove them without
  bumping the schema version. Day records may hold `cost_per_cup: null` (nullable column);
  older stored days keep the list price on no-sale days and the refusal reasons they had.
  `inventory_open` (nullable `inventory_open_*` columns) is absent on days played before
  it, and so are the
  events' `sales`/`stock`/`cups_left`: the client then hides the playback's stock panel.
  Documents saved before the per-customer list was dropped still carry
  `engine.last_day_events`: `from_payload` ignores it and the next save drops it.
- **Day views are never stored.** `summarize` returns `day_view`s, so persistence writes
  only the record's own fields (`_day_row`, `_TYPE_FIELDS`, `_HOUR_FIELDS`, all explicit)
  and `load_game_detail` derives the views again; `test_persistence` checks the stored
  summary equals the live one.
- **`GameConfig` error messages are shown verbatim to players** — write them for players.
- **`state_service.redis_client`** is the backend object even when it is SQL; read it by
  name at call sites.
- **`app/db/base.py` must import `from ..models.base import Base`** — `.base` is a
  self-import.
- **Error handling.** Sockets: raise `GameError(code, message)` for refusals → `error
  {code, message}` (except `preview_plan`, whose refusals go inside `plan_preview` as an
  `error: {code, message}` field, since the client's `error` listener toasts and clears the stored
  game on `NO_GAME`); handlers are `@sio.event` + `@guarded()`, which logs handler + sid (never
  the payload) and emits a generic `SERVER_ERROR`. REST: `HTTPException` with a fixed
  string `detail`, `logger.exception` server-side, `raise ... from None`; never `str(e)`.
- **Startup never hard-fails on a missing credential.** No `FIREBASE_SERVICE_ACCOUNT_JSON`
  → CRITICAL log, guests still play, signed-in users are refused. That var must be one line,
  single-quoted or unquoted.
- **`CORS_ORIGINS` must never contain `"*"`** (validator raises; credentials are on).
- **Never hardcode Socket.IO `logger`/`engineio_logger` to `True`** — tied to `DEBUG`;
  engine.io logs full packets. Never log tokens, guest ids, uids or the service account.
- **Tests** use a temp SQLite DB via `DB_URL_OVERRIDE` set in `tests/conftest.py` before
  `app` is imported; tables are recreated per test; no Redis. `.env` is never read: the
  same block sets `ENV_FILE` empty, and `Settings` (`app/config.py`) takes its dotenv file
  from `ENV_FILE` (default `.env`; empty reads none), so a developer's file can't leak in.
  `asyncio_mode = strict` → every async test needs `@pytest.mark.asyncio`. A change to the
  Redis backend needs a manual smoke test against a real Redis. Tests marked `dbschema`
  run on a Testcontainers `postgres:18` migrated to head (`tests/fixtures/postgres.py`):
  the schema checks in `tests/test_db/` (committed DDL, models = migrations, UTC sessions,
  every timestamp `timestamptz`), and every test taking the `db_sessions` fixture runs
  once on SQLite and once on PostgreSQL through the real service code (stored-game parity
  in `test_persistence.py`, `live_games` in `test_live_games.py`).
- **PostgreSQL rules.** Driver psycopg 3 (`postgresql+psycopg://`, credentials
  percent-encoded). `Settings.db_connect_args` sets `options=-c timezone=UTC` and
  `connect_timeout=10` (libpq: an unreachable server fails a new connection after 10 s; no
  `statement_timeout`) on every connection, and TLS: `sslmode=verify-full` against `DB_SSL_CA` (PEM text is written to
  a temp file for `sslrootcert`), else `sslmode=require` unless `DB_REQUIRE_SSL=false`;
  any non-PostgreSQL URL (the suite's SQLite) gets no args. Every timestamp column is
  `DateTime(timezone=True)` (`timestamptz`), written as aware UTC, and every API field or
  stored-game timestamp leaves as ISO-8601 UTC with a `Z` (`app/schemas/types.py`
  `utc_z` / `UtcDateTime`); SQLite hands them back naive, which the code reads as UTC.
  `Base.metadata` has a naming convention, so constraint names are deterministic. The
  schema is one Alembic revision, `0001_postgres_baseline`; new revisions go on top, and
  `db/schema.postgres.sql` (from `Novus/scripts/db/gen_ddl.py`) is regenerated with
  them — the `dbschema` job fails when it drifts.
- **Abuse limits live in `app/core/abuse.py`**, as plain constants (no env knobs), in
  process memory (one instance). REST: `AbuseLimitMiddleware`, added *before* CORS in
  `main.py` so a 429 keeps its CORS headers; a heavy route is in `HEAVY_PATHS`, or in
  `HEAVY_PATH_PATTERNS` (prefix, suffix) when its path holds an id (the open results route). Sockets: open sockets and connects per address
  in `SocketManager.connect` (checked before the token is verified); events per sid,
  `preview_plan` per sid (`@guarded(per_sid=preview_limiter)`, dropped unanswered) and
  `create_game` per address (`@guarded(per_ip=create_game_limiter)`), in `guarded()`. A new
  limit is a `RateLimiter` in `abuse.py` passed to `guarded`, never a handler's own counter;
  a sid-keyed one goes in `SID_LIMITERS` so a disconnect forgets it. The
  client address is the `cf-connecting-ip` header (Render fronts services with Cloudflare,
  which overwrites it), else the RIGHTMOST `X-Forwarded-For` entry (Render appends it):
  `request.client` is forgeable because the Dockerfile trusts every proxy
  (`FORWARDED_ALLOW_IPS=*`). Tests start from zero counts (autouse `fresh_limits`).
  Chunked bodies (no `Content-Length`) are counted toward the 64 KB REST cap, and the first
  request from each IP source logs `client ip source: …` (no IP) so the deployment's header
  can be checked in Render's logs.
- **Guest ids are bearer secrets.** Anyone holding one controls that guest's live game and
  can `POST /games/claim` its finished games. Known and accepted; don't widen it.
- Deps are pinned exactly (`==`); `anyio` arrives transitively.

## Deploy

`render.yaml`: one Docker web service `lemony-backend`, health check `/api/v1/health`
(`/health/deep` for DB/Redis readiness, always HTTP 200, answer reused for 15 s), `plan: free`,
`REDIS_ENABLED=false`, CORS `https://lemonysim.web.app` + localhost. Migrations run at
container startup: the Dockerfile CMD is `alembic upgrade head && exec uvicorn …`, because
free Render instances have no pre-deploy command. A failed migration exits non-zero, the new
instance never becomes healthy and Render keeps the previous deploy live. Run one instance
only (Alembic must not race itself), and never `create_all()` at startup. The database is
`lemonydb` in Lemony's own Neon project (Free plan, PostgreSQL 18, AWS US East 1 — the
Render service's region), reached as `neondb_owner` on port 5432: `DB_*` are set in Render
(`sync: false`), `DB_REQUIRE_SSL=true`. `DB_HOST` is Neon's **direct** endpoint
(`ep-….us-east-1.aws.neon.tech`, no `-pooler`): the pooled endpoint is PgBouncer in
transaction mode and rejects the `options=-c timezone=UTC` startup parameter. `DB_SSL_CA` is
the Let's Encrypt roots ISRG Root X1 + ISRG Root X2 in one PEM bundle (Neon's certificates
chain to them; the same public file for all four games, not a secret): on Render the PEM
text of both certificates, locally `<Novus>/novus-secrets/neon-ca.pem`. Check TLS with the
boot log line (`verify-full`), psql's `\conninfo` (`SSL Connection | true`) or
`Novus/scripts/db/verify_live_db.py`, never `pg_stat_ssl`: Neon's proxy terminates TLS, so
it shows `f` even on encrypted links.

Neon Free: compute fixed at 0.25 CU, suspended after 5 min idle (the first request after
that waits a cold start, under a second; `pool_pre_ping=True` replaces the dropped
connections), 100 CU-hours a month and 1 GB, `max_connections` 112, a short restore
window. Keep your own backups: `pg_dump -Fc --no-owner --no-privileges` against the direct
endpoint.

**Connection budget.** Lemony's whole budget is 50 connections, deploys included. Per
instance at most `DB_POOL_SIZE + DB_MAX_OVERFLOW` = 20 + 4 = 24 (`pool_timeout=10`: a
request that waits longer for a connection fails instead of hanging). During a deploy
Render runs the old and new instance side by side, the new one's startup `alembic upgrade
head` takes 1 and an operator `psql` session 1:

| State | Connections |
|---|---|
| Normal running | 24 |
| Deploy overlap (old + new instance) | 48 |
| + startup `alembic upgrade head` | 49 |
| + one operator `psql` | 50 |

A backend run locally against Neon is one more instance and takes the game over budget
during a deploy, so everyday development uses a local Docker PostgreSQL 18
(`DB_HOST=localhost`, `DB_PORT=5432`, `DB_USER=postgres`, empty `DB_SSL_CA`,
`DB_REQUIRE_SSL=false`). The pool variables are set in the Render dashboard only if they
change (they are not in `render.yaml`, so the defaults apply). With Redis off `live_games`
is the pool's busiest user: every store operation opens a session in a worker thread and
closes it before returning, so no connection is held across an `await` or a lock wait.

**Deploy order for the handoff-hardening release: backend before frontend.** The new
backend accepts the old frontend's signed-in and guest handshakes, while the new frontend
relies on the backend refusing a handshake with no identity (`{idToken: null, guestId:
null}`) so that it retries.

`autoDeployTrigger: checksPass` makes `.github/workflows/ci.yml` the deploy gate: a push
to `main` deploys only once CI passes. Don't add a deploy job or deploy hook to CI (it
would deploy twice). `.dockerignore` keeps `.env`, `venv/` and `tests/` out of the image
— keep it in step with anything new that must not ship.
