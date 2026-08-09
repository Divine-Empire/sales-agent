-- 003_rls_policies.sql
-- Explicit RLS policies for every table. Idempotent: safe to re-run.
--
-- WHY THIS EXISTS
-- ---------------
-- After 001/002 every table had RLS enabled with zero policies. That is
-- already deny-all for anon/authenticated, and service_role bypasses RLS
-- entirely — so the effective posture was correct but *accidental*: it read as
-- "we forgot to write policies" rather than "we decided nobody but the backend
-- may touch this".
--
-- This migration makes the decision explicit and auditable in SQL. It also
-- hardens against a real failure mode: if someone later enables a browser
-- client, adds a policy to one table, and assumes the rest are covered, an
-- accidental grant is far easier to spot next to a deliberate deny.
--
-- THE MODEL
-- ---------
--   service_role  → bypasses RLS by design. This backend is the sole writer.
--                   Never granted a policy here; it does not need one.
--   anon          → the publishable key, safe to ship in a browser. Denied
--                   everywhere except the product catalog.
--   authenticated → no end-user auth exists in this system today. Denied,
--                   pending a real dashboard with real user identities.
--
-- Every table below holds customer PII, commercial data, or telemetry. The
-- default is deny; each exception is argued individually.

-- ---------------------------------------------------------------------------
-- Deny-all for anon + authenticated on every sensitive table.
--
-- A policy with `using (false)` is the explicit form of "no row is ever
-- visible". It cannot be satisfied, so SELECT/INSERT/UPDATE/DELETE all return
-- nothing / are rejected for these roles.
-- ---------------------------------------------------------------------------

do $$
declare
    t text;
    sensitive_tables text[] := array[
        'customers',
        'conversations',
        'messages',
        'machine_documents',
        'lead_scores',
        'conversation_summaries',
        'opt_out_list',
        'handover_requests',
        'ai_logs',
        'reports'
    ];
begin
    foreach t in array sensitive_tables loop
        execute format('drop policy if exists %I on public.%I', 'deny_anon_' || t, t);
        execute format(
            'create policy %I on public.%I as restrictive for all to anon, authenticated using (false) with check (false)',
            'deny_anon_' || t, t
        );
    end loop;
end;
$$;

-- ---------------------------------------------------------------------------
-- machines — the one deliberate exception.
--
-- Product catalog data: model codes, specs, applications, price *ranges*.
-- This is marketing material the client publishes anyway, and a future
-- storefront or public catalog page is a plausible consumer.
--
-- Read-only, and only rows flagged active — so a machine can be withdrawn
-- from public view by clearing is_active without deleting history.
--
-- Note what is NOT exposed: machine_documents stays denied above. Internal
-- manuals and spec sheets are not public just because the catalog is.
-- ---------------------------------------------------------------------------

drop policy if exists machines_public_read on public.machines;
create policy machines_public_read
    on public.machines
    for select
    to anon, authenticated
    using (is_active);

-- Writes to the catalog remain backend-only: no insert/update/delete policy
-- exists for anon or authenticated, so those are denied by default.

-- ---------------------------------------------------------------------------
-- Verification
--
-- Expected after this migration:
--   every table  → rls = true
--   10 tables    → 1 restrictive deny policy
--   machines     → 1 permissive select policy
--
--   select tablename, policyname, permissive, roles, cmd
--   from pg_policies where schemaname = 'public' order by tablename;
-- ---------------------------------------------------------------------------
