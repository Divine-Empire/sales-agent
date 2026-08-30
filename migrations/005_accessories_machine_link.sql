-- ---------------------------------------------------------------------------
-- accessories — add per-machine linkage.
--
-- 004_accessories.sql deliberately deferred this ("no machine linkage yet by
-- design; that comes in a later pass once the client has real data to model
-- the relationship against"). The client now wants exactly that: each
-- accessory belongs to one specific machine, picked from the catalog when
-- adding it — not a flat, machine-agnostic list. Simple FK, not a join
-- table: one accessory fits one machine's parts list, matching how the
-- client actually thinks about it ("choose machine, then add accessories
-- for it"). If a part is ever genuinely shared across machines, the fix is
-- adding the same accessory again under the other machine, not a
-- many-to-many upgrade — deliberately kept simple.
-- ---------------------------------------------------------------------------
alter table accessories
    add column if not exists machine_id uuid references machines (id) on delete cascade;

create index if not exists accessories_machine_idx on accessories (machine_id) where is_active;
