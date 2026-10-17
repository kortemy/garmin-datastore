-- Watchdog test mode: `select garmin.watchdog(true)` sends a test email even when healthy,
-- to check the in-database alert path (Vault secrets → pg_net → Resend).

drop function if exists garmin.watchdog();

create function garmin.watchdog(p_test boolean default false)
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
    subject   text := '[garmin-datastore] watchdog';
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

    if cardinality(problems) = 0 and not p_test then
        return 'ok';
    end if;

    if p_test then
        subject := '[garmin-datastore] test alert (database watchdog)';
        message := 'This is a test of garmin-datastore''s in-database watchdog (pg_cron -> pg_net -> Resend).'
                   || E'\nCurrent problems: ' || coalesce(nullif(array_to_string(problems, '; '), ''), 'none');
    else
        message := array_to_string(problems, E'\n');
        insert into garmin.alerts (account_id, condition, message, delivered)
        values (null, 'watchdog', message, api_key is not null and recipient is not null);
    end if;

    if api_key is null or recipient is null then
        return 'problems recorded (email not configured)';
    end if;

    perform net.http_post(
        url := 'https://api.resend.com/emails',
        headers := jsonb_build_object('Authorization', 'Bearer ' || api_key, 'Content-Type', 'application/json',
                                      'Idempotency-Key', 'watchdog-' || to_char(now(), 'YYYYMMDDHH24MISS')),
        body := jsonb_build_object('from', sender, 'to', jsonb_build_array(recipient),
                                   'subject', subject, 'text', message),
        timeout_milliseconds := 10000
    );
    return case when p_test then 'test sent' else 'alerted' end;
end
$$;

revoke all on function garmin.watchdog(boolean) from public, anon, authenticated;
