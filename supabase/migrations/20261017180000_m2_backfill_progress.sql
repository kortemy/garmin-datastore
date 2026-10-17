-- Backfill progress in v_sync_health (FR-5 "observable"). New columns are appended so
-- `create or replace view` keeps existing ones in place.

create or replace view garmin.v_sync_health with (security_invoker = true) as
with units as (
    select account_id,
           case when domain like 'activity\_%' then 'activity' else 'daily' end as scope,
           status, scope_key
    from garmin.work_units
), progress as (
    select account_id,
           count(*) filter (where scope = 'daily')                                         as daily_total,
           count(*) filter (where scope = 'daily' and status in ('done', 'empty'))         as daily_complete,
           count(*) filter (where scope = 'activity')                                      as activity_total,
           count(*) filter (where scope = 'activity' and status in ('done', 'empty'))      as activity_complete,
           min(scope_key) filter (where scope = 'daily' and status in ('done', 'empty')
                                    and scope_key ~ '^\d{4}-\d{2}-\d{2}$')                 as oldest_day_synced
    from units
    group by account_id
)
select
    a.id as account_id,
    a.label,
    a.status,
    a.status_reason,
    a.cooldown_until,
    a.backfill_start,
    a.last_incremental_at,
    (select max(finished_at) from garmin.sync_runs r where r.account_id = a.id and r.status in ('ok', 'partial'))
        as last_success_at,
    (select count(*) from garmin.work_units w where w.account_id = a.id and w.status = 'pending') as units_pending,
    (select count(*) from garmin.work_units w where w.account_id = a.id and w.status = 'failed')  as units_failed,
    (select count(*) from garmin.work_units w where w.account_id = a.id and w.status in ('done', 'empty'))
        as units_complete,
    (select pg_size_pretty(sum(bytes_gz)) from garmin.raw_objects o where o.account_id = a.id) as raw_size,
    p.daily_complete,
    p.daily_total,
    p.activity_complete,
    p.activity_total,
    p.oldest_day_synced,
    round(100.0 * (coalesce(p.daily_complete, 0) + coalesce(p.activity_complete, 0))
          / nullif(coalesce(p.daily_total, 0) + coalesce(p.activity_total, 0), 0), 1) as backfill_pct,
    (select pg_size_pretty(sum(bytes)) from garmin.activity_files f where f.account_id = a.id) as fit_size,
    (select count(*) from garmin.activities x where x.account_id = a.id) as activities
from garmin.accounts a
left join progress p on p.account_id = a.id;
