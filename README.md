# Divine Empire Sales Agent

An AI sales agent for Divine Empire India Pvt. Ltd. — construction equipment, survey instruments, civil lab equipment, construction chemicals and safety items.

Runs on **Telegram** today. WhatsApp Business Cloud API is designed for and stubbed, pending client credentials.

The agent qualifies leads, answers product questions from the company catalog, captures leads to Supabase, alerts the sales team, and escalates anything commercial to a human. It speaks English, Hindi and Hinglish.

---

## How it works

```
Telegram webhook          WhatsApp Cloud API  (stub)
       │                          │
       └──────► ChannelAdapter ◄──┘
                     │
        IncomingMessage / OutgoingMessage   (channel-agnostic)
                     │
                 AgentCore
                     │  qualify · retrieve · tools · hand off
     ┌───────────────┼───────────────┐
     │               │               │
 LLM gateway     ProductRAG        Store
 GPT-4o→Groq      Qdrant          Supabase
```

The core never learns which chat platform it is talking to. `conversation_id` is always `{channel}:{user_id}` — `telegram:12345` today, `whatsapp:919876543210` later. That convention is what makes the WhatsApp migration an adapter plus one route.

---

## Quick start

Prerequisites: **Python 3.12**, [**uv**](https://docs.astral.sh/uv/), and accounts for OpenAI, Groq, Qdrant Cloud, Supabase and Telegram.

```bash
git clone https://github.com/Divine-Empire/sales-agent.git
cd sales-agent
uv sync
cp .env.example .env      # then fill it in — see below
```

### 1. Supabase

Create a project (region: **Singapore**, to match where the app deploys), then run the migrations in order from the SQL Editor:

```
migrations/001_initial_schema.sql      11 tables + current_leads view
migrations/002_security_hardening.sql  security_invoker view, pinned search_path
migrations/003_rls_policies.sql        explicit deny-all + public catalog read
```

From **Settings → API** copy the project URL into `SUPABASE_URL` and the **`service_role`** key into `SUPABASE_SERVICE_KEY`. The service key bypasses RLS by design and must stay server-side.

### 2. Qdrant

Create a free cluster, put the URL and API key in `.env`, then ingest the catalog:

```bash
uv run python -m app.rag
# → ingested 18 chunks into 'sales-agent'
```

Re-running updates in place; point ids derive from the heading, so it never duplicates.

### 3. Telegram

Message [@BotFather](https://t.me/BotFather):

```
/newbot                 → name and username, returns the token
/setcommands            → paste the block below
/setdescription         → shown before a customer presses Start
```

```
start - Start a conversation
help - What I can help you with
products - What we supply
contact - Sales team contact details
clear - Clear the conversation and start fresh
stop - Stop receiving messages
```

Get your numeric chat id from [@userinfobot](https://t.me/userinfobot) for `OPS_CHAT_ID` — that is where lead and handoff alerts arrive. Open a chat with your bot and press **Start**, or it cannot message you.

Generate a webhook secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 4. Run

```bash
uv run uvicorn app.main:app --reload --port 8000
curl localhost:8000/health
```

Telegram needs a public HTTPS URL. For local development, tunnel:

```bash
ngrok http 8000
curl -X POST "localhost:8000/admin/telegram/set-webhook?url=https://YOUR-NGROK-URL"
```

Message your bot. You should get a sales reply, and an ops alert once a lead is captured.

---

## Configuration

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | GPT-4o (primary) and embeddings |
| `GROQ_API_KEY` | Fallback provider |
| `GROQ_MODEL` | Must be `openai/gpt-oss-120b` — the bare id does not exist on Groq |
| `QDRANT_URL` / `QDRANT_API_KEY` | Vector store |
| `QDRANT_COLLECTION` | Default `sales-agent` |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | Database; service_role key |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `OPS_CHAT_ID` | Where lead and handoff alerts go |
| `TELEGRAM_WEBHOOK_SECRET` | Must match what `setWebhook` registered |
| `PORT` | Default 10000; Render injects this |

`SUPABASE_DB_PASSWORD` and `SUPABASE_POOLER_URL` are for running migrations from your machine. The app talks to Supabase over the REST API and never needs them in production.

---

## Deploying to Render

Docker web service, no blueprint needed:

1. **New → Web Service**, connect the repo
2. **Runtime: Docker**, region **Singapore** (same as Supabase — every DB round trip becomes a local hop, and one conversation turn makes several)
3. Health check path: `/health`
4. Add each environment variable from the table above
5. Deploy, then register the webhook:

```bash
curl -X POST "https://YOUR-SERVICE.onrender.com/admin/telegram/set-webhook"
```

With no `url` parameter it uses `RENDER_EXTERNAL_URL`, which Render sets automatically.

**The free tier sleeps after 15 minutes idle.** The first message after a sleep takes ~30 seconds to wake the service. Send one test message before any demo. All state lives in Supabase, so sleeping loses nothing.

---

## API

| Route | Purpose |
|---|---|
| `GET /health` | Health check; dependency-free by design |
| `POST /webhooks/telegram` | Telegram updates; verifies the secret header |
| `POST /admin/telegram/set-webhook` | Register the webhook |
| `GET /admin/telegram/info` | Bot identity — confirms the token is live |
| `GET /api/leads` | Ranked leads (`?limit=`, `?category=hot`) |
| `GET /api/handovers` | Handover queue (`?status=pending`) |
| `GET /api/conversations/{id}` | Full history plus current summary |

> **The `/api/*` routes are unauthenticated** and expose customer PII. They back a future dashboard and must not reach a public production deployment without auth. This is the largest known security gap.

---

## Demo script

Run these before any client demo. They are the eval set; re-run after every prompt change.

| # | Send | Expect |
|---|---|---|
| 1 | "do you have bar bending machines?" | Asks bar diameter — one question, not three |
| 2 | "32mm TMT, RCC building work" | GW42 class, ~₹79,000, offers the cutter as a pair |
| 3 | "Rajesh Kumar from Shreeji Constructions" | Lead alert in the ops chat |
| 4 | "I want a formal quotation for 3 ride on rollers" | Handoff alert with context + contact details |
| 5 | "what is the price of your CNC laser cutting machine?" | Declines — we do not stock it. **Must not invent a price** |
| 6 | "नमस्ते, क्या आपके पास टोटल स्टेशन है?" | Replies in Hindi with catalog prices |
| 7 | "bhai safety helmet ka rate kya hai?" | Replies in Hinglish |
| 8 | "/clear" | Forgets the conversation; the lead survives |
| 9 | "stop messaging me" | Opts out immediately; no further replies |

Script 5 is the important one. Inventing a specification is the failure that ends a demo.

**Before demoing:** wake the Render service, confirm the Supabase project is not paused (free tier pauses after 7 days idle), run the scripts, check a lead row lands, and grep logs for `llm_fallback` so you know whether you are running on Groq before the client asks.

---

## Development

```bash
uv run ruff check . && uv run ruff format .
uv run python -m app.rag          # re-ingest the catalog
docker build -t sales-agent .     # verify the image builds
```

### Layout

| Module | Responsibility |
|---|---|
| `app/config.py` | Environment settings; blank vars fall back to defaults |
| `app/models.py` | Channel-agnostic pydantic models |
| `app/enums.py` | Vocabularies mirroring the SQL CHECK constraints |
| `app/store.py` | Supabase; every failure caught so a DB hiccup never costs a reply |
| `app/llm.py` | The only module that calls OpenAI or Groq |
| `app/rag.py` | Qdrant search and `python -m app.rag` ingestion |
| `app/prompts.py` | The system prompt — treat changes as code |
| `app/agent.py` | Agent core, three tools, capped loop |
| `app/commands.py` | Slash commands answered without an LLM call |
| `app/intelligence.py` | Post-reply scoring, intent, summaries |
| `app/channels.py` | Telegram adapter, WhatsApp stub |
| `app/main.py` | FastAPI routes |

### Design decisions worth knowing

**The tool loop is capped at 3 iterations.** An uncapped agent loop eventually burns an API budget.

**Fallback only on retryable errors** — rate limit, timeout, connection, 5xx. A 400 fails identically on Groq; that is a bug to fix, not to retry.

**The webhook acks in under a millisecond** and processes in a detached task. The LLM round-trip alone exceeds Telegram's timeout, and a slow ack means retries and duplicate replies.

**`save_lead` rejects placeholder values.** The model fills required fields rather than skipping the call, which produced real leads reading "Unknown | Unknown" during testing. It now returns an error telling the model to ask.

**`/clear` deletes messages only.** Leads, summaries, handovers and opt-outs survive. A customer tidying their chat is not withdrawing an enquiry, and a dropped opt-out would be a compliance failure.

**Scoring runs after the reply.** It is an LLM call the customer never sees; inline it would add seconds to every turn. Scores are appended, never updated, so ranking movement stays auditable.

**Retrieval failure is never fatal.** Empty or low-scoring results mean the agent says it will check with the team instead of inventing a specification.

---

## Migrating to WhatsApp

When the client provides Cloud API credentials:

1. Implement `parse` and `send` in `WhatsAppAdapter` — the full payload mapping is in its docstring
2. Add one route in `app/main.py`: `GET` for `hub.challenge` verification, `POST` for messages
3. Verify `X-Hub-Signature-256` (stricter than Telegram's plain secret header)

Nothing else changes. Store, agent, RAG, scoring and reporting already work off `conversation_id`.

---

## Status

| Phase | |
|---|---|
| 1 — Scaffold | Done |
| 2 — Schema + models | Done |
| 3 — Supabase store | Done |
| 4 — LLM gateway | Done |
| 5 — RAG | Done |
| 7 — Agent core | Done |
| 9 — Channels | Done |
| 10 — API | Done |
| 11 — Deployment | Done |
| 8 — Lead scoring, intent, summaries | Done |

All BRD capabilities are implemented except the dashboard UI, which is a separate project.

### Known gaps

- `/api/*` routes are unauthenticated (see above)
- The dashboard UI is a separate project; this repo provides the API behind it
- Catalog prices are approximate public listings — the agent is instructed to say so and to offer a callback for exact pricing
- Hybrid dense+sparse retrieval is the upgrade path for model-number queries; dense-only is adequate at this catalog size
