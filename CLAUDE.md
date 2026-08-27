# sales-agent — project context

This file exists so any agent (or human) working in this repo, or in the
sibling `sales-agent-dashboard` repo, has an accurate picture of what this
service is, what state it's in, and what not to touch. Keep it current —
update it in the same commit as any change that makes a claim below stale.

## What this is

The AI sales agent for **Divine Empire India Pvt. Ltd.** (industrial/
construction machinery). FastAPI backend on Render, Supabase for canonical
data, Qdrant for product-knowledge RAG, GPT-4o with a Groq fallback.

Live channels: **Telegram** and, since 2026-08-26, **WhatsApp** — the latter
through the existing `whatsapp-portal`, never Meta directly. Read the
"WhatsApp status" section below before touching anything WhatsApp-related:
three systems share one phone number and one Meta webhook slot, which is
still the most important constraint in this file.

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
`app/channels.py` holds `TelegramAdapter` (live), `WhatsAppPortalAdapter`
(live — sends via the portal's API) and `WhatsAppAdapter` (a deliberate
unimplemented stub documenting the direct-to-Meta path, kept for reference;
`ADAPTERS` does not use it). `app/whatsapp_portal.py` is the read side of the
portal relationship, backing the dashboard's WhatsApp tab.
`app/redis_client.py` is the optional operational layer (see below).
`app/analytics.py` backs the dashboard's aggregate endpoints.

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

## WhatsApp — live since 2026-08-26, read this before touching it

**Current state.** WhatsApp is a live channel. The AI answers inbound
customer messages automatically. Three systems share **one phone number**
(`+91 70242 22373`, Meta phone_number_id `945246702014228`) and Meta permits
exactly **one webhook URL per number** — that slot belongs to
`whatsapp-portal`. Everything below follows from that.

**The flow, end to end:**

```
customer → Meta → whatsapp-portal /api/webhook/{userId}   (owns the webhook)
                    ├─ stores the message in its own Supabase
                    └─ forwardToSalesAgent() ──→ this service
                                                  POST /webhooks/whatsapp-inbound
                                                    ↓ agent decides a reply
                                                  portal /api/send-message → customer
```

- **Inbound**: `POST /webhooks/whatsapp-inbound` (`app/main.py`) receives a
  small JSON body forwarded by `forwardToSalesAgent()` in
  `whatsapp-portal/src/app/api/webhook/[userId]/route.ts`. Text-only and
  new-messages-only, so campaign button taps and Meta webhook retries do not
  trigger AI replies. Config on the portal's Vercel env
  (`SALES_AGENT_URL`, `SALES_AGENT_SECRET`); unsetting `SALES_AGENT_URL` is
  the kill switch. Gated here by `WHATSAPP_AGENT_ENABLED` +
  `WHATSAPP_INBOUND_SECRET` (both live on Render).
- **Outbound**: `WhatsAppPortalAdapter` (`app/channels.py`) calls the portal's
  `/api/conversations/get-or-create` then `/api/send-message` — **never Meta
  directly.** The portal's Supabase only gains rows from the portal's own
  code, and it writes `wa_message_id` inline with the send because that is
  the only moment it is known; Meta's later delivery/read webhooks match on
  exactly that column. Sending from here would leave the portal's inbox
  showing customer questions with no answers and its status updates
  unmatched. The conversation id is cached (24h) — it never rotates, and
  resolving it cost 1.7–4.1s per reply. As of 2026-08-27, outbound text is
  also run through `to_whatsapp_text()` (`app/channels.py`) before sending —
  the WhatsApp counterpart to `to_telegram_html()`: same Markdown-shaped
  model output (`### heading`, `- bullet`, `**bold**`), converted to
  WhatsApp's own plain-text markup (`*bold*` single-asterisk, no native
  heading/list elements) instead of HTML, since WhatsApp has no parse-mode
  concept. Deliberately a separate function rather than a shared one with
  Telegram's — the two platforms' bold syntax differs enough that branching
  inside one function would be harder to follow than two short ones.
- **Reads**: `app/whatsapp_portal.py` + `/api/whatsapp/conversations[/{id}]`
  back the dashboard's WhatsApp tab, so that project needs no portal
  credentials. See "What the dashboard can rely on" below.
- **First-contact greeting** (added 2026-08-27): WhatsApp has no
  slash-command concept in this flow (the forwarded payload is just
  `{from, text, ...}`), so unlike Telegram's `/start` there's no way for a
  customer to ask for the company intro explicitly. `app/agent.py` detects a
  genuine first message instead — `store.has_prior_messages(conversation_id)`
  checked before this turn's own message is saved — and prepends
  `commands.START_MESSAGE` to the model's actual reply, so the customer gets
  one message (intro + answer to whatever they asked), not two separate
  sends. Same constant Telegram's `/start` returns. Its `Brochure:` line is a
  hardcoded Google Drive **direct-download** URL
  (`drive.google.com/uc?export=download&id=...`), not the share-page link —
  the share page renders an HTML viewer, not the raw PDF, and reads as
  broken when tapped from inside a chat app. Deliberately hardcoded rather
  than an env setting: the link isn't sensitive and isn't expected to change
  often, so a runtime setting wasn't worth the extra indirection. No native
  document/file attachment on either channel yet — both
  `TelegramAdapter.send` and `WhatsAppPortalAdapter.send` are text-only
  today; the brochure is a link, not an attachment. If that Drive link ever
  breaks (Drive's virus-scan interstitial can trigger on larger files, and
  this URL form isn't an officially documented stable API), swap it for
  proper hosting rather than another Drive workaround.

**Things that will bite you:**

- **Do not register a Meta webhook for this number from this service.** It
  would silently break the portal's live webhook, taking down both the
  marketing pipeline's reply tracking and the AI. A second consumer needs a
  second number, or a coordinated cutover only the user decides.
- **`app_scripts/` does NOT receive Meta traffic.** Its `doPost` is
  legacy/dead for inbound. An earlier version of this integration put the
  forward there on that assumption and it would never have fired — verified
  against the portal's live Supabase, which holds current `direction=inbound`
  rows with `source='internal'` and `message_type` values (`button`,
  `unsupported`) that only the portal's own webhook switch produces. The real
  Sheet path is: portal Supabase → `syncResponsesIncremental` (separate "WH
  Script Buttons" Apps Script project, bound to the "Official Webhook" Sheet
  `1_r5eK…`) → that Sheet's `RESPONSES` tab. Do not re-add a forward to
  `app_scripts/`.
- **`app_scripts/` layout**: the live bulk-send script is `code.gs` (not
  `app.gs`, as older notes said), `current_active_system/` is the
  authoritative export to diff against, and `official_webhook_code.gs`
  belongs to the *other* Apps Script project. The two `code.gs` copies have
  genuinely diverged before — always diff before pasting anything in.
- **Phone format is the join key across all three systems.** Use the
  91-prefixed form (`919876543210`), which is what the portal stores and what
  this service keys `conversation_id` on. `app_scripts/`'s `_cleanPhone`
  *strips* the `91` for Sheet row matching — never feed that form to the
  portal or the agent; it creates a duplicate contact and splits the thread.
- **The `WhatsAppAdapter` stub in `app/channels.py` is deliberately left
  unimplemented**, documenting the direct-to-Meta path if it is ever wanted.
  `ADAPTERS[Channel.WHATSAPP]` resolves to `WhatsAppPortalAdapter`, not it.
- **Do not read the portal's secrets.** Its Vercel vars for
  `WHATSAPP_TOKEN`/`WHATSAPP_PHONE_NUMBER_ID`/`WHATSAPP_WABA_ID` are marked
  Sensitive — that is intentional access control, not an obstacle to route
  around.
- The client's own Cloud API credentials are in `.env` (added 2026-08-16) but
  **unused by the live path**, since outbound goes through the portal. They
  remain only for the stub's direct-to-Meta option.

**Verified before going live** (portal mocked): forwarded message → correct
`whatsapp:<phone>` conversation → RAG hit → Hinglish reply matching a Hinglish
question → exactly the two portal calls in order. Degrades correctly (failed
conversation lookup still sends; failed send returns False), and a non-object
JSON body is rejected rather than crashing.


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

  Both are live now (`REDIS_JOBS_ENABLED=true`, workflow running on
  schedule). Keep them paired: the flag on with no drain running means jobs
  accumulate and intelligence scoring/summaries silently stop updating rather
  than falling back to inline. `worker_run_once_finished` in the Actions run
  log is the signal that a drain completed.
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

## Catalog: accessories/parts (added 2026-08-27)

A second, deliberately simpler catalog table alongside `machines`:
`accessories` (`migrations/004_accessories.sql`) — `name`, `category`,
`description`, `is_active`, no other fields. **No machine linkage yet** —
that's an explicit, deferred design decision, not an oversight. Accessories
are entered manually (typed/pasted into the dashboard), never via document
upload; there is no OCR/extraction path for them and none is planned until
Google Sheet integration is revisited.

CRUD lives in `app/store.py` (`upsert_accessory`/`update_accessory_fields`/
`delete_accessory`/`list_accessories`, mirroring the `machines` functions)
and `/api/accessories` routes in `app/main.py` (GET list, POST create, PATCH
update, DELETE). Same public-read RLS exception as `machines`
(`migrations/004_accessories.sql`) since chat needs to read it without a
service-key path.

RAG ingestion reuses `app/documents.py`'s existing chunk/embed/Qdrant-upsert
machinery via a new `ingest_accessory()` — one Qdrant point per accessory
(name + description, rarely needs chunking), payload tagged
`record_type: "accessory"` so it's distinguishable from machine chunks if
that's ever needed. Deleting or deactivating an accessory removes its Qdrant
point (`delete_accessory_from_index`) — note this is *not* what `machines`
does today; `delete_machine` leaves its Qdrant chunks behind. Don't assume
parity between the two catalogs' delete behavior.

`app/prompts.py`'s `SYSTEM_PROMPT` was rewritten in the same pass (2026-08-27)
for shorter replies, an explicit cross-questioning framework (company,
location, project type, timeline), and recommending accessories alongside a
machine once one's been identified — still bound by the existing "never
invent a spec" rule. Prompt-only change; no new tools, no `agent.py`
structural change.

## What the dashboard (sibling repo) can rely on

The `/api/*` surface as of this writing: `leads`, `handovers` (+ PATCH status
and PATCH category-override), `conversations` (inbox list, added
2026-08-17 — see below), `conversations/{id}`, `overview`, `reports/{type}`,
`customers` (+ PATCH), `opt-outs`, `summaries`, `logs`, `machines`
(+ upload/text-add/delete/PATCH), `accessories` (+ POST/PATCH/DELETE, added
2026-08-27 — see "Catalog: accessories/parts" above). All require
`X-API-Key`. There is **no unread/read-tracking concept anywhere in the
schema.**

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

**Portal pagination — a real trap, documented so nobody rediscovers it.**
`app/whatsapp_portal.py`'s `list_conversations` accepts a `limit` above the
portal's per-response ceiling and assembles it from several cursor-paged
calls (`_PORTAL_PAGE_MAX = 500`). That page size is deliberate and must not be
raised to 1000: Supabase clamps any response to 1000 rows, and the portal
derives `hasMore` by over-fetching one row (`limit + 1`), so requesting
exactly 1000 gets the probe row clamped away and `hasMore` reads **false**
even with ~11,700 conversations remaining. A first attempt at 1000 stalled
dead at 1000 rows. Rows are also de-duplicated by id, because conversations
sharing a `last_message_at` can straddle a cursor boundary and repeat. A
mid-sequence portal failure keeps the pages already collected rather than
discarding a partly-built list. (This ceiling bug is the portal's, not ours —
its own inbox would stall at 1000 on infinite scroll too. Left unfixed there
deliberately: working around it on this side was the lower-risk change.)

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
- The first prompt rewrite for concise/qualifying replies (2026-08-27) used
  soft language ("weave these in as the conversation gives you an opening",
  "let the conversation lead") for the qualifying questions. In real use the
  model took that as license to skip qualifying indefinitely — a whole
  Telegram conversation went by (services, then a specific total station
  model, then spec details) with zero questions asked back about company,
  project, location, or timeline; it just kept answering. Fixed same day by
  making qualifying mandatory: goal #5 now says a reply that is only product
  information with no question and no next step is a mistake, and the
  "Qualifying" section frames it as "you are always missing at least one of
  these until you have all of them," not an optional nicety. If you're
  touching `app/prompts.py`'s qualifying language again, keep it phrased as
  a requirement, not a suggestion — the soft version measurably didn't work.
- Telegram's quick-reply keyboard (the button row above the input box,
  `app/commands.py`'s old `QUICK_REPLIES` + `TelegramAdapter.send`'s
  `keyboard` param) was removed entirely 2026-08-27, at the client's
  request — it was visual clutter next to the actual qualifying
  conversation the prompt now runs. `TelegramAdapter.send` no longer takes
  a `keyboard` argument at all; don't reintroduce it without asking first.
- The mandatory-qualifying fix above (same day) fixed the "never asks
  anything" problem but immediately surfaced a second real one: a vague
  open-ended question like "machines ke bare mein batao" got answered with
  the ENTIRE catalog — four categories, twelve machines, every price — as a
  bulleted/numbered list, with a qualifying question just tacked on at the
  end. Technically satisfied goal #5 (a question was asked) but violated
  everything else — not short, not one thing at a time, and formatted like
  a bot's structured menu rather than a person typing. Root cause: "Never
  dump everything you know" had a loophole ("unless the customer explicitly
  asked for a full list") and the model treated an open-ended question as
  implicitly asking for one. Fixed by: (1) explicitly stating a vague
  question is an opening, not a catalog request — name one or two broad
  areas, then ask what they need, only listing specifics once the customer
  has given enough context; (2) banning bullets/numbered lists/bold
  headers in normal replies outright, since a human doesn't format chat
  messages like a spec sheet. `app/channels.py`'s `to_telegram_html`/
  `to_whatsapp_text` markdown-to-platform-markup conversion is unrelated
  and still needed as a safety net for whatever formatting slips through
  despite the prompt — don't remove it thinking the prompt fix makes it
  redundant.
- Found by locally simulating a longer conversation against the real
  pipeline (rag.search -> prompts.build_messages -> llm.complete, no
  Supabase writes) after the qualifying/conciseness fixes above: a bulk
  order message caused the model to call `request_human_handoff`, and the
  literal text "request_human_handoff" appeared appended to the customer-
  facing reply. Root cause was in `_handle_message_locked`'s tool loop
  (`app/agent.py`): the assistant's tool-call turn was echoed back into
  message history with `content: response.content` verbatim — but GPT-4o
  sometimes narrates a tool call in that same content field (e.g. "I'll
  notify the team — request_human_handoff") even though the customer-facing
  reply is supposed to come only from the *next* completion, after tool
  results are fed back. That narration, once persisted in history, was
  visible to the model on a later turn and got echoed into an actual reply.
  Fixed by setting `content: None` on the echoed tool-call message instead
  of `response.content` — nothing is lost (the real reply never comes from
  this field), and the narration can no longer enter history at all.
  The leak itself was observed once, in a real user's transcript, before
  any local reproduction was attempted — the fix was made and then verified
  by running the same bulk-order scenario 4 times in a monkeypatched local
  harness (no Supabase writes) with zero recurrences of the literal tool
  name; the leak was never independently reproduced pre-fix, since GPT-4o's
  content-alongside-tool-calls narration is non-deterministic. If you touch
  this tool loop again, do not reintroduce echoing `response.content` on a
  tool-call turn.
- Found the same way (local simulation against the real pipeline): a real
  question — "IM-55 aur IM-105 mein kya difference hai", both genuine
  catalog codes — got "I don't have those specific details" even though
  both are in the catalog. Root cause was retrieval quality, not the
  prompt: Qdrant holds BOTH the original `knowledge_base.md` price-table row
  for IM-55/IM-105 AND much longer, richer spec-sheet documents uploaded
  later for a different Sokkia series (iM-62, iM-65, iM-100, FX-201/202,
  via the dashboard's upload path). On pure vector similarity, the longer
  documents' shared vocabulary ("Sokkia", "Total Station", technical specs)
  outscored the correct-but-sparse price-table row and pushed it out of the
  top-`rag_top_k` results. Also found in the process: `store.get_machine_by_code`
  (`app/store.py`) was already built for exactly this — its own docstring
  says "vector search is weakest exactly here" — but was never called from
  anywhere in the live agent path; it was dead code.
  Fixed in `app/rag.py`: `search()` now runs `_exact_code_matches()`
  alongside the vector search — `extract_codes()` (already used at
  ingestion time) applied to the CUSTOMER's message, then an exact payload
  filter (`scroll()` with a `codes` match, not `query_points()` — there is
  nothing to rank by similarity for an exact filter) against Qdrant, scored
  `1.0` and merged to the front of the result list, deduped, capped back to
  `rag_top_k` so context size is unchanged. This needed a payload index on
  `codes` that did not exist in production — `client.scroll()`'s filter
  400'd with "Index required but not found" until `ensure_collection()`
  (`app/rag.py`, already called on every document ingest) was extended to
  also call `create_payload_index` (idempotent, safe on every call) and the
  index was created once directly against production to make the fix live
  immediately rather than waiting for the next upload. Verified end-to-end:
  the same comparison question now returns the correct ₹2,89,000 / ₹3,90,000
  prices and a real comparison instead of a decline.
- After all the prompt fixes above, a real Telegram conversation still hit
  the old bulleted-catalog-dump behavior on a genuinely fresh conversation
  (no history-imitation possible) with the new code confirmed live via
  Render's deploy log AND `/api/logs` (`used_fallback: false`, model
  `gpt-4o` — ruled out a Groq-fallback theory too). `/api/logs` showed the
  reverted turn's `completion_tokens` at 346 vs ~20-40 on compliant turns.
  First attempted fix: lowered `llm_temperature` 0.3 -> 0.15 (`app/config.py`)
  — 6/6 local runs immediately after looked compliant, but **the exact same
  bulleted-dump behavior recurred live 10 minutes after that fix was
  confirmed deployed** (verified against Render's exact deploy-live
  timestamp, not just "deployed" — see the note below about always getting
  the precise time, not a yes/no). So temperature alone was not sufficient;
  a prompt instruction and a lower temperature are both a *nudge*, never a
  hard guarantee, and 600 tokens (`llm_max_output_tokens`) is easily enough
  room for a full bulleted catalog even at low temperature.
  Real fix (`app/agent.py`, same day): a post-generation guard, not another
  prompt tweak. `_looks_like_catalog_dump()` flags a reply only when it is
  unambiguously bot-shaped — 3+ bullet/numbered lines AND 400+ characters,
  deliberately conservative so a genuine short comparison (2 bullets, one
  question) is never touched. A flagged reply goes through
  `_compress_reply()`, one extra cheap completion (temperature 0.1) that
  rewrites it into 1-3 plain sentences, keeping only the most relevant
  item(s) and the original question — a real rewrite, not a truncation, so
  nothing is cut off mid-sentence the way capping `llm_max_output_tokens`
  would risk. Falls back to the original text on any compression-call
  failure. This is a hard backstop on top of the prompt/temperature layers,
  not a replacement for them — it only ever fires on the failure case, and
  costs nothing extra when the model already replied correctly.
  Verified: the detector correctly flags the original 1050-char bulleted
  reply and leaves a legitimate 231-char 2-bullet comparison alone;
  compression turns the flagged reply into a 177-char plain sentence that
  no longer flags; 10/10 fresh runs through the real `_handle_message_locked`
  path (Supabase writes monkeypatched, LLM calls real) stayed clean without
  the guard needing to fire even once, meaning temperature + guard together
  now cover both the common case and the tail.
  The guard's logging (same day) is verifiable from Render's logs alone,
  without reading transcripts: `reply_guard_triggered` fires the moment a
  dump is caught (`original_chars`); `reply_guard_result` reports what
  compression actually did (`compressed_chars`, `unchanged`,
  `still_flagged` — the direct yes/no answer to "did it work"); a `WARNING`-
  level `reply_guard_did_not_fix` fires if compression ran but the result is
  still flagged, and `reply_guard_compression_call_failed` /
  `reply_guard_compression_empty` cover the LLM-call-itself-failed case
  separately, so a Render log filter on "reply_guard" tells you everything
  about how often this fires and whether it's actually working, without
  needing to correlate against a customer's screenshot.
- A third client requirement, same day: never volunteer a price unless the
  customer actually asked for one. Tried prompt-only first — a hard rule in
  `app/prompts.py` PLUS a worked example matching the exact failing query
  ("IM-55 ke bare mein batao" should get specs with zero rupee amounts) —
  and it failed 5/5 in local testing against the real pipeline, same
  pattern as the catalog-dump case. Prompt instructions are a strong nudge
  on GPT-4o, never a hard guarantee; anything phrased as "never do X" that
  actually matters needs the code-level backstop, not more prompt tuning.
  Added a second post-generation guard in `app/agent.py`, structurally
  identical to the catalog-dump one: `_asked_for_price()` checks the
  CUSTOMER's message (not the reply) for actual price-intent words (price,
  cost, rate, budget, kitna, keemat, etc.) — deliberately a plain regex, not
  a fuzzy judgement, since unlike bullets-vs-no-bullets there's no
  legitimate ambiguous case here. If the reply contains a ₹/Rs/INR amount
  and the customer's message didn't ask for one, `_strip_unrequested_price()`
  runs one more cheap completion to rewrite the price out — a real rewrite
  (drops the pricing clause/sentence, keeps everything else), not a regex
  string-delete, which would leave broken grammar behind ("available at ."
  after deleting "₹2,89,000"). Same `_triggered`/`_result`/`_did_not_fix`
  logging pattern as the catalog-dump guard, prefixed `reply_price_guard_*`.
  Verified: 5/5 "tell me about X" replies came back price-free with specs
  intact; 3/3 "what's the price of X" replies correctly still had the price.
  These two guards run sequentially (not `elif`), since a reply could in
  principle trip both at once.

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

**`REDIS_JOBS_ENABLED=true`** on the web service, drained by the GitHub
Actions workflow (see the Phase E entry above for why Actions rather than a
Render service). Verified: scheduled runs succeed roughly every few minutes
and a real queued job goes enqueue → drain → acked.

The pairing matters — the flag and a running drain must go together. With the
flag on and nothing consuming the stream, jobs pile up and intelligence
scoring/summaries silently stop updating rather than falling back to inline
(the inline fallback fires only when *enqueueing* fails, not when nothing
drains). If you ever disable the workflow, turn the flag off too.

**`REDIS_CACHE_ENABLED=true`** on the web service (enabled 2026-08-26; the
WhatsApp reply path also depends on it for the conversation-id cache) and deliberately left `false` in
`.env.render` for you to enable explicitly — unlike Phases B–D, caching has
enough staleness-risk surface (and this phase's own live-testing already
caught one real bug — see the Phase F entry above) that it's worth a
deliberate look before flipping it in production, rather than defaulting it
on. It's already `true` locally (`.env`) and fully verified there against
the real Supabase/Qdrant/OpenAI backends.
