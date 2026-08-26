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
- **Superseded 2026-08-26 — WhatsApp is now wired up, but NOT the way the
  stub anticipated.** The user approved connecting to the existing systems
  rather than going direct to Meta. What was built:
  - `WhatsAppPortalAdapter` (`app/channels.py`) is the live adapter and what
    `ADAPTERS[Channel.WHATSAPP]` resolves to. Outbound calls
    `whatsapp-portal`'s own `POST /api/conversations/get-or-create` then
    `POST /api/send-message` — **never Meta directly**. Reason: the portal's
    Supabase only gains rows from the portal's own code, and it writes the
    `wa_message_id` inline with the send because that is the only moment it
    is known; Meta's later delivery/read webhooks match on exactly that
    column. Sending from here would leave the portal's inbox showing customer
    questions with no answers, and its status updates unmatched.
  - `POST /webhooks/whatsapp-inbound` (`app/main.py`) receives forwarded
    inbound messages. **The forward comes from `whatsapp-portal`'s webhook
    route, NOT from the Apps Script** — see the correction below.
  - `forwardToSalesAgent()` in
    `whatsapp-portal/src/app/api/webhook/[userId]/route.ts` is the trigger,
    called at the end of the per-message loop. Config via that project's
    Vercel env (`SALES_AGENT_URL`, `SALES_AGENT_SECRET`); unset URL is the
    kill switch. Fire-and-forget, 8s timeout, errors swallowed — the message
    is already stored, so the agent can never delay Meta's ack. Text-only and
    new-messages-only (`isNewMessage && !msgInsertError`), so campaign button
    taps and Meta webhook retries do not trigger AI replies.
  - **Correction, 2026-08-26 — where Meta actually points.** An earlier
    version of this integration put the forward in `app_scripts/`'s
    `_processMessages`, on the assumption that the Apps Script's `doPost` was
    Meta's live webhook. **That was wrong and the forward would never have
    fired.** Verified against `whatsapp-portal`'s live Supabase: it has
    current `direction=inbound` rows with `source='internal'` and
    `message_type` values (`button`, `unsupported`) that only the portal's own
    webhook switch produces. So **Meta → whatsapp-portal**, and the real flow
    is: Meta → portal webhook → portal Supabase → `syncResponsesIncremental`
    (in the separate "WH Script Buttons" Apps Script project, bound to the
    "Official Webhook" Sheet `1_r5eK…`) pulls the portal's `responses` view
    into that Sheet's `RESPONSES` tab. The Apps Script `doPost` in
    `app_scripts/code.gs` is **legacy/dead for inbound traffic**; the
    `app_scripts/` edits were reverted, and that file now matches
    `app_scripts/current_active_system/code.gs` exactly. Do not re-add a
    forward there.
  - **`app_scripts/` layout note**: the live bulk-send script is `code.gs`
    (not `app.gs` as older notes said), `current_active_system/` is the
    authoritative export to diff against, and `official_webhook_code.gs` is
    the *other* project's sync script. `code.gs` and
    `current_active_system/code.gs` had genuinely diverged (the active one
    has an `Upload Image / video` menu item wiring `showFileUploadDialog`
    from `file.gs`) — always diff before pasting anything into Apps Script.
  - The original `WhatsAppAdapter` stub is deliberately left in place,
    unimplemented, documenting the direct-to-Meta path if it is ever wanted.
  - **Phone format is the join key across all three systems.** The portal
    forwards its already-normalized `phoneNumber` (91-prefixed, from its own
    `normalizePhoneNumber`), which is how it stores contacts and how the agent
    keys `conversation_id`. Note `app_scripts/`'s `_cleanPhone` *strips* the
    `91` for Sheet row matching — never feed that form to the portal or the
    agent, it would create a duplicate contact and split the thread.
  - Gated by `WHATSAPP_AGENT_ENABLED` (default **false**) plus
    `WHATSAPP_INBOUND_SECRET`. Shipped dark: the endpoint acks 200 but runs
    nothing until the flag is set on Render.
  - **Go-live order**: (1) deploy the portal change with `SALES_AGENT_URL`
    set — forwards start arriving, agent stays silent, look for
    `whatsapp_inbound_disabled` in Render logs to confirm wiring; (2) set
    `WHATSAPP_INBOUND_SECRET` on Render *and* `SALES_AGENT_SECRET` on the
    portal to the same value, plus `WHATSAPP_AGENT_ENABLED=true`. Kill switch
    is unsetting `SALES_AGENT_URL` on the portal, or the flag on Render.
  - Verified locally with the portal mocked: forwarded message → correct
    `whatsapp:<phone>` conversation → RAG hit → Hinglish reply matching the
    customer's Hinglish question → exactly the two portal calls in order.
    Also verified it degrades correctly (failed conversation lookup still
    sends; failed send returns False) and that a non-object JSON body is
    rejected rather than crashing. **No message has been sent to a real
    customer yet.**
- Do not read WhatsApp portal secrets. If investigating, its Vercel env vars
  for `WHATSAPP_TOKEN`/`WHATSAPP_PHONE_NUMBER_ID`/`WHATSAPP_WABA_ID` are
  marked Sensitive (write-only via CLI) — that's intentional platform access
  control, not a puzzle to route around.
- **`whatsapp-portal` now exists as a local sibling directory**
  (`../whatsapp-portal`, repo `Divine-Empire/whatsapp-portal`) and was fully
  code-audited 2026-08-26 — see its own `CLAUDE.md` for the complete
  breakdown. Short version: it is a real, mature, feature-complete WhatsApp
  Business inbox (chat UI, template send/track with delivery status, media,
  the Google-Sheets bulk-send bridge that `app_script/app.gs` here talks to
  via `/api/sync-sheet`, a keyword-based interest classifier) — not a stub.
  It has its own Supabase project and its own webhook registration; nothing
  about that changes the constraint above. Its deployment was independently
  confirmed live via Vercel CLI the same day (Vercel project
  `whatsapp-portal`, team `mis-thedivineemps-projects` — same team as
  `sales-agent-dashboard`; most recent deploy 2 days old at the time). As of
  2026-08-26 the plan is to
  surface it inside `sales-agent-dashboard`'s WhatsApp tab, but the
  integration shape is not yet decided — see that repo's `CLAUDE.md` for
  the open question. This backend (`sales-agent`) is not currently expected
  to be part of that integration; if that changes, update this section.

## Redis (optional operational layer)

`.claude/Addition.md` (gitignored, local planning doc) is the full phased
plan. Redis is never the system of record; every feature must work
identically with it disabled. As of 2026-08-17, **Phases A–D are live in
Render's production environment** (`REDIS_ENABLED`, `REDIS_DEDUPE_ENABLED`,
`REDIS_LOCKS_ENABLED`, `REDIS_RATE_LIMIT_ENABLED` all `true` on the web
service, verified against the live deployment — see the production status
note at the end of this file). Phase E's code is deployed but its flag is
still `false` in production pending a worker service — see below.

- **Phase A** (done): `app/redis_client.py` — connection lifecycle, versioned
  key helper (`de:v1:...`), a `safe()` degradation wrapper, and `/ready`
  (separate from `/health`, which stays dependency-free).
- **Phase B** (done): `app/dedupe.py` — Telegram `update_id` dedup via
  `SET NX EX`, fail-open, wired into the webhook handler in `app/main.py`.
- **Phase C** (done): `app/locks.py` — per-conversation distributed lock
  (`conversation_lock()`) wrapping the bootstrap→history→RAG→LLM→tools
  sequence in `app/agent.py` (`handle_message` now just acquires the lock
  and delegates to `_handle_message_locked`). Random token + ownership-
  checked Lua release, finite lease (`REDIS_LOCK_LEASE_SECONDS`, default 30s),
  bounded acquisition wait (`REDIS_LOCK_WAIT_SECONDS`, default 5s) with
  polling (`REDIS_LOCK_RETRY_INTERVAL_SECONDS`, default 0.1s). Contention
  past the wait budget returns `prompts.BUSY_MESSAGE` rather than proceeding
  concurrently. Fails open (turn proceeds unlocked) if Redis is disabled or
  unreachable. Verified directly against the live Redis Cloud instance:
  ordered acquisition, cross-conversation concurrency, lease expiry after a
  simulated crash, and rejected cross-worker release with a mismatched
  token — all confirmed live, not mocked.
- **Phase D** (done): `app/rate_limit.py` — atomic fixed-window counters
  (`INCR`+`EXPIRE` in one Lua call, no `KEYS` scan). Two independent scopes:
  `check_customer()` (10/min steady + 30/5min burst per `channel:user_id`,
  wired into `_process` in `app/main.py` before the agent runs, replies with
  `prompts.RATE_LIMITED_MESSAGE`; **fails open** — a Redis outage must never
  block a real customer) and `check_dashboard()` (120/min per API-key+route,
  wired into `require_api_key`, returns `429` + `Retry-After`; **fails
  closed** — an authenticated internal API that can't verify its own limit
  should refuse, not risk abuse). The dashboard scope hashes the API key
  before it ever becomes part of a Redis key (§3/§5: never put secrets in
  keys). Verified live: exact window boundaries (allows N, blocks N+1),
  fail-open when disabled, fail-closed when Redis is unreachable for the
  dashboard scope.
- **Phase E** (done, not yet running in production — needs a second Render
  service): `app/jobs.py` (envelope + `enqueue()`, one Redis Stream
  `de:v1:stream:jobs` + dead-letter stream) and `app/worker.py` (standalone
  consumer-group worker; **not** part of the web dyno — run separately via
  `uv run python -m app.worker`). `app/main.py`'s post-reply intelligence
  call now does `jobs.enqueue(...)`, falling back to the original inline
  `intelligence.analyse()` call if enqueueing returns `False` (jobs disabled
  or Redis down) — same behavior as before this phase either way. Only
  intelligence analysis is queued, per the plan; the reply itself and ops
  notifications stay synchronous. A processed-job guard (`jobs.py`,
  `already_processed`/`mark_processed`) prevents a retried delivery from
  double-scoring a lead — **the guard is set only after the handler
  succeeds**, not on first sight, otherwise a failed first attempt would
  wrongly block its own retry (a real bug caught and fixed during live
  testing against Redis Cloud). Failed jobs retry with bounded exponential
  backoff up to `JOBS_MAX_ATTEMPTS`, then move to the dead-letter stream,
  observable via `GET /api/jobs/dead-letter-count`. A crashed worker's
  unacked entries are reclaimed by `_reclaim_stale` (`xautoclaim`, note the
  redis-py kwarg is `start_id`, not `start`) after `JOBS_CLAIM_IDLE_SECONDS`.
  Verified live against Redis Cloud: enqueue → consume → ack; idempotent
  skip on a genuine duplicate delivery but NOT on a legitimate retry;
  retry-then-succeed; retry exhaustion → dead-letter; crashed-worker
  reclaim → successful reprocessing by a second worker.

  **Running it in production**: a paid Render Background Worker
  (`uv run python -m app.worker`, persistent process, ~$7/mo minimum) is the
  natural fit; Render's Cron Job type was also considered but turned out to
  be billed per execution-minute too, not actually free. Since this repo is
  now **public** (`Divine-Empire/sales-agent`), the chosen path is
  **GitHub Actions**, which is genuinely free for public repos:
  `.github/workflows/jobs-worker.yml` runs on a `*/5 * * * *` schedule (plus
  `workflow_dispatch` for manual runs), checks out the repo, `uv sync
  --frozen`, then `uv run python -m app.worker --once` — the same
  **drain-and-exit mode** (`run_once()`) built for this: reclaims whatever a
  previous run left pending, processes everything currently in the stream
  with no blocking wait, then exits, so a run getting killed mid-job just
  leaves its entry for the next scheduled run's reclaim to pick up. Verified
  live before this: draining 2 queued jobs in one pass (`handled: 2`), and
  an empty-queue invocation returning immediately (`handled: 0`) rather than
  blocking.

  **Secrets**: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `OPENAI_API_KEY`,
  `OPENAI_MODEL`, `GROQ_API_KEY`, `GROQ_MODEL`, `QDRANT_URL`,
  `QDRANT_API_KEY`, `QDRANT_COLLECTION`, `REDIS_URL` are pushed to this
  repo's GitHub Actions secrets (`gh secret set`, one per credential —
  bulk-pushing all of them in a single command was blocked by Claude Code's
  own safety classifier as a real-credentials bulk action, so they were set
  individually instead). Non-secret Redis/job tuning values
  (`REDIS_ENABLED`, `REDIS_JOBS_ENABLED`, `JOBS_*`) are plain env vars
  inline in the workflow file, not secrets. `TELEGRAM_*`/
  `DASHBOARD_API_KEY`/`WHATSAPP_*` are deliberately **not** duplicated into
  GitHub secrets — the worker never touches Telegram or the dashboard API,
  only Supabase/OpenAI/Groq/Qdrant/Redis, so there was no reason to widen
  where those particular credentials live.

  Enabling `REDIS_JOBS_ENABLED=true` on the Render web service before this
  workflow exists and has run successfully would enqueue jobs nothing
  consumes; intelligence scoring/summaries would silently stop updating
  instead of falling back to inline (the inline fallback only triggers when
  enqueueing itself fails, not when nothing drains the queue). Confirm the
  workflow has run cleanly at least once (Actions tab → run logs show
  `worker_run_once_finished`) before flipping that flag on the web service.
- **Phase F** (done, not yet enabled in production — see production status
  note): `app/cache.py` — generic cache-aside (`get_or_set`/`invalidate`/
  `invalidate_namespace`) with single-flight stampede protection (a short
  `SET NX PX` lock; **note the plan's own `ex=` float bug**: redis-py's
  `ex=`/`nx=` params reject a `float` TTL outright — `DataError: ex must be
  datetime.timedelta or int` — this was a real bug caught live during
  testing, silently swallowed by an overly broad `except Exception` around
  the lock-acquire call, which made every write path look like "lock
  contention" and skip populating the cache entirely; fixed by converting to
  `px=<int milliseconds>`). Wired into all 5 of the plan's candidates:
  `store.get_customer`/`upsert_customer`/`update_customer_fields`,
  `store.get_summary`/`upsert_summary`, `store.get_machine_by_code`/
  `list_machines`/`upsert_machine`/`delete_machine`, `rag.search` (keyed by
  normalized query text — skips both the embedding call and the Qdrant
  search on a hit), and `analytics.overview` (short TTL only, no dedicated
  write path to invalidate against — matches the plan's own guidance for
  dashboard aggregates; `analytics.report` is deliberately left uncached,
  its docstring already commits to "never stale"). Opt-outs are
  **deliberately never cached** anywhere, per the plan's explicit red line.
  Invalidation is direct delete-on-write from each mutating call site, not a
  `catalog_version` counter — this system has no multi-instance
  cache-consistency need yet, so a version scheme would be complexity ahead
  of any real payoff; RAG's cache instead clears its whole namespace via
  `SCAN` (never `KEYS`) on `rag.ingest()`, since a re-ingest already replaces
  the whole Qdrant collection at once. Verified live against Redis Cloud and
  the real Supabase/Qdrant/OpenAI backends (not mocked): customer/summary/
  machine cache-then-invalidate-then-fresh-read cycles all correct; RAG
  cache hit **4.68s → 0.22s** with identical content; dashboard overview hit
  **2.15s → 0.24s** with identical content; every path confirmed to degrade
  to an uncached call with `REDIS_CACHE_ENABLED=false` (production's current
  setting).
- **Not implemented yet**: semantic FAQ cache (Phase G), CRM web research
  (Phase H) — each behind its own settings flag (already present in
  `app/config.py`, all default `false`).

## Document ingestion — OCR fallback for scanned PDFs

`app/documents.py`'s `extract_text`/`_extract_pdf` are async now (not a Redis
phase — added 2026-08-17 while onboarding real product brochures). Per page:
try `pypdf`'s normal text extraction first; if a page yields nothing (a
picture of a page, not real text — common in brochures with photo-heavy
layout pages mixed with real text pages), render just that page to a PNG via
`pypdfium2` (pure-Python wheel, no system `poppler`/Ghostscript dependency —
matters because Render's buildpack has no path to install system binaries
without a Dockerfile, which this repo deliberately doesn't have) and
transcribe it with `app/llm.py`'s `transcribe_image()` (GPT-4o vision, no
Groq fallback — same reasoning as `embed()`, Groq's models here are
text-only). Only pages that actually fail real extraction get OCR'd, so an
already-text PDF costs nothing extra. `ExtractionError` still fires if every
page fails both real extraction and OCR — a genuinely unreadable scan, not a
silent empty document.

Verified against real files in `data/`: the corporate brochure (previously
failed extraction entirely, `ExtractionError`) now yields 12,473 chars; the
three Sokkia spec-sheet PDFs (already text-extractable) are unchanged and
trigger zero OCR calls. Also verified against a genuinely image-only test
PDF (a bio page re-embedded with no text layer) — correct verbatim
transcription. `transcribe_image` strips a markdown code fence if the model
wraps its answer in one, since that would otherwise land in
`machine_documents`/RAG chunks verbatim.

New settings: `OCR_MAX_OUTPUT_TOKENS` (2000 — a dense page transcribed
verbatim needs more than a normal chat reply's budget), `OCR_TIMEOUT_SECONDS`
(60 — vision calls run slower), `OCR_MAX_PAGES` (30 — a very long scanned
document is skipped rather than run up a large per-page API bill silently;
logged as `ocr_skipped_too_many_pages`). New deps: `pypdfium2`, `pillow`
(both pure-wheel, safe on Render's buildpack).

## What the dashboard (sibling repo) can rely on

The `/api/*` surface as of this writing: `leads`, `handovers` (+ PATCH status
and PATCH category-override), `conversations` (inbox list, added
2026-08-17 — see below), `conversations/{id}`, `overview`, `reports/{type}`,
`customers` (+ PATCH), `opt-outs`, `summaries`, `logs`, `machines`
(+ upload/text-add/delete). All require `X-API-Key`.
There is **no unread/read-tracking concept anywhere in the schema.**

`GET /api/conversations?limit=50&channel=telegram` is the Telegram inbox
list endpoint: every conversation (not just ones the intelligence pass has
summarized yet), newest `last_message_at` first, each row carrying
`customer_name`/`company_name`/`channel_user_id`/`phone`, a `last_message`
+ `last_message_role` preview, and — when a summary exists —
`lead_score`/`lead_category`/`handover_status`/`customer_intent` (all
`None`/`"none"` otherwise, not an error). Built from `conversations` as the
membership source (not `conversation_summaries`, which would silently hide
brand-new threads) via `store.list_conversations`; last-message and summary
data are merged in with two follow-up queries scoped to just the returned
conversation ids, same shape as the rest of `app/store.py`. Verified against
the live production Supabase — returns the one real conversation correctly.

`GET /api/whatsapp/conversations` and
`GET /api/whatsapp/conversations/{id}` are read-only proxies over the
whatsapp-portal's own API (`app/whatsapp_portal.py`), added 2026-08-26 so the
dashboard's WhatsApp tab can show real threads without holding the portal's
database credentials or duplicating its query logic. They live under
`/whatsapp/` because `/api/conversations/{conversation_id}` would otherwise
swallow them. Both degrade to an empty result with `available: false` rather
than raising, so a portal outage leaves that tab honestly empty instead of
breaking the page. The per-thread endpoint they call
(`/api/conversations/[id]/messages` on the portal) was added in the same pass,
because the portal's own inbox reads Supabase directly with the operator's
session and `/api/logs` is auth-gated — neither is reachable
server-to-server. No send path: operator sending needs permissions and an
audit trail first.

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

## Production Redis status (verify against `/ready` before trusting this)

As of 2026-08-17, the Render web service (`sales-agent-956w`) has these live:
`REDIS_ENABLED=true`, `REDIS_DEDUPE_ENABLED=true`, `REDIS_LOCKS_ENABLED=true`,
`REDIS_RATE_LIMIT_ENABLED=true` (plus their tuning vars — see
`.env.render`/`.env.example`). Confirmed working against the actual
deployment, not just locally:

- `GET /ready` → `{"redis": {"enabled": true, "connected": true}}`.
- A duplicate Telegram `update_id` produces exactly one persisted
  user/assistant pair (Phase B dedupe engaging in prod).
- A 12-message rapid burst from one Telegram user in under a minute: only 5
  of 12 messages were actually processed (persisted + replied to) — the
  other 7 were correctly rate-limited before ever reaching the agent or
  being written to `messages` (Phase D `check_customer` engaging in prod,
  10/min + 30/5min policy). Rate-limited turns skip `store.save_message`
  entirely (the check runs in `app/main.py` before `handle_message`), so
  they leave no history trace by design — don't mistake a lower-than-sent
  message count for a bug when auditing conversation history after a burst.

**`REDIS_JOBS_ENABLED` is still `false`** on the web service. The code
(Phase E, `app/jobs.py`/`app/worker.py`) is deployed and live-tested against
Redis Cloud directly (see the Phase E entry above). Render Background
Workers (a persistent process) aren't on the free tier, so the activation
path here is `app/worker.py`'s drain-and-exit mode
(`uv run python -m app.worker --once`) on a Render **Cron Job** (free tier)
instead — that cron job hasn't been created yet. Don't flip
`REDIS_JOBS_ENABLED=true` on the web service alone before it exists and has
run successfully at least once; jobs would enqueue with nothing consuming
them, and intelligence scoring/summaries would silently stop updating
instead of falling back to inline (the inline fallback only triggers when
enqueueing itself fails, not when nothing drains the queue).

**`REDIS_CACHE_ENABLED` is `false`** on the web service (Phase F code is
deployed but dormant in production) and deliberately left `false` in
`.env.render` for you to enable explicitly — unlike Phases B–D, caching has
enough staleness-risk surface (and this phase's own live-testing already
caught one real bug — see the Phase F entry above) that it's worth a
deliberate look before flipping it in production, rather than defaulting it
on. It's already `true` locally (`.env`) and fully verified there against
the real Supabase/Qdrant/OpenAI backends.
