-- 002_security_hardening.sql
-- Resolves Supabase Security Advisor findings against 001.
-- Idempotent: safe to re-run.

-- ---------------------------------------------------------------------------
-- ERROR: Security Definer View — public.current_leads
--
-- Postgres views default to SECURITY DEFINER semantics: they run with the
-- owner's privileges and bypass the querying role's RLS. current_leads joins
-- customer PII onto lead scores, so it is the single object that most needs to
-- respect RLS rather than sidestep it.
--
-- security_invoker makes the view execute as the caller. The service-role key
-- still bypasses RLS (that is what the backend uses); anon/authenticated are
-- now correctly constrained by the underlying tables' policies.
-- ---------------------------------------------------------------------------
alter view public.current_leads set (security_invoker = on);

-- ---------------------------------------------------------------------------
-- WARN: Function Search Path Mutable — public.set_updated_at
--
-- A SECURITY DEFINER-adjacent function without a pinned search_path can be
-- hijacked by a caller who creates a shadowing object in an earlier schema.
-- Pinning it closes that path.
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at() returns trigger
    language plpgsql
    set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- RLS posture
--
-- Supabase enabled RLS on every table in 001. No policies are defined, so
-- anon and authenticated can read nothing — which is correct: this backend is
-- the sole client and uses the service-role key, which bypasses RLS entirely.
--
-- The "RLS Enabled No Policy" advisor notices are therefore expected and are
-- the desired end state for the prototype.
--
-- This changes the moment a browser dashboard queries Supabase directly. At
-- that point every table below needs an explicit policy; until then, deny-all
-- is the safer default.
-- ---------------------------------------------------------------------------
alter table public.customers              enable row level security;
alter table public.conversations          enable row level security;
alter table public.messages               enable row level security;
alter table public.machines               enable row level security;
alter table public.machine_documents      enable row level security;
alter table public.lead_scores            enable row level security;
alter table public.conversation_summaries enable row level security;
alter table public.opt_out_list           enable row level security;
alter table public.handover_requests      enable row level security;
alter table public.ai_logs                enable row level security;
alter table public.reports                enable row level security;
