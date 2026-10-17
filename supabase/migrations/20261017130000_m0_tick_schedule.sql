-- Call the Vercel tick endpoint every 10 minutes (design §9, D-1).
-- URL and shared secret live in Supabase Vault (`gds_tick_url`, `gds_tick_secret`),
-- created by `gds setup`. pg_net is fire-and-forget: the tick keeps running on
-- Vercel after the 5 s request timeout (verified in M0).

select cron.unschedule('gds_tick') where exists (select 1 from cron.job where jobname = 'gds_tick');

select cron.schedule(
    'gds_tick',
    '*/10 * * * *',
    $$
    select net.http_post(
        url := (select decrypted_secret from vault.decrypted_secrets where name = 'gds_tick_url'),
        headers := jsonb_build_object(
            'Content-Type', 'application/json',
            'X-GDS-Secret', (select decrypted_secret from vault.decrypted_secrets where name = 'gds_tick_secret')
        ),
        body := '{"source": "pg_cron"}'::jsonb,
        timeout_milliseconds := 5000
    );
    $$
);
