-- M6: dedicated database login for the deployed app (design §4.1, §10).
-- Vercel connects as gds_worker instead of postgres: read/write on garmin.* only, no Vault,
-- no pg_cron, no DDL. The role is created without a login here; `gds setup` generates the
-- password locally, sets it as a SCRAM verifier (the plaintext never reaches the server or
-- its logs), and pushes the resulting connection string to Vercel as DATABASE_URL.
-- BYPASSRLS: the worker owns all writes, like postgres did; RLS still keeps anon/authenticated out.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'gds_worker') then
        create role gds_worker nologin noinherit bypassrls;
    end if;
end
$$;

grant usage on schema garmin to gds_worker;
grant select, insert, update, delete on all tables in schema garmin to gds_worker;
grant usage, select on all sequences in schema garmin to gds_worker;
grant execute on function garmin.intraday_bucketed(uuid, text, date, integer) to gds_worker;

-- Tables and sequences added by later migrations (run as postgres) are covered automatically.
alter default privileges for role postgres in schema garmin
    grant select, insert, update, delete on tables to gds_worker;
alter default privileges for role postgres in schema garmin
    grant usage, select on sequences to gds_worker;

-- Read tools drop to gds_reader with `set local role` (gds/tools.py).
grant gds_reader to gds_worker with inherit false, set true;
