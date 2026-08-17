# sales-agent — project context

This file exists so any agent (or human) working in this repo, or in the
sibling `sales-agent-dashboard` repo, has an accurate picture of what this
service is, what state it's in, and what not to touch. Keep it current —
update it in the same commit as any change that makes a claim below stale.

## What this is

The AI sales agent for **Divine Empire India Pvt. Ltd.** (industrial/
construction machinery). FastAPI backend on Render, Supabase for canonical
data, Qdrant for product-knowledge RAG, GPT-4o with a Groq fallback.

Live channel today: **Telegram**. WhatsApp Business Cloud API credentials
have been provided by the client but **the adapter is not yet implemented —
do not build or enable it without explicit instruction.** See "WhatsApp
status" below; this is the most important boundary in this file.

Repo: `Divine-Empire/sales-agent` (primary remote `divine`) and
`teamai-botivate/sales-agent` (`origin`) — push to both.
Deployed: `https://sales-agent-956w.onrender.com`.

## Architecture, in one paragraph

`app/main.py` is the FastAPI app: Telegram webhook, admin routes, and the
`/api/*` dashboard API (auth'd via `X-API-Key` == `DASHBOARD_API_KEY`).
`app/agent.py` is the core loop: load history → RAG retrieve → LLM with tools
→ reply. `app/llm.py` is the only module that calls OpenAI/Groq. `app/rag.py`
does Qdrant search/ingestion. `app/store.py` is the only module that talks to
Supabase. `app/intelligence.py` does post-reply lead scoring/intent/summary.
`app/channels.py` holds `TelegramAdapter` (live) and `WhatsAppAdapter`
(stub — raises `NotImplementedError`, fully documented mapping in its
docstring). `app/redis_client.py` is the optional operational layer (see
below). `app/analytics.py` backs the dashboard's aggregate endpoints.

`conversation_id` is always `"{channel}:{user_id}"` (e.g.
`telegram:5377541635`) — stable, never rotates, is the join key across every
table and every dashboard URL. Anything that needs to identify a conversation
across systems should use this, not a synthetic id.

## Database

Supabase project `djawztrswbstqwceznnz` (Singapore region). Migrations in
`migrations/`, applied in order, idempotent. RLS is on for every table —
`service_role` (used by this backend) bypasses it by design; `anon`/
`authenticated` are deny-all except `machines` (public catalog read, active
rows only). Full schema reasoning in `docs/data-model.md` (gitignored, local
reference only — not shipped, ask if you need it summarized).

## WhatsApp status — read this before touching anything WhatsApp-related

- The client's real WhatsApp Business Cloud API credentials (access token,
  phone number ID, WABA ID) **are already in `.env`**, added 2026-08-16.
- **The phone number is the same one currently served by a separate, live,
  already-in-production system** — a Google Apps Script
  (`app_script/app.gs`, reference copy only, not part of this app's runtime)
  that receives Meta's webhook directly and does bulk template sending from a
  Google Sheet, plus `whatsapp-portal-divine.vercel.app` (a completely
  separate Next.js + Supabase project, repo `Divine-Empire/whatsapp-portal`,
  its own Supabase project `zpkikvgmmbtekbcuqahf` — not ours).
- Meta allows **one webhook URL per phone number.** Registering this app's
  webhook for that number would silently break the Apps Script's live
  webhook (or vice versa). This is not implemented and is not a "just wire it
  up" task — it needs a coordinated cutover decision (replace the existing
  webhook, or get a second number) that only the user makes.
- Explicit instruction as of 2026-08-17: **wait for the user's go-ahead**
  before implementing `WhatsAppAdapter`, registering any webhook, or sending
  anything through the Cloud API. Config plumbing (`app/config.py` settings,
  `.env.example` docs) is done; the adapter itself is untouched.
- Do not read WhatsApp portal secrets. If investigating, its Vercel env vars
  for `WHATSAPP_TOKEN`/`WHATSAPP_PHONE_NUMBER_ID`/`WHATSAPP_WABA_ID` are
  marked Sensitive (write-only via CLI) — that's intentional platform access
  control, not a puzzle to route around.

## Redis (optional operational layer)

`.claude/Addition.md` (gitignored, local planning doc) is the full phased
plan. **Only Phase A is implemented**: `app/redis_client.py` — connection
lifecycle, versioned key helper (`de:v1:...`), a `safe()` degradation
wrapper, and `/ready` (separate from `/health`, which stays
dependency-free). Redis is never the system of record; every feature must
work identically with it disabled. `REDIS_ENABLED` currently `true` in local
`.env` against a real Redis Cloud instance — **not yet added to Render's
production environment.** No dedup, locking, rate limiting, job queue, or
caching is implemented yet — those are Phases B–G, one at a time, each behind
its own settings flag (already present in `app/config.py`, all default
`false`).

## What the dashboard (sibling repo) can rely on

The `/api/*` surface as of this writing: `leads`, `handovers` (+ PATCH status
and PATCH category-override), `conversations/{id}`, `overview`,
`reports/{type}`, `customers` (+ PATCH), `opt-outs`, `summaries`, `logs`,
`machines` (+ upload/text-add/delete). All require `X-API-Key`. There is
**no purpose-built "conversation list/inbox" endpoint yet** — if the
dashboard's Telegram inbox needs one (last-message preview joined against
`current_leads`/`conversation_summaries`), that's new backend work, ask for
it rather than assembling it from multiple round trips in the browser.
There is **no unread/read-tracking concept anywhere in the schema.**

`current_leads` is a view over append-only `lead_scores` — every score
(AI-generated or manually overridden from the dashboard) is a new row, never
an update. This is deliberate: ranking history stays auditable. A manual
override via `PATCH /api/leads/{id}?category=` writes
`factors: {"manual_override": 1}` so it's distinguishable from an AI score at
a glance, and the next real message still triggers ordinary AI re-scoring —
overriding a lead's category is a correction, not a permanent lock.

## Known real bugs already fixed here (don't reintroduce)

- The handoff tool used to make the model recite contact details on every
  subsequent message after one bulk-order request, effectively ending the
  conversation. Fixed in `app/prompts.py`/`app/agent.py`: a handoff is a
  point-in-time notification, the model must keep selling afterward. If
  you're touching the system prompt or the `request_human_handoff` tool
  description, re-read that commit (`10debbd`) first.
- `GROQ_MODEL` must be `openai/gpt-oss-120b`, not the bare model name — Groq
  404s otherwise, and that only surfaces when the primary model is already
  down.

## Conventions

`uv` only, never pip. `ruff check`/`ruff format` before committing.
Conventional commits. Push to both `divine` and `origin` remotes.
`docs/` and `.claude/` are gitignored (planning material, not shipped code)
— if you need their content, ask; don't assume it doesn't exist just because
`git ls-files` won't show it.
