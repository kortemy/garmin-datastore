-- M4: OAuth 2.1 state for the Claude connector (design §8.1) and the reader role switch.
-- Only hashes of codes and tokens are stored. RLS on with no policies: app-only tables.

create table garmin.oauth_clients (
    client_id   text primary key,
    info        jsonb not null,           -- OAuthClientInformationFull from dynamic registration
    created_at  timestamptz not null default now()
);

-- An /authorize request waiting for the owner to log in (single use, short-lived).
create table garmin.oauth_requests (
    id          text primary key,
    client_id   text not null references garmin.oauth_clients (client_id) on delete cascade,
    params      jsonb not null,
    expires_at  timestamptz not null,
    created_at  timestamptz not null default now()
);

create table garmin.oauth_codes (
    code_hash   text primary key,
    client_id   text not null references garmin.oauth_clients (client_id) on delete cascade,
    data        jsonb not null,
    expires_at  timestamptz not null
);

create table garmin.oauth_tokens (
    token_hash  text primary key,
    kind        text not null check (kind in ('access', 'refresh')),
    family      uuid not null,            -- access + refresh issued together; revoked together
    client_id   text not null references garmin.oauth_clients (client_id) on delete cascade,
    scopes      text[] not null default '{}',
    resource    text,
    expires_at  timestamptz not null,
    created_at  timestamptz not null default now()
);
create index oauth_tokens_family on garmin.oauth_tokens (family);

create table garmin.oauth_login_failures (
    id          bigint generated always as identity primary key,
    at          timestamptz not null default now()
);

alter table garmin.oauth_clients enable row level security;
alter table garmin.oauth_requests enable row level security;
alter table garmin.oauth_codes enable row level security;
alter table garmin.oauth_tokens enable row level security;
alter table garmin.oauth_login_failures enable row level security;

-- The app connects as postgres and enters the read-only role for query tools.
grant gds_reader to postgres;
grant select on garmin.v_domain_coverage, garmin.v_activities to gds_reader;
