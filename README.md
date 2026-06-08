# Gook Backend

FastAPI backend for Gook — an AI-powered kitchen assistant that tracks your pantry, suggests recipes, and orders groceries.

## Tech Stack

- **FastAPI** — API framework
- **Supabase (PostgreSQL)** — database
- **Firebase Admin SDK** — auth token verification
- **dbmate** — database migrations
- **LangGraph** — agent graph execution (Smart Grocery agent)
- **Anthropic Claude** — pantry analysis, cart building, pantry image scanning
- **psycopg2** — sync DB driver (all standard routes)
- **psycopg v3 + psycopg-pool** — async DB driver (agent nodes only, for parallel queries)
- **pgvector** — semantic ingredient search (future)

## Prerequisites

- Python 3.13 ([pyenv](https://github.com/pyenv/pyenv) recommended)
- [dbmate](https://github.com/amacneil/dbmate) — for running migrations

## Local Development Setup

### 1. Clone the repo

```bash
git clone <repo-url>
cd gook-backend
```

### 2. Create and activate a virtual environment

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | Supabase PostgreSQL connection string |
| `FIREBASE_PROJECT_ID` | Yes | Firebase project ID |
| `FIREBASE_SERVICE_ACCOUNT_KEY` | Dev only | Path to service account JSON (see below) |
| `ANTHROPIC_API_KEY` | Yes | Claude API key — used by the Smart Grocery agent and pantry scan |
| `INSTACART_SERVICE_URL` | No | URL of the Instacart TypeScript service (default: `http://localhost:3001`) |
| `INSTACART_SERVICE_KEY` | No | Internal service-to-service key for the Instacart service |

**Getting the Firebase service account key:**

1. Go to [Firebase Console](https://console.firebase.google.com) → your project → Project Settings → Service Accounts
2. Click "Generate new private key" and download the JSON file
3. Place it in the project root (it's gitignored) and set the path in `.env`

On Cloud Run, leave `FIREBASE_SERVICE_ACCOUNT_KEY` blank — GCP provides credentials automatically.

### 5. Run database migrations

```bash
make migrate
# dbmate up
```

This applies all migrations from `db/migrations/` and updates `db/schema.sql`.

### 6. Seed the database

Run in order — USDA data must exist before mock data resolves ingredient IDs.

```bash
make seed
# python scripts/seed/seed_usda.py
# python scripts/seed/seed_mock.py
```

### 7. Run the development server

```bash
make dev
# uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`.

## Connecting the Frontend

The backend binds to `0.0.0.0` so any device on the same WiFi can reach it.

In development, the frontend auto-detects the backend IP from Expo's dev server — no manual configuration needed for physical devices. Just leave `EXPO_PUBLIC_API_URL` blank in `gook-frontend/.env`.

Set `EXPO_PUBLIC_API_URL` explicitly only when:

| Case | Value |
|---|---|
| Android Emulator | `http://10.0.2.2:8000` |
| Production | `https://your-deployed-api.com` |

## API Docs

Once running, interactive docs are available at:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Health Check

```bash
curl http://localhost:8000/health
```

---

## Team Workflow

### First-time setup (run once after cloning)

```bash
bash scripts/setup-hooks.sh
```

This installs the `pre-push` git hook from `git-hooks/`. **Every teammate must run this** — hooks aren't shared automatically by git.

### Worklog requirement

Every push requires a worklog entry. The pre-push hook enforces this — it will block your push and show instructions if you forget.

**How to write one (recommended):** Run `/junbi-worklog` in Claude Code. It reads your git diff and writes the file for you. All entries live in `worklogs/` — one file per push, named by date, time, and author.

### Updating CLAUDE.md (optional, intentional)

`CLAUDE.md` is the architectural reference — it documents conventions, library constraints, and gotchas that every developer and Claude session needs to know. It is **not** updated automatically on every push.

Run `/junbi-update-claude` in Claude Code when you've made a decision worth documenting:
- Added a library with non-obvious usage constraints
- Discovered an API quirk and wrote a workaround
- Established a new pattern or convention
- Changed something that would trip up a teammate or future Claude session

Don't run it just because you shipped a feature — that's what `worklogs/` is for.

**How to write one manually:**

1. Create a new file in `worklogs/` named:
   ```
   worklogs/YYYY-MM-DD_HHMM_<your-github-username>_<short-topic>.md
   ```
   Example: `worklogs/2026-06-07_1430_Jaden24_kroger-client-rewrite.md`

2. Fill it in:
   ```markdown
   # YYYY-MM-DD HH:MM — <your-name> — <topic>

   **Branch:** <branch-name>
   **Repo:** gook-backend

   ---

   ## What was done
   - ...

   ## Decisions made
   - ...

   ## Bottlenecks hit
   - ...

   ## Still mocked / pending
   - ...

   ## Next up
   - ...
   ```

3. Commit it alongside your code:
   ```bash
   git add worklogs/<filename>.md
   git commit -m "chore: worklog for <topic>"
   git push   # hook passes ✓
   ```

### Emergency bypass (hotfixes only)

```bash
SKIP_WORKLOG_CHECK=1 git push
```

Use sparingly — it's visible in git history that the check was skipped.

## Project Structure

```
gook-backend/
├── app/
│   ├── agents/
│   │   └── smart_grocery/         # Smart Grocery LangGraph agent
│   │       ├── graph.py           # StateGraph definition and compilation
│   │       ├── nodes.py           # All node functions (load_context, resolve_stores, etc.)
│   │       ├── state.py           # SmartGroceryState TypedDict
│   │       └── tools/
│   │           ├── instacart.py   # HTTP bridge to the Instacart TypeScript service
│   │           └── webview.py
│   ├── api/
│   │   └── routes/                # Route handlers
│   │       ├── users.py
│   │       ├── recipes.py
│   │       ├── pantry.py          # includes POST /pantry/scan (vision)
│   │       ├── grocery.py
│   │       ├── collections.py
│   │       ├── orders.py
│   │       ├── stores.py
│   │       └── agents.py          # Smart Grocery agent endpoints
│   ├── core/              # Auth dependencies (Firebase token verification)
│   ├── data/              # Legacy in-memory mock data (to be removed)
│   ├── db.py              # Connection pools: sync (psycopg2) + async (psycopg v3)
│   ├── schemas/           # Pydantic request/response schemas
│   ├── services/          # Business logic
│   └── main.py            # App entry point + lifespan (async pool init)
├── db/
│   ├── migrations/        # dbmate migration files
│   └── schema.sql         # Auto-updated by dbmate — current DB state
├── scripts/
│   ├── seed/
│   │   ├── sr_legacy_food_csv/   # USDA SR Legacy CSV files
│   │   ├── seed_usda.py          # Seeds ingredient_categories + ingredients
│   │   └── seed_mock.py          # Seeds demo user data for development
│   └── visualize_graph.py        # Renders the Smart Grocery graph as PNG + HTML
├── config.py              # Settings (loaded from .env)
├── draft_schema.sql       # Source of truth schema (mirrors db/migrations/)
└── requirements.txt
```

## Smart Grocery Agent

The Smart Grocery agent is a [LangGraph](https://langchain-ai.github.io/langgraph/) `StateGraph` that automates grocery ordering end-to-end.

### Graph flow

```
load_context → resolve_stores → analyze_pantry → search_store → compare_prices
    → build_cart → [INTERRUPT: user reviews cart] → place_order → finalize
```

| Node | What it does |
|---|---|
| `load_context` | Fetches pantry, saved recipes, grocery list, preferences, and user zip code from DB **in parallel** (5 async queries via psycopg v3) |
| `resolve_stores` | Calls Instacart for nearby stores, scores each one by price/quality fit against the user's preferences and budget, sets the recommended store |
| `analyze_pantry` | Claude identifies what to buy: recipe gaps, expiring items, and missing staples |
| `search_store` | Searches the recommended store's catalog via the Instacart TypeScript service |
| `compare_prices` | Estimates cart total across all major retailers (Walmart, Costco, Kroger, Publix, Whole Foods, Aldi) |
| `build_cart` | Claude picks the best product match per item with budget awareness |
| `human_checkpoint` | **LangGraph interrupt** — graph pauses, frontend shows cart + store picker with insights |
| `place_order` | Executes checkout via Instacart, returns a `checkout_url` opened in a WebView |
| `finalize` | Writes confirmed cart items to the user's grocery list in DB |

### Store recommendation logic

`resolve_stores` scores each nearby store based on the user's profile:

- **Tight budget** (`weeklyBudget ≤ $150`) → favors Walmart, Aldi, Costco
- **Quality dietary tags** (organic, vegan, keto, etc.) → favors Whole Foods, Sprouts, Trader Joe's
- **Large household** (4+) → bulk bonus for Costco
- **No strong signal** → rewards balanced mid-tier stores (Kroger, Target, HEB)

At `human_checkpoint`, the frontend receives the full ranked `nearby_stores` list with `insight` and `insight_type` fields so the user can override the recommendation before confirming.

### Async DB architecture

Agent nodes use **psycopg v3** (`AsyncConnectionPool`) instead of the psycopg2 sync pool used by all other routes. This allows `load_context` to run its 5 DB queries in parallel via `asyncio.gather()`, and avoids blocking the event loop in `finalize`.

The psycopg2 pool remains in place for all standard API routes — nothing changed there.

### Visualizing the graph

```bash
python scripts/visualize_graph.py
# → scripts/smart_grocery_graph.png
# → scripts/smart_grocery_graph.html  (opens automatically in browser)
```

### Running the Instacart TypeScript service

The agent's `search_store` and `place_order` nodes proxy to a TypeScript microservice. When it's not running, all calls fall back to deterministic stub data so the agent still executes end-to-end.

```bash
cd services/instacart
npm install && npm run dev   # runs on :3001 by default
```

---

## Pantry Scan (Vision)

`POST /pantry/scan` accepts a photo of a fridge or pantry shelf and uses Claude's vision model to detect ingredients.

**Request:** multipart form upload, field name `image` (JPEG / PNG / WebP, max 5 MB)

**Response:**
```json
{
  "items": [
    {
      "name": "spinach",
      "quantity": 1,
      "unit": "bag",
      "category": "produce",
      "expiryDate": "2026-04-19",
      "addedVia": "scan",
      "freshnessNote": "Slightly wilted — use soon",
      "confidence": "high"
    }
  ],
  "model": "claude-sonnet-4-6",
  "count": 4
}
```

The endpoint **does not save to DB** — it returns detected items for the user to review. To save confirmed items call `POST /pantry/bulk` with `addedVia: "scan"`.

If `ANTHROPIC_API_KEY` is not set, the endpoint returns stub data so the feature is testable in dev without credentials.

---

## Database

Schema is managed via dbmate migrations in `db/migrations/`. The `db/schema.sql` file is auto-generated by dbmate after each `dbmate up` — do not edit it manually.

To roll back the last migration:
```bash
dbmate down
```

To check migration status:
```bash
dbmate status
```

## Authentication

All endpoints (except `/health`) require a Firebase ID token as a Bearer token:

```
Authorization: Bearer <firebase-id-token>
```

The backend verifies the token using Firebase Admin SDK and extracts `uid` + `email` to identify the user.
