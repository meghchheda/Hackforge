# Reserve Juggler AI

A risk-aware treasury engine. Every hour it decides how much cash to keep for payouts and where the rest can
earn (overnight repo, bonds, equities, with an overdraft line as backup), explains each decision, routes large
moves through two-person approval and records everything in a tamper-evident log. It is decision support: it
never moves money by itself.

It ships with a demo year (`data/treasury_4tier_dataset.csv`, 8,760 hourly rows for 2026). You can upload your own
data or stream live hourly rows into it.

## Run it

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python backend\app.py
```

Open http://localhost:8000 for the landing page, or http://localhost:8000/app for the dashboard. The server
starts at once and trains the models in the background. That takes about 20 s the first time and a few seconds
after that, because trained models are cached in `data/cache/`.

**First visit:** with no accounts yet, the dashboard asks you to create the first admin. That admin adds
everyone else under **Trust → Users**.

For a demo with one account per role:

```bash
.venv\Scripts\python backend\manage.py seed-demo
```

This prints random passwords for `admin`, `operator`, `approver`, `analyst`, `viewer` and `feeder`, and saves them
to `data/demo_credentials.txt` (git-ignored). Delete that file once you've noted them.

## Accounts and roles

| Role | Can |
| --- | --- |
| admin | Everything: users, API tokens, policy, balances, datasets |
| operator | Run the desk (funding, sweeps, allocation) and request approvals |
| approver | Approve or reject other people's requests (never their own) |
| analyst | Run Digital Twin scenarios and reverse stress tests |
| viewer | Read-only |
| ingest | Machine role: push live rows, nothing else |

Security details:
- **Passwords** are stored as salted PBKDF2-SHA256 hashes (310,000 iterations).
- **Sessions** are HttpOnly, SameSite=Strict cookies. State-changing calls must also send `X-Requested-With: rj`.
- **API tokens** are bearer tokens, stored only as hashes.
- **Lockout:** five wrong passwords lock that account for five minutes for that client.
- **Rate limits** apply to sign-in, the assistant, uploads and ingest.
- **Audit log:** sign-ins, failed sign-ins, user changes, token changes, uploads, activations, live rows and decisions all go to the hash-chained audit log.

Command line: `python backend/manage.py create-user NAME --roles admin`, `set-password NAME`,
`create-token NAME --label "bank feed"`, `list-users`.

## Bring your own data

Go to **Data** (admin), drop a CSV and check the column mapping, then activate it.
- **Columns:** the app guesses which column is the date, deposits, withdrawals or net flow from the headers.
- **Granularity:** hourly totals or individual transactions both work; rows are added up into clock hours.
- **Gaps:** missing hours count as zero flow.
- **Market columns:** any you don't have (repo rate, bond yield, haircuts, overdraft APR…) use standard values and are flagged as *assumed* in the honesty report.
- **Checks:** at least 60 days, at most five years, no negative flows, and net flow must equal deposits − withdrawals when both are given.
- **Activation** retrains the models, which takes under a minute. You can set starting balances, which are suggested from the size of your flows, and scale the policy's dollar amounts to match.

## Live mode

Push new hourly rows as they happen, from a bank export job, a webhook or the bundled feeder:

```bash
curl -X POST http://localhost:8000/api/live/rows -H "Authorization: Bearer $RJ_TOKEN" -H "Content-Type: application/json" -d "{\"rows\":[{\"timestamp\":\"2026-11-16 10:00\",\"deposits_usd\":1200000,\"withdrawals_usd\":950000}]}"
```

What happens to the rows:
- **Storage:** each batch is appended to the active uploaded dataset. The demo file itself is read-only.
- **Validation:** rows must come after the newest stored hour, and flows must be non-negative. Market fields you leave out carry forward.
- **Updates:** the engine re-scores on its current models within seconds. It fully retrains after `RJ_RETRAIN_EVERY_HOURS` new hours (24 by default).
- **Dashboard:** it follows the newest hour by itself. Tonight's sweep and what-ifs that run past the newest hour use projected flows, labelled as such.
- **Stale feed:** if no rows arrive for longer than the policy's `stale_after_hours`, the circuit breaker switches to conservative mode.

Streaming demo, one data hour every 10 seconds, picking up from mid-November. This needs an admin token from
Trust → Users, because the feeder uploads the history first:

```bash
.venv\Scripts\python tools\live_feed.py --token %RJ_TOKEN% --csv data\treasury_4tier_dataset.csv --start "2026-11-16 10:00" --every 10 --upload-history
```

For a sped-up feed, start the server with `RJ_LIVE_SECONDS_PER_HOUR=10` so the stale-feed check uses the same clock.

## Configuration

Set these in the environment or in `.env` (see `.env.example`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `RJ_HOST` / `RJ_PORT` | 127.0.0.1 / 8000 | Where to listen (use 0.0.0.0 in a container) |
| `RJ_DATA_DIR` | `data/` | Database, uploaded datasets, model cache |
| `RJ_SECURE_COOKIES` | 0 | Set to 1 when served over HTTPS (also sends HSTS) |
| `RJ_TRUSTED_PROXIES` | — | Reverse-proxy IPs whose `X-Forwarded-For` is trusted |
| `RJ_PUBLIC_DEMO` | 1 | Landing page shows headline numbers and the assistant without sign-in; set 0 for real data |
| `RJ_DEMO_TOOLS` | 1 | Shows the audit tamper-test button; set 0 in production |
| `RJ_SESSION_HOURS` | 12 | Idle session lifetime |
| `RJ_RETRAIN_EVERY_HOURS` | 24 | Full refit after this many new live hours |
| `RJ_LIVE_SECONDS_PER_HOUR` | 3600 | Wall-clock seconds per data hour for the stale-feed check |
| `RJ_MAX_UPLOAD_MB` | 50 | Upload size limit |
| `RJ_LOG_LEVEL` | INFO | Request log: method, path, status, time, user, request id |
| `ENABLE_CLAUDE_COPILOT` | 0 | Optional Claude copilot; needs `ANTHROPIC_API_KEY` and account credits |

Health checks: `GET /healthz` (process up) and `GET /readyz` (engine loaded; 503 while building). API docs are at `/api/docs`.

## Deploy with Docker

```bash
docker compose up -d --build
```

The image runs as a non-root user and keeps all state in the `rj-data` volume. The compose file turns public demo
mode and the tamper-test tool off. Put it behind an HTTPS reverse proxy, then set `RJ_SECURE_COOKIES=1` and
`RJ_TRUSTED_PROXIES`.

## Tests

```bash
.venv\Scripts\python -m unittest discover -s tests -v
```

The 30 tests take about 90 seconds and use only the standard library:
- **Dataset checks:** normalising transaction exports, gaps, net-only files and rejections.
- **Accounts:** password hashing, sessions, tokens, lockout and last-admin protection.
- **Engine:** generic splits, the model cache, live projections, stale feeds and balances.
- **End to end:** a real server process covering roles, CSRF, maker-checker, upload and activation, live rows, the stale-feed breaker, the audit chain and sign-out.

## Screens

| Screen | What it does |
| --- | --- |
| Command Center | Cash vs reserve, runway, tonight's sweep, live what-if sliders, five-tier view |
| Forecast | 1/6/24 h forecasts with conformal bands, error vs baselines, coverage, drift test |
| Funding Ladder | Ranked borrow-vs-sell paths, break-even clock, approval request |
| Night-Lock | 17:00 sweep sizing, overdraft and breach probability, calibration history |
| Allocation | LP targets, binding constraints, reserve waterfall, reproducible decision |
| Stress Lab | Digital Twin shocks, reverse stress map, wrong-way risk |
| Backtest | Gain vs fixed reserve, gap hours, perfect-hindsight ceiling, price of safety, report |
| Replay | The whole period animated, with alerts and circuit-breaker shading |
| Trust | Data checks, honesty report, policy as code, approvals, audit log, users and tokens |
| Data | Datasets, upload and mapping, activation, live-feed status |

The **Ask assistant** drawer answers questions from the same engines, with no API key.

## Layout

- `backend/app.py` — FastAPI service: authentication, roles, routes, middleware.
- `backend/settings.py` — settings read from the environment.
- `backend/manage.py` — command line for accounts and tokens.
- `backend/engine/` — the engine modules:
  - `data`, `datasets` — loading, checks, uploads, registry, live append.
  - `forecast`, `scenarios`, `treasury`, `stress` — the models.
  - `state` — the per-dataset engine.
  - `manager` — background builds, live updates, stale-feed monitor.
  - `auth`, `audit` — accounts and the hash-chained log.
  - `assistant`, `copilot` — the chat helpers.
- `frontend/` — the single-page app plus landing page (plain JavaScript and Chart.js).
- `tools/live_feed.py` — streams a CSV into the live API.
- `tests/` — the test suite.
