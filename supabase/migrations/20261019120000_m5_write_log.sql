-- M5: audit log for writes to Garmin (requirements FR-17..19, design §8.3).
-- One row per write tool call. The idempotency key makes a retried call return the first
-- result instead of writing twice. `created_by_system` marks entities this system created;
-- only those may later be rescheduled, unscheduled, deleted or undone (FR-18).

create table garmin.write_log (
    id                 bigint generated always as identity primary key,
    account_id         uuid not null references garmin.accounts (id) on delete cascade,
    idempotency_key    text not null,
    tool               text not null,
    input              jsonb not null,
    preview            text,
    status             text not null default 'pending' check (status in ('pending', 'ok', 'failed')),
    garmin_response    jsonb,
    error              text,
    entity_kind        text,        -- weigh_in | blood_pressure | hydration | workout | scheduled_workout | activity | gear
    entity_id          text,
    entity_date        date,
    created_by_system  boolean not null default false,
    undo               jsonb,       -- what reverses this write (e.g. previous activity fields)
    undone_by          bigint references garmin.write_log (id),
    resync             jsonb,       -- work units queued / run after the write
    created_at         timestamptz not null default now(),
    finished_at        timestamptz,
    unique (account_id, idempotency_key)
);
create index write_log_entity on garmin.write_log (account_id, entity_kind, entity_id);
create index write_log_created on garmin.write_log (account_id, created_at desc);

alter table garmin.write_log enable row level security;
grant select on garmin.write_log to gds_reader;
create policy gds_reader_select on garmin.write_log for select to gds_reader using (true);
