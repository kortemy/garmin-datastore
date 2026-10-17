-- Daily watchdog (design §9): runs inside Postgres and emails via Resend with pg_net, so
-- alerts still arrive if Vercel is down. Secrets come from Vault (set by `gds setup` when
-- RESEND_API_KEY and ALERT_EMAIL are in .env). Without them, problems are only recorded
-- in garmin.alerts.

create or replace function garmin.watchdog()
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
    h         record;
    api_key   text;
    recipient text;
    sender    text;
    problems  text[] := '{}';
    message   text;
begin
    select decrypted_secret into api_key from vault.decrypted_secrets where name = 'gds_resend_api_key';
    select decrypted_secret into recipient from vault.decrypted_secrets where name = 'gds_alert_email';
    select coalesce((select decrypted_secret from vault.decrypted_secrets where name = 'gds_alert_from'),
                    'Garmin Datastore <onboarding@resend.dev>') into sender;

    for h in select * from garmin.v_sync_health loop
        if h.status = 'needs_reauth' then
            problems := problems || format('%s: Garmin needs re-authentication (%s). On your PC run: uv run python -m gds.cli auth',
                                           h.label, coalesce(h.status_reason, 'token rejected'));
        end if;
        if h.last_success_at is null or h.last_success_at < now() - interval '24 hours' then
            problems := problems || format('%s: no successful sync since %s.', h.label,
                                           coalesce(to_char(h.last_success_at, 'YYYY-MM-DD HH24:MI "UTC"'), 'ever'));
        end if;
        if h.units_failed > 0 then
            problems := problems || format('%s: %s work unit(s) failed permanently. See get_sync_status.',
                                           h.label, h.units_failed);
        end if;
    end loop;

    if cardinality(problems) = 0 then
        return 'ok';
    end if;

    message := array_to_string(problems, E'\n');
    insert into garmin.alerts (account_id, condition, message, delivered)
    values (null, 'watchdog', message, api_key is not null and recipient is not null);

    if api_key is null or recipient is null then
        return 'problems recorded (email not configured)';
    end if;

    perform net.http_post(
        url := 'https://api.resend.com/emails',
        headers := jsonb_build_object('Authorization', 'Bearer ' || api_key, 'Content-Type', 'application/json'),
        body := jsonb_build_object('from', sender, 'to', jsonb_build_array(recipient),
                                   'subject', '[garmin-datastore] watchdog', 'text', message),
        timeout_milliseconds := 10000
    );
    return 'alerted';
end
$$;

revoke all on function garmin.watchdog() from public, anon, authenticated;

select cron.unschedule('gds_watchdog') where exists (select 1 from cron.job where jobname = 'gds_watchdog');
-- 06:00 UTC = 08:00 CEST / 07:00 CET
select cron.schedule('gds_watchdog', '0 6 * * *', $$select garmin.watchdog()$$);
