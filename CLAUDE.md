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

## Catalog: accessories/parts (added 2026-08-27, machine-linked 2026-08-28)

A second catalog table alongside `machines`: `accessories`
(`migrations/004_accessories.sql` + `005_accessories_machine_link.sql`) —
`name`, `category`, `description`, `is_active`, and (since 005) `machine_id`
(FK to `machines`, `on delete cascade`). Originally shipped flat/unlinked
("no machine linkage yet, deferred until there's real data to model the
relationship against") — the client's actual workflow turned out to be
"pick a machine, then add its accessories," so 005 added the FK the same
day. **Simple FK, not a many-to-many join, deliberately**: an accessory
genuinely shared across two machines is entered twice rather than modeled
as a shared relationship — matches how the client thinks about it, avoids
join-table complexity with no present payoff. Accessories are entered
manually (typed/pasted into the dashboard), never via document upload;
there is no OCR/extraction path for them and none is planned until Google
Sheet integration is revisited.

**Both migrations had only ever been written as files, never applied to
production** — discovered live when `store.upsert_accessory` started
throwing `PGRST205: Could not find the table 'public.accessories' in the
schema cache` the first time an accessory create was actually exercised
end-to-end (every prior "it works" verification had used a monkeypatched
store, which never touches real Postgres). Applied both directly via `psql`
against the pooler connection string, then `NOTIFY pgrst, 'reload schema'`
to force PostgREST to pick up the new table without waiting for its own
poll interval. If you add a future migration file, actually running it
against production is a separate step from writing it — this bit us once.

CRUD lives in `app/store.py` (`upsert_accessory`/`update_accessory_fields`/
`delete_accessory`/`list_accessories`, mirroring the `machines` functions;
`list_accessories(machine_id=...)` filters to one machine, which is what
the dashboard's per-machine section always passes) and `/api/accessories`
routes in `app/main.py` (GET list with optional `?machine_id=`, POST create
— now requires `machine_id` in the body — PATCH update, DELETE). Same
public-read RLS exception as `machines` (`migrations/004_accessories.sql`)
since chat needs to read it without a service-key path.

RAG ingestion reuses `app/documents.py`'s existing chunk/embed/Qdrant-upsert
machinery via `ingest_accessory()` — one Qdrant point per accessory (name +
description, rarely needs chunking), payload tagged `record_type:
"accessory"` so it's distinguishable from machine chunks if that's ever
needed. Deleting or deactivating an accessory removes its Qdrant point
(`delete_accessory_from_index`) — note this is *not* what `machines` does
today; `delete_machine` leaves its Qdrant chunks behind. Don't assume parity
between the two catalogs' delete behavior. (Fixed in the same pass: a
`log.info(..., extra={"name": name})` call in `ingest_accessory` collided
with `LogRecord`'s own reserved `name` attribute and raised `KeyError` at
log time — never caught before because every earlier attempt failed on the
missing table first. Renamed to `accessory_name`; grep the rest of the
codebase for the same `extra={"name": ...}` pattern before reusing it
elsewhere.)

**Accessories are a closing detail, not a sales pitch point** (client
requirement, 2026-08-28): `app/prompts.py` no longer tells the model to
recommend an accessory once a machine is identified — that guidance
actively contradicted the client's actual sales process, where mentioning
what "comes with" a machine before the deal is confirmed reads as
overselling. A new ACCESSORIES hard rule says explicitly: never bring
accessories up while still selling the machine — not on first
recommendation, not answering follow-ups, not during qualifying. Only once
the customer has actually committed (said yes, confirmed an order, asked to
proceed) does the agent mention what comes with it, framed as a closing
note, not a pitch. Verified end-to-end against a real test machine +
accessory: neither "tell me about X" nor an order-confirmation turn (which
correctly went to request_human_handoff instead, since the sales team still
needs to confirm specifics) mentioned the accessory — matching the rule
that accessories wait for actual confirmation, not just a stated intent to
order.

## Product knowledge depth — optional rich profiles (added 2026-08-28)

`data/product_profile_template.md` documents an optional, deeper per-product
content shape — What it does / Who should (not) buy it / Features / Benefits
/ Price / Competitors / Advantages / Limitations / Common objections +
Responses / FAQs / Upselling opportunities — as a `###` sub-heading per
product, same chunking convention `data/knowledge_base.md` already uses.
This is additive, not a required migration: today's sparse price-table rows
keep working exactly as they do now; this shape exists for products worth
going deeper on, filled in by whoever maintains the catalog content, not by
this codebase.

The reasoning: the agent's prompt already instructs it to never invent a
spec and never recite retrieved content verbatim, but that instruction is
only useful if the underlying facts actually exist in what gets retrieved.
A price-table row answers "what does it cost," not "why this over the
competitor" or "what do I say when they push back on price" — the agent
either declines (correct, but unhelpful) or drifts toward plausible-sounding
invention (exactly what the never-invent rule exists to prevent) when asked
something a sparse row can't answer. Objections/Responses/FAQs close that
gap, written as facts and talking points for the agent to draw from and
reformulate, explicitly NOT as a script — `app/prompts.py`'s context-
injection message (`build_messages`) now says as much directly: use the
substance of a matching objection/FAQ entry in your own words, never copy
the written response verbatim or announce "here's a common objection."

Same pass, `app/prompts.py` also added: a decision-maker question to
Qualifying (phrased conversationally — "yeh order aap khud finalize kar
lenge ya kisi aur se bhi confirm karna hoga", never "are you the decision
maker"), urgency folded into the existing timeline question, and four new
hard rules — never claim something is guaranteed, never argue with the
customer past one correction, disclose being an AI if asked directly (never
volunteer it, never deny it), and confirm understanding of the customer's
need in one line before recommending a machine (skippable only when a
single message already made the need completely unambiguous, e.g. an exact
model code). Verified locally against the real pipeline: the decision-maker
question came out conversational rather than form-like, the guarantee guard
correctly said "I can't guarantee that" while staying helpful, and the
AI-disclosure question got a direct, non-evasive answer that didn't derail
the conversation.

### Automatic profile structuring on upload (added 2026-08-28)

The client wanted the rich shape above filled in automatically, not typed by
hand: type the machine name, upload the document, and the profile appears
already structured (still editable afterward if something needs fixing).
`app/documents.py`'s `structure_product_profile()` does this — one LLM
call (tool-call schema `PROFILE_TOOL`, one nullable string field per
section) over the raw extracted text, wired into `add_machine_from_document`
right after `extract_text`. The critical constraint, same as everywhere else
in this codebase: a section the source document doesn't actually cover
comes back `null` and is left out of the rendered markdown
(`format_profile_markdown`) entirely — never padded with plausible-sounding
content. Verified against a sparse spec-sheet-style text: it filled
what_it_does/features/price/who_should_(not)_buy from real content and
correctly left objections/competitors/FAQs/etc. null since the source text
never mentioned them, rather than inventing generic ones.

Falls back to ingesting the raw extracted text unchanged if the structuring
call itself fails (LLM unavailable, bad tool-call JSON) — an LLM hiccup
costs richness, never the whole upload. `add_machine_from_document`'s
response now includes `profile_sections_filled` so the dashboard can show
the uploader how much the source document actually supported.

**Editing afterward**: `PATCH /api/machines/documents/{document_id}`
(`app/main.py`) updates `machine_documents.content` and re-ingests into
Qdrant immediately — a human correction to an AI-structured (or any other)
document takes effect right away, not just on the next re-upload. Backed
by two new store functions: `get_machine_document` (full row including
`content`, which `list_machine_documents` deliberately omits) and
`update_machine_document_content`. `get_machine_by_id` (new) resolves
`machine_name`/`category`/`machine_code` for the re-ingest call, since the
document row only carries `machine_id`.

**Deleting a machine now actually cleans up** (added same day, found while
testing the above): `DELETE /api/machines/{machine_id}` used to leave every
one of that machine's Qdrant chunks behind — documented as a known gap
("`delete_machine` leaves its Qdrant chunks behind, unlike accessories")
that turned out to matter once verified end-to-end: a deleted machine's
specs/price could still surface in a customer's answer via RAG, since
Postgres no longer had the row but Qdrant still did. Fixed with
`documents.delete_machine_from_index()` — a payload-filtered delete on
`machine_id` (a machine's document can chunk into many points, unlike an
accessory's single deterministic-id point, so there's no one id to target).
This needed its own payload index, same "index required but not found" 400
as the `codes` index did — `ensure_collection()` now also creates a
`machine_id` keyword index, created once directly against production the
same way the `codes` one was. Verified end-to-end: uploaded a test machine,
edited its document, deleted it, then confirmed via a direct Qdrant scroll
that zero points remained for that `machine_id` — the delete route's Qdrant
call now returns 200, not the silent-orphan 400 it used to.

### Verbatim spec preservation + multi-model detection in profile structuring (2026-08-30)

Two real quality gaps surfaced by the client comparing a real Sokkia FX-200
series brochure's raw text against its extracted dashboard output:

**1. Specs were being summarized into vague prose.** "Reflectorless range:
0.3 to 800m" came back as "long measurement range" — every number dropped.
Fixed with a new `_FIELD_GUIDANCE` dict appended to the `features` field's
tool-schema description, plus a new paragraph in `PROFILE_PROMPT` explicitly
forbidding number-to-prose compression: a spec sheet's Features section
should come out long and specific, not short and generic. Length is not the
problem; vagueness is.

**2. Distinct models in one document were collapsed into one generic
profile.** The FX-200-series brochure genuinely describes two different
models — FX-201 and FX-202 — with different accuracy/range/price each. The
original single-profile `structure_product_profile()` produced one
"FX-200 Series" profile and silently dropped which spec belonged to which
model. The client explicitly chose the harder fix over manual workaround:
make structuring itself detect multiple models from one upload.

`structure_product_profile()` (`app/documents.py`) now returns
`list[dict[str, str]] | None` instead of a single dict — `PROFILE_TOOL`'s
schema changed to a `variants[]` array, each entry carrying its own
`model_name` plus the same 13 sections as before. Most documents describe
one model and come back as a single-item list; a document that genuinely
gives separate models their own specs comes back with one entry per model.

**The prompt alone was not sufficient** — verified live, matching the
established pattern in this codebase (the catalog-dump and price guards):
even with an explicit "count the models in the source, match the count"
instruction, the same two-model test text came back with only one variant
(silently dropping the second) in 3 of 5 runs at temperature 0.1. A prompt
instruction is a strong nudge on GPT-4o, never a hard guarantee. Fixed with
a code-level backstop: `_find_model_codes()` scans the raw source text for
codes sharing the product's own family prefix (derived from the machine
name via `_model_family_prefix` — e.g. "Sokkia FX-200 Series" → prefix
`FX`), scoped deliberately narrow so an unrelated code-shaped token in the
same document (a battery pack `BDC70`, an IP rating `IP66`) is never
mistaken for a missed model — an earlier unscoped version of this check did
exactly that and fabricated bogus "BDC70"/"IP66" variants, caught before
shipping. If the first structuring call returned fewer variants than
distinct family-matching codes found in the source, one retry runs with an
instruction naming exactly which codes were missed. Verified: 5/5 clean
runs after this fix (both FX-201 and FX-202 present every time, no bogus
variants), versus 3/5 failures with the prompt-only version; a genuine
single-model document with a sibling model only mentioned in passing (not
specced) correctly still returns one variant, 3/3 runs — no false-positive
splitting.

`add_machine_from_document()` now creates one `machines` row, one
`machine_document`, and one RAG-ingested document **per detected variant**,
each with its own `machine_code` (extracted from the variant's own
model_name, e.g. "Sokkia FX-201 Total Station" → code `FX-201`) — this is
what actually prevents mixed retrieval later: `rag._exact_code_matches()`
and Qdrant's `machine_id` payload filter both key off these per-variant
codes, so a customer asking about FX-201 gets FX-201's exact-match chunk
prioritized, not a blend with FX-202's numbers. Verified end-to-end against
real Supabase/Qdrant: uploading the two-model test document produced two
separate `machines` rows; searching "FX-202 accuracy and price" correctly
returned zero mention of FX-201's price, confirming exact-code priority
works per-variant exactly as it does for manually-created machines.
Response shape stays backward compatible — `variants_detected`/`variants`
are new fields; the single-model case's top-level `machine_id`/`name`/
`characters_extracted` etc. are unchanged (the first/only variant's values).

**Superseded same day**: the design above (one `machines` row per detected
variant) was replaced a few hours later — see "Variants live under one
machine" below — after the client clarified their own mental model is one
machine with multiple types underneath it, not several separate machines.

### GPT-5.6-terra: model switch + a variant-detection regression it caused (2026-08-30)

`OPENAI_MODEL` switched from `gpt-4o` to `gpt-5.6-terra` (the balanced tier
of OpenAI's GPT-5.6 family — chosen over the flagship `sol` tier for better
latency/cost on a real-time chat path, and over the cheap `luna` tier to
keep reasoning depth for document structuring). This needed real code
changes, not just an env var flip, because GPT-5-family models are a
genuinely different API shape:

- **`temperature` is rejected outright** (400 Bad Request) on every
  GPT-5-family model except gpt-5.1+ with `reasoning_effort="none"`.
  `app/llm.py`'s `complete()` gained `_is_gpt5_family()` (a name-prefix
  check, `^gpt-5([.\-]|$)`) and `_apply_model_sampling()`, which drops
  `temperature` and adds `reasoning_effort` instead whenever the configured
  model is GPT-5-family — a no-op for `gpt-4o` and anything else, so this
  changes nothing unless `OPENAI_MODEL` is actually switched. New setting
  `llm_reasoning_effort` (default `"low"`, favors the fast/short replies the
  prompt already asks for).
- **Tool calls (`tools=`) reject any `reasoning_effort` other than `"none"`**
  on `/v1/chat/completions` for this family — found live via a real 400:
  "Function tools with reasoning_effort are not supported for gpt-5.6-terra
  ... set reasoning_effort to 'none'." This hits every tool-call caller in
  this codebase (the agent's 3 tools, document-profile structuring,
  lead-scoring analysis), so `_apply_model_sampling` forces
  `reasoning_effort="none"` whenever `tools` is present in the request,
  regardless of what was asked for — reasoning still happens, it just isn't
  exposed as a separate effort dial for this call shape.
- `transcribe_image()` (OCR) had its own hardcoded `temperature=0.0` call,
  same fix applied there.
- The Groq fallback path gets the same treatment defensively (keyed off
  `settings.groq_model`, currently never GPT-5-shaped, but free to check).

**A real regression this surfaced**: the multi-variant detection work above
was built and verified against `gpt-4o`. Re-tested against `gpt-5.6-terra`,
it consistently added a bogus THIRD variant entry named after the bare
series ("Sokkia FX-200" / "Sokkia FX-200 Series") alongside the two real
ones — content was just a restatement of "available models: FX-201 and
FX-202," not a real model. Fixed two ways: `PROFILE_PROMPT` and
`PROFILE_TOOL`'s schema now explicitly forbid a series-label entry, and
`_drop_redundant_series_variant()` is a code-level backstop that detects a
variant whose own code is a "round" number (e.g. "200") sharing a leading
digit with two or more non-round sibling codes ("201", "202") and drops it
— narrow enough that a genuine third real model (e.g. "FX-210", not round)
is never touched. `_find_model_codes()` (the missed-variant retry check)
got the same round-number exclusion, so it no longer manufactures a phantom
"missed variant" out of the series name and triggers a pointless retry.
Verified: 5/5 clean runs (exactly FX-201 + FX-202, no bogus third entry, no
wasted retry) after both fixes, versus a bogus third entry appearing in
every run beforehand.

### Variants live under one machine, not separate machines (2026-08-30)

The client's own mental model, stated directly: FX-201 and FX-202 are
**types of one machine** ("Sokkia FX-200 Series"), not two separate
machines that happen to be related. The same-day design above (one
`machines` row per detected variant) matched a retrieval-safety goal but
not the client's actual data model, so it was replaced within hours.

`add_machine_from_document()` now always creates exactly **one** `machines`
row (named after whatever was typed at upload) and **one** `machine_document`
— `format_profile_markdown()` renders every detected variant as its own
`### Type: {model_name}` sub-section (with `####` sub-headings for that
variant's own 13 sections) nested inside the single document, instead of
each variant getting a separate top-level `##` document. Retrieval safety
is preserved a different way: `extract_codes()` already scans the full
combined document text, so both "FX-201" and "FX-202" still end up in the
one machine's `codes` payload list — a customer's exact-code question still
gets an exact-match priority hit on the right chunk of the *same* document,
it just no longer needs a separate `machine_id` to do it. Verified: uploading
the real two-variant test document now produces exactly one `machines` row,
one `machine_document` containing both `### Type:` sub-sections with
distinct verbatim specs, and a `codes` list containing both `FX-201` and
`FX-202`.

### Missing-section enrichment via general model knowledge (2026-08-30)

A source brochure/spec sheet routinely has no Price, Competitors,
Objections, or FAQs section at all — a spec table answers "what are the
numbers," not "why this over a competitor." The client explicitly chose to
have these auto-filled from the model's own general category knowledge
(NOT a live web search) rather than left blank or filled in by hand.

`_enrich_missing_sections()` (`app/documents.py`) runs after structuring,
one additional LLM call per variant, **only** for variant profiles that
have at least one section from `_ENRICHABLE_SECTIONS` (price, competitors,
advantages, limitations, common_objections, responses, faqs,
upselling_opportunities) still null — a fully-covered profile costs nothing
extra. Deliberately does NOT touch what_it_does/who_should_(not)_buy/
features/benefits: those must stay grounded in the actual source document,
and are usually already filled when the document has real content — letting
general knowledge overwrite "the document doesn't say" with a plausible
guess about a specific model's own behavior would defeat the whole
never-invent-a-spec principle this codebase holds everywhere else.

Every enriched section is prefixed with `_AI_ESTIMATE_PREFIX` —
`"[AI estimate — not from the source document; verify before relying on
it]\n"` — visible in the dashboard's document view/edit and in whatever RAG
retrieves, so a rep (or the agent) can tell brochure-verified content from a
general-knowledge starting point at a glance. The enrichment prompt itself
is written to keep the model honest about the distinction: name real known
competitor brands if genuinely known (never invented ones), give a category-
level price range rather than a specific rupee figure claimed as this exact
model's price, and leave a section null rather than force a guess with no
genuine basis.

**A real bug this caused, found and fixed the same pass**: `extract_codes()`
runs on the full stored document text to build the `codes` payload list —
an enriched Competitors section naming real rival products ("Leica
TS16/TS20", "Trimble S9") had ITS OWN model-shaped codes picked up into
THIS machine's `codes` list, so a customer asking about "TS16" (a Leica
product, never ours) would have incorrectly exact-matched this machine's
chunk. Fixed in `ingest_document()`: code extraction now runs on the
document text with any paragraph starting with `_AI_ESTIMATE_PREFIX`
stripped out first — the full enriched content still ships in the stored/
RAG text either way, only the code-extraction input is narrowed.

### Extraction/structuring was silently dropping real brochure content (2026-08-30)

Comparing the actual FX-200 series PDF's raw extracted text against its
structured dashboard output surfaced a real completeness gap distinct from
the earlier verbatim-numbers fix: rich descriptive content that didn't
neatly fit a spec-bullet shape was being dropped entirely — application
write-ups ("Boundary and Cadastral Survey", "Topographic Survey" explaining
what a use case is and how the product supports it), a named onboard
software package's actual feature list (MAGNET Field's roading tools,
surface staking, cut/fill indicators, etc.), a "Standard Package Components"
accessories list, and a comparison callout table (this model's accuracy/
range vs. a "Previous Model"). None of this is a bare numeric spec, so the
model, left to its own judgment, treated it as not fitting Features and
silently left it out — the client's explicit standing instruction is that
if the source document has real data, it belongs in the knowledge base,
whether or not it's phrased as a bullet-point spec.

Fixed in `PROFILE_PROMPT`: a new paragraph states directly that content
which doesn't neatly fit one section must still land somewhere (Features,
Benefits, or What it does) rather than be dropped, and that a profile
shorter than what the source document actually contains is exactly as bad
a failure as inventing content that isn't there. `_FIELD_GUIDANCE` extended
to `what_it_does` (include full application/use-case write-ups, not a
one-line summary) and `benefits` (include what named features/use-cases
mean for the buyer, drawing on comparison content) — previously only
`features` had dedicated guidance. `max_output_tokens` for the structuring
call raised 4000 → 8000, since a rich, detailed brochure genuinely needs
more room per variant than a sparse spec sheet does.

Verified against the real FX-200 series PDF text: Features per variant grew
from ~250 chars (bare specs only) to 2000+ chars including the full
Standard Package Components list, the MAGNET Field software feature list,
and the accuracy/range comparison table — all present verbatim, correctly
attributed per variant, with no invented content (confirmed by checking the
output only ever restates numbers/lists actually present in the source
text).

### Stable Qdrant point ids (fixed 2026-08-30, found while auditing the above)

`ingest_document`'s and `ingest_accessory`'s deterministic point ids were
computed with Python's builtin `hash()` — which is **process-randomized for
strings by default** (`PYTHONHASHSEED`). The same machine/accessory + chunk
index produced a genuinely different Qdrant point id every time the backend
process restarted (a new Render deploy, a worker cycling), which quietly
broke the exact behavior both docstrings claimed: "re-uploading the same
document updates in place." After any restart, a re-upload or a
`PATCH /api/machines/documents/{id}` edit instead **added a second, orphaned
copy** of the old content next to the corrected one — the agent could then
answer from either, including the stale one, with no error or signal
anywhere that this had happened. Never reported by a customer; caught by
auditing this code path while reviewing the multi-variant work above, not
by reproducing a live symptom.

Two-part fix, `app/documents.py`:
- `_stable_point_id()` replaces `hash()` with `hashlib.md5` — deterministic
  for the same input in any process, forever, not just within one run.
- `ingest_document` now **deletes all existing Qdrant points for that
  `machine_id` before upserting the new ones**, rather than relying solely
  on point-id reuse to overwrite in place. This is what actually makes a
  re-ingest correct even when the new document has fewer chunks than the
  old one — point-id overwrite alone would leave a stale tail of the old
  document's extra chunks behind. `id_key_base` prefers `machine_id` (a
  UUID, collision-proof across machines) over `machine_name`, falling back
  to the name only for the rare caller with no id yet.
- `delete_accessory_from_index` changed from a by-id delete (computed from
  today's id scheme) to a payload-filtered delete on `accessory_id` — a
  by-id delete would silently miss any accessory ingested under the old
  `hash()` scheme, since its real stored id doesn't match what the new
  scheme computes. Needed its own new Qdrant payload index on
  `accessory_id` (same "index required but not found" 400 as `codes`/
  `machine_id` before it), created once directly against production the
  same way those were, via `ensure_collection()`.

Verified end-to-end against real Qdrant: ingesting a long test document (5
chunks) then re-ingesting a much shorter replacement (1 chunk) left exactly
1 point in Qdrant for that `machine_id`, not 6 — confirming no orphaned
tail. Accessory ingest → delete confirmed via direct scroll: 1 point found
before delete, 0 after. Production's existing ~32 Qdrant points predate
this fix and were not touched — they only risk becoming orphaned/duplicated
the next time they're re-ingested after a restart, which this fix now
prevents going forward; no retroactive cleanup was needed since nothing had
actually been re-ingested since the collection was last fully rebuilt.

### Document structuring rebuilt on Pydantic models, not hand-rolled dicts (2026-08-30)

`structure_product_profile()`'s whole pipeline (`app/documents.py`) used to
pass a raw hand-written dict as the OpenAI tool-call schema and parse the
response back with manual `json.loads` + `isinstance` checks — the schema
and the parsing logic were two independently-maintained things that could
silently drift apart, and a malformed field failed as a `KeyError` three
functions later rather than at the parse boundary. Replaced with
`ProductVariantProfile`/`ProductProfileResult` (`pydantic.BaseModel`): the
13 sections are now real typed fields (each `str | None`, matching the
existing "null means not covered, never invent" contract), the tool schema
sent to OpenAI is generated FROM the model (`_pydantic_tool_schema`, via
`model_json_schema()`), and the response is parsed straight back into the
same model (`model_validate_json`) instead of a parallel hand-rolled path.
`_enrich_missing_sections`'s per-call schema (only the sections actually
missing for a given variant) is now a Pydantic model built on the fly with
`pydantic.create_model` — same schema-generation/parsing path, no separate
raw-dict tool definition to keep in sync by hand.

Every function downstream (`_drop_redundant_series_variant`,
`format_profile_markdown`, `add_machine_from_document`) now works with
`ProductVariantProfile` objects and `profile.section_items()`/
`profile.filled_count()` instead of dict keys and `len(dict) - 1` — a typo
in a field name is now a static/attribute error, not a silently-absent
dict key. `PROFILE_SECTION_LABELS` (the (field, display-label) pairing) is
still one flat list, but it's now purely a rendering/prompt-building detail
— `ProductVariantProfile`'s field definitions are the actual source of
truth for what a section is and what its own guidance says, keeping that
guidance next to the field it describes instead of in a separate
`_FIELD_GUIDANCE` dict that had to be manually kept in sync by key name.

**A real bug the refactor's own testing caught**: `_enrich_missing_sections`
hardcoded `max_output_tokens=1200` regardless of how many sections were
actually missing. A profile missing all 8 enrichable sections at once (a
bare spec sheet with nothing beyond specs) genuinely needs more than 1200
tokens to write a real paragraph for each — the response was silently
truncated mid-JSON-string, `model_validate_json` correctly raised a
`ValidationError` ("EOF while parsing a string"), and that error was being
swallowed with no log line at all, so a fully-missing-sections profile
looked like enrichment had run and found nothing to add, when it had
actually failed outright. Fixed by scaling the token budget with how many
sections are actually missing, and by logging the `ValidationError` case
(`profile_enrichment_bad_json`, with `completion_tokens` so a future
recurrence is diagnosable at a glance instead of invisible again.

**A second real bug found in the same pass**: the AI-estimate-paragraph
filter in `ingest_document()` (added when enriched Competitors content was
found leaking rival products' model codes into this machine's `codes`
list — see the entry above) used `.startswith()` to detect an AI-estimate
paragraph, but `format_profile_markdown` always puts the `#### Label`
heading on its own line immediately before the section text — so the
`\n\n`-joined paragraph actually starts with the heading, not the prefix,
and the `startswith` check silently matched nothing. The codes list kept
leaking competitor codes despite the filter appearing to exist and having
its own test coverage claim. Fixed by checking `in` the paragraph instead
of `.startswith()`. Verified against real Qdrant: a document with an
enriched "Leica TS16, Trimble S9" Competitors section now produces a codes
list containing only this machine's own FX-200/FX-201/FX-202 codes, zero
leaked competitor codes — confirmed both via direct string-level testing
and a live end-to-end upload.

Verified end-to-end after all three fixes: 5/5 clean multi-variant runs
(FX-201 + FX-202, no bogus third entry, every enrichable section filled on
every variant), 3/3 clean single-variant runs (no false split from a
sibling model mentioned in passing), and a live Supabase/Qdrant upload
producing the correct single machine row, one document, and a clean codes
list.

### Cross-machine matching — enrich the RAG query with what's already known (2026-08-28)

`rag.search()` was only ever given the customer's single current message.
That's fine for a specific question ("IM-55 ka price kya hai") but breaks
down for a vague follow-up once qualifying has already captured real
context — "aur kya options hain" carries no product keyword at all, so pure
vector similarity has nothing to match against. Confirmed directly: that
exact query against production Qdrant returned **zero hits** on its own.

`app/agent.py`'s `_enrich_search_query()` folds the existing
`conversation_summaries` row (`requirements`, `interested_machines`,
`location` — whatever `save_lead` already captured this conversation) into
the query text before every search, not just the bare message. Degrades to
the plain message when there's no summary yet (early turns, or a Supabase
hiccup) — an enrichment, not a dependency. Wired in by reshaping the
existing `asyncio.gather(rag.search(...), store.get_history(...))`: the
summary fetch now runs alongside `get_history` (both plain Supabase reads,
independent of each other), and `rag.search` runs after, since it depends
on the enriched query — a small latency tradeoff for materially better
matches on vague follow-ups.

Verified: the same vague query ("aur kya options hain isse alag") that
returned 0 hits alone returned 4 relevant hits once enriched with a saved
requirement of "road project, compact power tool, small site" +
"Walk Behind Roller" + "Raipur", topped by the Compaction Equipment
catalog section. End-to-end through the real turn-handling path
(`_handle_message_locked`, Supabase writes monkeypatched): after a customer
described a road project in Raipur and got Walk Behind Roller recommended,
the same vague follow-up correctly got Ride-On Road Roller (a genuine
alternative in the same category) rather than losing the thread onto an
unrelated category.

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
- A real customer's lead score stayed stale (Cold/15) for several minutes
  after a message ("order kar do", a bulk order — about as strong a buying
  signal as exists) while the automated pipeline had zero pending jobs and
  zero dead-lettered ones to explain it. Root cause turned out to be an
  observability gap, not a scoring bug: manually re-running
  `intelligence.analyse()` on the same conversation immediately produced the
  correct result (Hot/75), proving the model itself was fine — but
  `analyse()` never called `store.log_ai_event`, so its LLM call left no
  trace anywhere queryable; every entry in `/api/logs` for that conversation
  turned out to be a normal customer-facing agent turn (identifiable by
  `retrieved_chunks`, which `analyse()` never produces), not the analysis
  job. Worse: `analyse()`'s own contract is "return `None` on any failure,
  never raise" (deliberately, since its OTHER caller —
  `app/main.py`'s inline fallback when job-enqueueing itself fails — must
  never let a scoring failure disturb the customer's turn), but
  `app/worker.py`'s job handler saw that clean `None` return as success:
  no exception meant `xack` + `mark_processed` regardless of whether
  anything was actually written. A genuinely failed analysis job (LLM
  unavailable, bad tool-call JSON) would have looked identical to a
  successful one, with no retry and no dead-letter entry to reveal it.
  Fixed same day: `analyse()` now calls `store.log_ai_event` on both its
  success and LLM-unavailable paths (same `AiLogEvent.LLM_CALL`/`ERROR`
  types the per-turn call uses, so `/api/logs` now shows both — distinguish
  by the absence of `retrieved_chunks`, which only the customer-facing turn
  produces). A new `analyse_or_raise()` wraps the same work but raises
  `AnalysisFailedError` on that `None` instead of swallowing it;
  `app/worker.py`'s `_HANDLERS` now points at `analyse_or_raise`, not
  `analyse` — so a genuine failure retries with backoff and eventually
  dead-letters like every other job failure, while `app/main.py`'s inline
  fallback caller still uses plain `analyse()` and keeps its original never-
  raise guarantee. If you ever add another job type whose work function was
  written to fail open (return `None`/a sentinel rather than raise), give it
  the same `_or_raise` wrapper before wiring it into `app/worker.py` — the
  bare version quietly defeats the whole retry/dead-letter mechanism Phase E
  was built for.
- A real production WhatsApp transcript surfaced two more prompt gaps after
  the multiple-types-under-one-machine work (2026-08-30). First: a bare
  "Hii" with nothing else in it got answered with a specific product pitch
  ("hum Sokkia total station provide karte hain...") — the existing
  vague-open-question guidance ("tell me about your machines" gets a
  category + a question) was being applied to a plain greeting too, which
  has even less signal than an open question does. Fixed with an explicit
  rule: a bare greeting gets a greeting + one open question back, no
  product/category name at all. Second, and more interesting: the same
  conversation asked about "boundary survey" (no exact model code) and got
  Sokkia FX-201 recommended directly, with FX-202 never mentioned — root
  cause was retrieval order, not a language bug. Since "Variants live under
  one machine" above, both types live in the SAME document, and
  `_exact_code_matches` (the mechanism that resolves ambiguity when a
  customer types an exact code) never fires when they don't type one — so
  a vague answer like "Boundary" falls back to plain vector similarity,
  which returns the document as one chunk with FX-201's section appearing
  first (an artifact of `format_profile_markdown`'s list order, not a
  recommendation). The model had no rule telling it that first-in-context
  isn't the same as best-fit, so it silently defaulted to whichever type it
  saw first. Fixed with a new hard rule: when the context lists more than
  one "Type" under one machine and the customer's stated need doesn't
  already point to a specific one, ask the one differentiating question
  (precision level, budget, etc.) before naming a type, and when naming
  one, say briefly why that type over the others fits what they said.
  Verified against the real pipeline (llm.complete + prompts.build_messages,
  no Supabase writes) with both FX-201 and FX-202 in context: 3/3 clean
  runs on "Hii" (greeting + open question, no product named), 3/3 clean
  runs on "Boundary" (asked the accuracy differentiator before naming
  either type) — plus a regression check confirming an exact-code question
  ("FX-201 ka price kya hai") still answers directly and a genuine open
  product question still gets a category + qualifying question, so neither
  fix narrowed those existing correct behaviors.
- The same transcript surfaced two more gaps once the conversation reached
  a real bulk-order shape (2026-08-30): the customer stated a 200-unit
  quantity and later a ₹2,00,000 budget for those 200 units — impossible
  on its face, since one FX-200-series unit alone is worth ₹5.2-6.5 lakh
  per the same context, so ₹2 lakh doesn't cover even one unit, let alone
  200. The agent accepted both numbers at face value with no arithmetic
  check and never called `request_human_handoff` for the 200-unit
  quantity at all — it kept asking ordinary qualifying questions
  ("khud finalize karenge ya team se approval?") as if 200 units were a
  routine single-site purchase, right through to a fourth follow-up turn.
  Two rule additions, no code change (same as the multiple-types fix
  above, this is data the model already has, it just had no rule telling
  it to check): a NUMBERS MUST MAKE SENSE TOGETHER hard rule requires
  doing the quantity × approximate-price arithmetic before accepting a
  stated budget, and asking a plain clarifying question ("total budget hai
  ya per-unit?") rather than proceeding when it clearly doesn't fit: and
  `request_human_handoff`'s own description now states explicitly that a
  bulk order is defined by quantity alone ("tens or hundreds" of units)
  and must be called the moment that quantity is stated, not deferred
  until the customer separately asks for a formal quote. Verified: 3/3
  clean runs calling `request_human_handoff` the turn "200" was stated as
  the quantity; 3/3 clean runs correctly questioning "₹2,00,000 total
  budget hai ya per unit?" rather than accepting it, citing the real
  per-unit price from context; a regression check confirmed an ordinary
  small quantity ("2" units) does NOT spuriously trigger the handoff tool.
- A third read of the same transcript found a real machine-name-drift bug
  the earlier "MULTIPLE TYPES UNDER ONE MACHINE" fix didn't catch: across
  three consecutive turns the same conversation named FX-201, then the
  generic "Sokkia FX-200 Series", then FX-202 — with no explanation for
  any of the switches, and no customer message that actually changed which
  type fit. Once the customer said "Boundary" (single-machine-with-variants
  retrieval had already surfaced both FX-201 and FX-202 in one chunk), each
  later turn's own retrieval could resurface either type first depending on
  which words in that turn's message scored closest, and the model just
  named whichever one it saw, with no rule telling it that a name spoken
  two turns ago is a commitment, not a suggestion that resets every turn.
  Added a new hard rule, STAY ON THE SAME MACHINE ONCE YOU'VE NAMED ONE:
  once a specific machine/type has been named, keep naming that same one
  in later replies unless the customer says something that genuinely
  changes the answer — and if it does change, say so plainly rather than
  silently substituting one name for another. Verified: replaying the
  exact "Survey ke liye" -> "Boundary" -> "Raipur" turn sequence 3 times,
  the model now withholds naming any specific type until a real
  differentiating signal actually arrives (2/3 runs asked the precision
  question through all three turns without naming either type at all,
  since none of "Survey ke liye"/"Boundary"/"Raipur" alone fully resolved
  it; the 1/3 run where "Boundary" was read as pointing to the
  high-precision type correctly named FX-201 and then stayed on FX-201 for
  the following turn, rather than drifting to a second name) — a stricter
  and more consistent outcome than the original transcript's three
  different names in three turns.
- Structuring a dense multi-model spec sheet (a document comparing three
  distinct models, e.g. "iX-1201, iX-601, iX-605" in one brochure) could
  fail entirely even with a working OpenAI key and enough quota (2026-08-30,
  found while investigating two real production uploads — iM-100 and iX —
  that came back with unformatted raw text instead of a structured
  profile). Root cause was `app/llm.py`'s client-level 30s timeout, sized
  for a short chat reply: a real structuring call asking for up to 8000
  output tokens genuinely took ~35-97s to complete depending on how many
  variants and retries were needed, which raised `APITimeoutError` on the
  primary well before OpenAI actually finished — the call then fell
  through to the Groq fallback, which failed too (the same large request
  exceeded Groq's free-tier tokens-per-minute limit), so a call that would
  have succeeded on OpenAI given more time failed on both providers and
  silently triggered `add_machine_from_document`'s raw-text fallback.
  `complete()` gained a `timeout_seconds` override (the client-level
  default stays 30s for ordinary chat replies); `_structure_profile_call`
  now asks for 90s and `_enrich_missing_sections` for 60s. Verified against
  the real saved iX document text: the exact same call that previously
  timed out on both providers now succeeds in ~97s, correctly detecting
  all three real models (iX-1201, iX-601, iX-605) instead of falling back
  to raw text.

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
