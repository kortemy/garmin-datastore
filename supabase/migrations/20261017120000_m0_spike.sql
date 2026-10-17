-- M0 spike schema: accounts, encrypted Garmin tokens, token audit, and spike logs.
-- The `garmin` schema is not exposed through the Supabase REST API.

create extension if not exists pgcrypto with schema extensions;
create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;

create schema if not exists garmin;
revoke all on schema garmin from public, anon, authenticated;

create table garmin.accounts (
    id          uuid primary key default extensions.gen_random_uuid(),
    label       text not null unique,
    status      text not null default 'active' check (status in ('active', 'needs_reauth', 'cooldown')),
    created_at  timestamptz not null default now()
);

create table garmin.garmin_tokens (
    account_id         uuid primary key references garmin.accounts (id) on delete cascade,
    ciphertext         bytea not null,
    nonce              bytea not null,
    access_expires_at  timestamptz,
    version            integer not null default 1,
    updated_at         timestamptz not null default now()
);

-- Every token write: login, library auto-refresh, forced refresh. Answers "do refresh tokens rotate?"
create table garmin.token_events (
    id                 bigint generated always as identity primary key,
    account_id         uuid not null references garmin.accounts (id) on delete cascade,
    event              text not null,
    refresh_rotated    boolean,
    access_expires_at  timestamptz,
    created_at         timestamptz not null default now()
);

create table garmin.spike_ticks (
    id           bigint generated always as identity primary key,
    source       text not null,
    phase        text not null,
    detail       jsonb not null default '{}',
    received_at  timestamptz not null default now()
);

create table garmin.spike_results (
    id          bigint generated always as identity primary key,
    kind        text not null,
    result      jsonb not null,
    created_at  timestamptz not null default now()
);

alter table garmin.accounts enable row level security;
alter table garmin.garmin_tokens enable row level security;
alter table garmin.token_events enable row level security;
alter table garmin.spike_ticks enable row level security;
alter table garmin.spike_results enable row level security;
