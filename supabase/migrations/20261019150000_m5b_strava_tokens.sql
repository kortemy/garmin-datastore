-- M5b: Strava OAuth token for W-9 (Strava title/description updates, design §8.4).
-- Encrypted like the Garmin token (AES-256-GCM, TOKEN_ENC_KEY). App-only: RLS on, no policies.

create table garmin.strava_tokens (
    account_id         uuid primary key references garmin.accounts (id) on delete cascade,
    athlete_id         bigint,
    scopes             text,
    ciphertext         bytea not null,
    nonce              bytea not null,
    access_expires_at  timestamptz,
    status             text not null default 'active' check (status in ('active', 'needs_reauth')),
    updated_at         timestamptz not null default now()
);

alter table garmin.strava_tokens enable row level security;
