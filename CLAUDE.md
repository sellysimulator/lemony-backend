# Lemony_Backend — agent orientation

## What this app is

**Lemony** is a **single-player** lemonade-stand business sim. A player (signed in with
Google via Firebase, or a guest) configures a game, then plays it one **day** at a time:
each day they plan (buy ingredient packs, set a recipe and a price), the server simulates
the day (weather, customer arrivals by hour and person type, buy/refuse decisions, stock
perishing), and the player reviews the result. After `num_days` the game is finished and
persisted. The metric is **profit** (`final_cash - starting_cash`); there is no other score.

This repo is the **backend**: FastAPI + Socket.IO on Python 3.11, MySQL (SQLAlchemy +
Alembic) for finished games and user stats, and a switchable live-state store (MySQL
`live_games` table by default, Redis optional). The server computes every number; the
client sends only a plan.

### Where it sits

```
Novus/
├── game_stack.md            shared template + security invariants (canonical copy)
├── Beery/, Selly/           sibling games built from the same template
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
guest identity at the handshake, refresh-resumes-the-game, MySQL persistence of finished
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

Some docstrings cite `00-decisions.md D19`, `15 §3.7`, `12 §2`. Those are Beery-era plan
docs that do not exist here — dangling references, not missing files to go find.

## Architecture in one pass

- **`app/core/` engine modules are pure** (`engine`, `demand`, `people`, `weather`,
  `ingredients`, `defaults`, `config_model`, `money`): stdlib + pydantic only, no settings,
  services, DB or I/O. `core/firebase.py` and `core/checks/` are *not* pure (they read
  settings) — the "pure" rule applies to the engine modules, not the package.
- **`LemonadeGame`** (`core/engine.py`) is rebuilt from its JSON payload on every service
  call and serialised back. Phases: `planning → played → (ack) → planning … → finished`.
- **`services/game_service.py` is the only code that mutates a live game document.** It
  owns locking, ownership checks, `GameError(code, message)` refusals, and end-of-game
  persistence (`db_service.persist_finished_game`, once, idempotent on `public_id`).
- **`services/state_service.py` + `state_backend.py`**: keys `game:<id>`, `active:<identity>`,
  locks `lock:game:<id>` / `lock:active:<identity>`, TTL `GAME_TTL_SECONDS`.
- **Auto-discovery everywhere.** A new REST router (module exporting `router` in
  `app/api/v1/`), socket handler module (`app/sockets/handlers/`), startup check
  (`app/core/checks/`, `register_check`) or model module (`app/models/`) is picked up by
  dropping in the file. Don't hand-register in `main.py`.

## Commands

```bash
python3.11 -m venv venv && ./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
./venv/bin/uvicorn app.main:application --reload --port 8080   # `application`, NOT `app`
./venv/bin/pytest -q --cov          # coverage floor 75 (pyproject)
./venv/bin/ruff check app tests     # clean — keep it clean
./venv/bin/alembic upgrade head
./venv/bin/alembic revision --autogenerate -m "..."
```

`app.main:app` is REST-only; the Socket.IO ASGI wrapper is `application`. `mypy` and
`black` are installed but **not clean and not gated** — don't mass-reformat or "fix" mypy
as a side effect of an unrelated change; do it as its own change if asked.

## Contracts that span both repos

Change these together, backend first, and ship both halves in the same window:

| What | Backend | Frontend |
|---|---|---|
| Socket events & payloads | `app/sockets/handlers/game.py` (+ `connection.py` handshake) | `src/api/socketHandlers.ts`, `src/types/game.ts` |
| REST shapes | `app/schemas/user.py`, `app/api/v1/*` | `src/api/rest.ts` |
| Engine output shapes (`view()`, day record, `summary()`) | `app/core/engine.py` | `src/types/game.ts` (hand-written, no codegen) |
| Money rounding | `app/core/money.py` `round_cents` | `src/utils/money.ts` `roundCents` |
| Rounding fixture | `tests/fixtures/rounding_cases.json` | `src/__tests__/fixtures/rounding_cases.json` (byte-identical) |
| Pack price, purchase cost | `core/config_model.py` `pack_price`, `engine.purchase_cost` | `src/game/configMath.ts` `packPrice`, `src/game/costing.ts` |
| Demand/perish maths (help & previews only) | `core/demand.py`, `people.py`, `ingredients.py`, `weather.py` | `src/game/configMath.ts` |
| Vocabulary constants | `core/defaults.py` | `src/types/game.ts` |

**Rounding rule:** trim `value*100` to 12 significant digits, round half away from zero,
`/100`, normalise `-0`. `tests/test_core/test_money.py` fails if the two fixture copies
diverge (skips if the frontend isn't a sibling checkout).

## Invariants

- **Identity is server-only.** Resolved once at `connect` from `auth.idToken` (verified via
  the single path `api/deps.verify_firebase_id_token`) or `auth.guestId`
  (`^guest_<uuid>$`), else a fresh server-minted guest id. Read afterwards from
  `socket_manager.sid_to_identity[sid]`, never from a payload. A bad `idToken` refuses the
  connection; it never falls back to the guest id.
- **Never send** `identity`, the engine seed / raw engine payload, or `session_token`
  (except the single `game_created` emit to the creating sid). Clients get
  `engine.view()` / `summary()` only.
- **Ownership reads as absence.** Someone else's game returns "no such game", never a
  distinguishable error.
- **Determinism.** Seed from `SystemRandom` at create; per-operation RNG is
  `random.Random(f"{seed}:{day}:{purpose}")`. No global `random`. The order of RNG draws in
  `run_day` (arrivals → shuffle → one draw per visitor) and the Poisson method are part of
  replay behaviour — changing them changes every stored game's outcome.
- **Idempotency.** Every outbound event carries a per-game monotonic `seq` (the client drops
  stale ones). `submit_day` replays return the stored record with `duplicate=True`;
  `persist_if_finished` is safe to repeat and `resume_game` retries it.
- **Game formulas belong to the user.** Keep existing formulas; if one looks wrong, flag it
  as an explicit before/after proposal rather than rewriting it inside another change.

## Gotchas

- **SQL live-state backend = single instance.** With `REDIS_ENABLED=false` (the default),
  locks are process-local `asyncio.Lock`s. Run exactly one instance, no `--workers`. Scaling
  out requires `REDIS_ENABLED=true` + `REDIS_URL`. The startup check warns about this.
  (Unlike Beery, Redis-off here means the MySQL `live_games` table, which survives
  restarts, not an in-process dict.)
- **Stored-game compatibility.** Live docs carry `schema_version` (`SCHEMA_VERSION=1`); a
  mismatch is **deleted on read**, never migrated. `from_payload` re-validates config with
  `GameConfig`, so tightening validation can make in-flight games unloadable. Legacy paths
  (`pack_sizes`, `Batch.unit_cost=None`) exist for old docs — don't remove them without
  bumping the schema version.
- **`GameConfig` error messages are shown verbatim to players** — write them for players.
- **`state_service.redis_client`** is the backend object even when it is SQL; read it by
  name at call sites.
- **`app/db/base.py` must import `from ..models.base import Base`** — `.base` is a
  self-import.
- **Error handling.** Sockets: raise `GameError(code, message)` for refusals → `error
  {code, message}`; handlers are `@sio.event` + `@guarded()`, which logs handler + sid (never
  the payload) and emits a generic `SERVER_ERROR`. REST: `HTTPException` with a fixed
  string `detail`, `logger.exception` server-side, `raise ... from None`; never `str(e)`.
- **Startup never hard-fails on a missing credential.** No `FIREBASE_SERVICE_ACCOUNT_JSON`
  → CRITICAL log, guests still play, signed-in users are refused. That var must be one line,
  single-quoted or unquoted.
- **`CORS_ORIGINS` must never contain `"*"`** (validator raises; credentials are on).
- **Never hardcode Socket.IO `logger`/`engineio_logger` to `True`** — tied to `DEBUG`;
  engine.io logs full packets. Never log tokens, guest ids, uids or the service account.
- **Tests** use a temp SQLite DB via `DB_URL_OVERRIDE` set in `tests/conftest.py` before
  `app` is imported; tables are recreated per test; no Redis, `.env` never read.
  `asyncio_mode = strict` → every async test needs `@pytest.mark.asyncio`. A change to the
  Redis backend needs a manual smoke test against a real Redis.
- **Guest ids are bearer secrets.** Anyone holding one controls that guest's live game and
  can `POST /games/claim` its finished games. Known and accepted; don't widen it.
- Deps are pinned exactly (`==`); `anyio` arrives transitively.

## Deploy

`render.yaml`: one Docker web service `lemony-backend`, health check `/api/v1/health`
(`/health/deep` for DB/Redis readiness, always HTTP 200), `preDeployCommand: alembic upgrade
head`, `REDIS_ENABLED=false`, CORS `https://lemonysim.web.app` + localhost.

Known gaps — verify before relying on them:
- **No CI.** There is no `.github/workflows`; `render.yaml` comments mention a `ci.yml` that
  doesn't exist, so `autoDeployTrigger: checksPass` gates nothing. Run tests + ruff yourself.
- **No `.dockerignore`**, and the Dockerfile does `COPY . .` — `venv/` and `.env` would be
  copied into a locally built image.
