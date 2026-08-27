-- ---------------------------------------------------------------------------
-- accessories — parts/accessories catalog, manually maintained (no machine
-- linkage yet by design; that comes in a later pass once the client has real
-- data to model the relationship against).
-- ---------------------------------------------------------------------------
create table if not exists accessories (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    category    text,
    description text,
    is_active   boolean     not null default true,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists accessories_category_idx on accessories (category) where is_active;

alter table accessories enable row level security;

-- Same deliberate exception as machines (003_rls_policies.sql): catalog data
-- is fine to expose read-only to anon/authenticated, only active rows.
drop policy if exists accessories_public_read on public.accessories;
create policy accessories_public_read
    on public.accessories
    for select
    to anon, authenticated
    using (is_active);

-- Writes to the catalog remain backend-only: no insert/update/delete policy
