-- Per-domain audit (M3 exit: "every endpoint is synced or explicitly empty").
create view garmin.v_domain_coverage with (security_invoker = true) as
select
    w.account_id,
    w.domain,
    count(*)                                         as units,
    count(*) filter (where w.status = 'done')        as done,
    count(*) filter (where w.status = 'empty')       as empty,
    count(*) filter (where w.status = 'pending')     as pending,
    count(*) filter (where w.status = 'failed')      as failed,
    max(w.last_fetched_at)                           as last_fetched_at,
    case
        when count(*) filter (where w.status = 'failed') > 0  then 'failing'
        when count(*) filter (where w.status = 'pending') > 0 then 'in_progress'
        when count(*) filter (where w.status = 'done') > 0    then 'synced'
        else 'empty'
    end                                              as state
from garmin.work_units w
group by w.account_id, w.domain;

grant select on garmin.v_domain_coverage to gds_reader;
