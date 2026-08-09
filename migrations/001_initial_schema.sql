-- 001_initial_schema.sql
-- AI Sales Agent — full schema (BRD §17).
-- Idempotent: safe to re-run. Apply via the Supabase SQL editor or psql.
--
-- Access is server-side only via the service-role key, so RLS is left off.
-- This MUST change before any browser client talks to Supabase directly.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- customers — one row per person per channel
-- ---------------------------------------------------------------------------
create table if not exists customers (
    id                 uuid primary key default gen_random_uuid(),
    channel            text        not null,
    channel_user_id    text        not null,
    name               text,
    company_name       text,
    location           text,
    preferred_language text        not null default 'en',
    phone              text,
    email              text,
    -- denormalised from opt_out_list: checked before every outbound message,
    -- and that check should not be a join
    is_opted_out       boolean     not null default false,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    constraint customers_channel_user_unique unique (channel, channel_user_id),
    constraint customers_channel_check check (channel in ('telegram', 'whatsapp'))
);

-- ---------------------------------------------------------------------------
-- conversations — a thread, keyed by {channel}:{user_id}
-- ---------------------------------------------------------------------------
create table if not exists conversations (
    id              uuid primary key default gen_random_uuid(),
    conversation_id text        not null unique,
    customer_id     uuid        references customers (id) on delete cascade,
    channel         text        not null,
    status          text        not null default 'active',
    started_at      timestamptz not null default now(),
    last_message_at timestamptz not null default now(),
    constraint conversations_status_check
        check (status in ('active', 'handed_over', 'closed', 'opted_out'))
);

create index if not exists conversations_status_recent_idx
    on conversations (status, last_message_at desc);
create index if not exists conversations_customer_idx
    on conversations (customer_id);

-- ---------------------------------------------------------------------------
-- messages — the agent's memory; last 20 rows form the LLM history window
-- ---------------------------------------------------------------------------
create table if not exists messages (
    id              bigint generated always as identity primary key,
    conversation_id text        not null,
    role            text        not null,
    content         text        not null,
    created_at      timestamptz not null default now(),
    constraint messages_role_check check (role in ('user', 'assistant', 'system', 'tool'))
);

-- hottest query in the system: the history read
create index if not exists messages_conversation_recent_idx
    on messages (conversation_id, created_at desc);

-- ---------------------------------------------------------------------------
-- machines — product catalog, source of record for recommendation (BRD §6, §7)
-- ---------------------------------------------------------------------------
create table if not exists machines (
    id             uuid primary key default gen_random_uuid(),
    machine_code   text not null unique,
    name           text not null,
    category       text not null,
    description    text,
    -- shape varies by machine category, so jsonb rather than sparse columns
    specifications jsonb       not null default '{}'::jsonb,
    applications   text[]      not null default '{}',
    industries     text[]      not null default '{}',
    -- ranges only; formal pricing is always a human handoff
    price_range    text,
    lead_time      text,
    is_active      boolean     not null default true,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

create index if not exists machines_category_idx on machines (category) where is_active;

-- ---------------------------------------------------------------------------
-- machine_documents — source docs behind the RAG index (BRD §5)
-- ---------------------------------------------------------------------------
create table if not exists machine_documents (
    id         uuid primary key default gen_random_uuid(),
    machine_id uuid        references machines (id) on delete cascade,
    doc_type   text        not null,
    title      text,
    source_url text,
    -- extracted text kept here so re-embedding never re-parses the source PDFs
    content    text        not null,
    indexed_at timestamptz,
    created_at timestamptz not null default now(),
    constraint machine_documents_type_check check (
        doc_type in ('brochure', 'manual', 'spec_sheet', 'catalog', 'faq', 'price_sheet')
    )
);

create index if not exists machine_documents_machine_idx on machine_documents (machine_id);
create index if not exists machine_documents_pending_idx
    on machine_documents (indexed_at) where indexed_at is null;

-- ---------------------------------------------------------------------------
-- lead_scores — append-only, one row per scoring event (BRD §9, §11)
-- ---------------------------------------------------------------------------
create table if not exists lead_scores (
    id              bigint generated always as identity primary key,
    conversation_id text        not null,
    customer_id     uuid        references customers (id) on delete cascade,
    score           integer     not null,
    category        text        not null,
    intent          text,
    -- per-factor breakdown: "why is this hot?" is the first question a rep asks
    factors         jsonb       not null default '{}'::jsonb,
    confidence      numeric(3, 2),
    scored_at       timestamptz not null default now(),
    constraint lead_scores_score_range check (score between 0 and 100),
    constraint lead_scores_category_check
        check (category in ('hot', 'warm', 'cold', 'not_interested'))
);

-- current score per conversation + ranking
create index if not exists lead_scores_conversation_recent_idx
    on lead_scores (conversation_id, scored_at desc);
create index if not exists lead_scores_ranking_idx
    on lead_scores (category, score desc, scored_at desc);

-- ---------------------------------------------------------------------------
-- conversation_summaries — BRD §14 field set, one current row per conversation
-- ---------------------------------------------------------------------------
create table if not exists conversation_summaries (
    id                  uuid primary key default gen_random_uuid(),
    conversation_id     text not null unique,
    customer_id         uuid references customers (id) on delete cascade,
    customer_name       text,
    company_name        text,
    location            text,
    preferred_language  text,
    interested_machines text[] not null default '{}',
    requirements        text,
    budget              text,
    timeline            text,
    -- mirrored from the latest lead_scores row so a dashboard row is one read
    lead_score          integer,
    lead_category       text,
    customer_intent     text,
    summary             text,
    next_action         text,
    handover_status     text        not null default 'none',
    ai_confidence       numeric(3, 2),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

create index if not exists conversation_summaries_category_idx
    on conversation_summaries (lead_category, lead_score desc);

-- ---------------------------------------------------------------------------
-- opt_out_list — BRD §13, compliance-critical
-- ---------------------------------------------------------------------------
create table if not exists opt_out_list (
    id              uuid primary key default gen_random_uuid(),
    channel         text        not null,
    channel_user_id text        not null,
    -- nullable: an opt-out can arrive before a customer row exists
    customer_id     uuid        references customers (id) on delete set null,
    conversation_id text,
    reason          text,
    opted_out_at    timestamptz not null default now(),
    constraint opt_out_channel_user_unique unique (channel, channel_user_id)
);

-- ---------------------------------------------------------------------------
-- handover_requests — BRD §12, backs the dashboard handover queue
-- ---------------------------------------------------------------------------
create table if not exists handover_requests (
    id              uuid primary key default gen_random_uuid(),
    conversation_id text        not null,
    customer_id     uuid        references customers (id) on delete cascade,
    reason          text        not null,
    -- the point of this table: a handoff without history moves work, not reduces it
    context         text,
    status          text        not null default 'pending',
    notified_at     timestamptz not null default now(),
    resolved_at     timestamptz,
    constraint handover_reason_check check (
        reason in ('formal_quote', 'price_negotiation', 'bulk_order',
                   'customer_request', 'low_confidence', 'other')
    ),
    constraint handover_status_check
        check (status in ('pending', 'acknowledged', 'resolved'))
);

create index if not exists handover_queue_idx
    on handover_requests (status, notified_at desc);

-- ---------------------------------------------------------------------------
-- ai_logs — per-turn telemetry. Highest-volume table; needs a retention
-- policy in production.
-- ---------------------------------------------------------------------------
create table if not exists ai_logs (
    id                bigint generated always as identity primary key,
    conversation_id   text        not null,
    event_type        text        not null,
    model             text,
    prompt_tokens     integer,
    completion_tokens integer,
    latency_ms        integer,
    -- answers "why did it say that?", otherwise unanswerable after the fact
    retrieved_chunks  jsonb,
    payload           jsonb,
    created_at        timestamptz not null default now(),
    constraint ai_logs_event_check check (
        event_type in ('llm_call', 'rag_search', 'tool_call', 'fallback', 'error')
    )
);

create index if not exists ai_logs_conversation_recent_idx
    on ai_logs (conversation_id, created_at desc);

-- ---------------------------------------------------------------------------
-- reports — stored aggregates (BRD §15), so history stays stable as
-- conversations continue to evolve
-- ---------------------------------------------------------------------------
create table if not exists reports (
    id           uuid primary key default gen_random_uuid(),
    report_type  text        not null,
    period_start date        not null,
    period_end   date        not null,
    metrics      jsonb       not null default '{}'::jsonb,
    generated_at timestamptz not null default now(),
    constraint reports_type_check check (report_type in ('daily', 'weekly', 'monthly')),
    constraint reports_period_unique unique (report_type, period_start)
);

-- ---------------------------------------------------------------------------
-- updated_at maintenance
-- ---------------------------------------------------------------------------
create or replace function set_updated_at() returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists customers_updated_at on customers;
create trigger customers_updated_at before update on customers
    for each row execute function set_updated_at();

drop trigger if exists machines_updated_at on machines;
create trigger machines_updated_at before update on machines
    for each row execute function set_updated_at();

drop trigger if exists conversation_summaries_updated_at on conversation_summaries;
create trigger conversation_summaries_updated_at before update on conversation_summaries
    for each row execute function set_updated_at();

-- ---------------------------------------------------------------------------
-- current_leads — latest score per conversation. Backs ranking (BRD §11):
-- "top 20 of 100" is a query, not a batch job.
-- ---------------------------------------------------------------------------
create or replace view current_leads as
select distinct on (ls.conversation_id)
    ls.conversation_id,
    ls.customer_id,
    ls.score,
    ls.category,
    ls.intent,
    ls.factors,
    ls.confidence,
    ls.scored_at,
    c.name         as customer_name,
    c.company_name,
    c.location,
    c.channel,
    conv.status    as conversation_status,
    conv.last_message_at
from lead_scores ls
left join customers c on c.id = ls.customer_id
left join conversations conv on conv.conversation_id = ls.conversation_id
order by ls.conversation_id, ls.scored_at desc;
