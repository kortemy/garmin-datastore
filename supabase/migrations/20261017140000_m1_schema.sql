-- M1 schema: sync engine tables, daily wellness tables, intraday series, consumer views.
-- Design §4. Raw payloads live in garmin.raw_objects (gzip bytea) rather than Storage:
-- measured volume (~50 MB for full history) fits the DB easily and avoids a second secret.

-- ---------------------------------------------------------------- accounts (extend)
alter table garmin.accounts
    add column backfill_start        date,
    add column home_timezone         text not null default 'CET',
    add column cooldown_until        timestamptz,
    add column status_reason         text,
    add column last_incremental_at   timestamptz;

-- ---------------------------------------------------------------- engine
create table garmin.leases (
    name        text primary key,
    holder      text not null,
    expires_at  timestamptz not null
);

create table garmin.work_units (
    account_id       uuid not null references garmin.accounts (id) on delete cascade,
    domain           text not null,
    scope_key        text not null,
    status           text not null default 'pending'
                     check (status in ('pending', 'done', 'empty', 'failed')),
    priority         integer not null default 10,
    attempts         integer not null default 0,
    next_attempt_at  timestamptz not null default now(),
    last_error       text,
    last_fetched_at  timestamptz,
    content_hash     text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    primary key (account_id, domain, scope_key)
);
create index work_units_queue on garmin.work_units (account_id, priority desc, scope_key desc)
    where status = 'pending';

create table garmin.sync_runs (
    id            bigint generated always as identity primary key,
    account_id    uuid references garmin.accounts (id) on delete cascade,
    trigger       text not null,
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    status        text not null default 'running'
                  check (status in ('running', 'ok', 'partial', 'skipped', 'failed')),
    units_done    integer not null default 0,
    units_empty   integer not null default 0,
    units_failed  integer not null default 0,
    requests      integer not null default 0,
    bytes_raw     bigint not null default 0,
    details       jsonb not null default '{}',
    error         text
);
create index sync_runs_recent on garmin.sync_runs (account_id, started_at desc);

create table garmin.raw_objects (
    account_id    uuid not null references garmin.accounts (id) on delete cascade,
    domain        text not null,
    scope_key     text not null,
    content_hash  text not null,
    bytes_raw     integer not null,
    bytes_gz      integer not null,
    payload_gz    bytea not null,
    fetched_at    timestamptz not null default now(),
    primary key (account_id, domain, scope_key)
);
-- payload_gz is already compressed; don't let TOAST try again.
alter table garmin.raw_objects alter column payload_gz set storage external;

create table garmin.alerts (
    id          bigint generated always as identity primary key,
    account_id  uuid references garmin.accounts (id) on delete cascade,
    condition   text not null,
    message     text not null,
    delivered   boolean not null default false,
    created_at  timestamptz not null default now()
);
create index alerts_dedupe on garmin.alerts (account_id, condition, created_at desc);

-- ---------------------------------------------------------------- daily data
create table garmin.daily_summary (
    account_id              uuid not null references garmin.accounts (id) on delete cascade,
    calendar_date           date not null,
    utc_offset_min          integer,
    total_steps             integer,
    step_goal               integer,
    total_distance_m        integer,
    floors_ascended         real,
    floors_descended        real,
    total_kcal              real,
    active_kcal             real,
    bmr_kcal                real,
    moderate_intensity_min  integer,
    vigorous_intensity_min  integer,
    intensity_goal_min      integer,
    resting_hr              integer,
    rhr_7d_avg              integer,
    min_hr                  integer,
    max_hr                  integer,
    avg_stress              integer,
    max_stress              integer,
    stress_qualifier        text,
    rest_stress_s           integer,
    low_stress_s            integer,
    medium_stress_s         integer,
    high_stress_s           integer,
    activity_stress_s       integer,
    uncategorized_stress_s  integer,
    bb_charged              integer,
    bb_drained              integer,
    bb_high                 integer,
    bb_low                  integer,
    bb_most_recent          integer,
    bb_during_sleep         integer,
    bb_at_wake              integer,
    avg_spo2                real,
    lowest_spo2             real,
    avg_waking_respiration  real,
    highest_respiration     real,
    lowest_respiration      real,
    highly_active_s         integer,
    active_s                integer,
    sedentary_s             integer,
    sleeping_s              integer,
    hydration_ml            real,
    hydration_goal_ml       real,
    sweat_loss_ml           real,
    updated_at              timestamptz not null default now(),
    primary key (account_id, calendar_date)
);

create table garmin.sleep (
    account_id               uuid not null references garmin.accounts (id) on delete cascade,
    calendar_date            date not null,
    sleep_start_utc          timestamptz,
    sleep_end_utc            timestamptz,
    utc_offset_min           integer,
    sleep_s                  integer,
    nap_s                    integer,
    deep_s                   integer,
    light_s                  integer,
    rem_s                    integer,
    awake_s                  integer,
    unmeasurable_s           integer,
    score                    integer,
    score_qualifier          text,
    deep_pct                 integer,
    light_pct                integer,
    rem_pct                  integer,
    sub_scores               jsonb,
    score_feedback           text,
    score_insight            text,
    avg_respiration          real,
    lowest_respiration       real,
    highest_respiration      real,
    avg_sleep_stress         real,
    avg_hr                   real,
    awake_count              integer,
    restless_moments         integer,
    avg_overnight_hrv        real,
    hrv_status               text,
    body_battery_change      integer,
    resting_hr               integer,
    sleep_need_min           integer,
    sleep_need_baseline_min  integer,
    sleep_need_feedback      text,
    window_confirmation      text,
    updated_at               timestamptz not null default now(),
    primary key (account_id, calendar_date)
);

create table garmin.hrv (
    account_id               uuid not null references garmin.accounts (id) on delete cascade,
    calendar_date            date not null,
    last_night_avg           integer,
    last_night_5min_high     integer,
    weekly_avg               integer,
    baseline_low_upper       integer,
    baseline_balanced_low    integer,
    baseline_balanced_upper  integer,
    status                   text,
    feedback_phrase          text,
    updated_at               timestamptz not null default now(),
    primary key (account_id, calendar_date)
);

create table garmin.training_readiness (
    account_id                     uuid not null references garmin.accounts (id) on delete cascade,
    reading_utc                    timestamptz not null,
    calendar_date                  date not null,
    utc_offset_min                 integer,
    is_morning                     boolean not null default false,
    score                          integer,
    level                          text,
    feedback_short                 text,
    feedback_long                  text,
    input_context                  text,
    sleep_score                    integer,
    sleep_factor_pct               integer,
    sleep_factor_feedback          text,
    recovery_time_min              integer,
    recovery_factor_pct            integer,
    recovery_factor_feedback       text,
    acwr_factor_pct                integer,
    acwr_factor_feedback           text,
    acute_load                     integer,
    stress_history_factor_pct      integer,
    stress_history_factor_feedback text,
    hrv_factor_pct                 integer,
    hrv_factor_feedback            text,
    hrv_weekly_avg                 integer,
    sleep_history_factor_pct       integer,
    sleep_history_factor_feedback  text,
    valid_sleep                    boolean,
    updated_at                     timestamptz not null default now(),
    primary key (account_id, reading_utc)
);
create index training_readiness_by_date on garmin.training_readiness (account_id, calendar_date);

create table garmin.training_status (
    account_id                      uuid not null references garmin.accounts (id) on delete cascade,
    calendar_date                   date not null,
    status_code                     integer,
    status_phrase                   text,
    sport                           text,
    fitness_trend                   integer,
    training_paused                 boolean,
    weekly_training_load            real,
    acute_load                      real,
    chronic_load                    real,
    chronic_load_min                real,
    chronic_load_max                real,
    acwr                            real,
    acwr_pct                        integer,
    acwr_status                     text,
    load_balance_phrase             text,
    monthly_load_aerobic_low        real,
    monthly_load_aerobic_high       real,
    monthly_load_anaerobic          real,
    aerobic_low_target_min          integer,
    aerobic_low_target_max          integer,
    aerobic_high_target_min         integer,
    aerobic_high_target_max         integer,
    anaerobic_target_min            integer,
    anaerobic_target_max            integer,
    vo2max                          real,   -- measured that day (max metrics), else null
    vo2max_latest                   real,   -- most recent estimate as of that day
    vo2max_latest_precise           real,
    vo2max_latest_date              date,
    updated_at                      timestamptz not null default now(),
    primary key (account_id, calendar_date)
);

-- One row per metric per day, samples as parallel arrays (design §4.4).
create table garmin.intraday_series (
    account_id      uuid not null references garmin.accounts (id) on delete cascade,
    calendar_date   date not null,
    metric          text not null,
    start_utc       timestamptz not null,
    utc_offset_min  integer not null,
    ts_offsets_s    integer[] not null,
    vals            real[] not null,
    updated_at      timestamptz not null default now(),
    primary key (account_id, calendar_date, metric),
    check (cardinality(ts_offsets_s) = cardinality(vals))
);
comment on table garmin.intraday_series is
  'Metrics: hr (bpm), stress (0-100; -1 off-wrist, -2 activity), body_battery (0-100), '
  'respiration (brpm), spo2 (%), steps (per 15 min), floors (ascended per 15 min), '
  'sleep_stage (0 deep, 1 light, 2 REM, 3 awake), hrv (ms, overnight).';

-- ---------------------------------------------------------------- RLS
alter table garmin.leases enable row level security;
alter table garmin.work_units enable row level security;
alter table garmin.sync_runs enable row level security;
alter table garmin.raw_objects enable row level security;
alter table garmin.alerts enable row level security;
alter table garmin.daily_summary enable row level security;
alter table garmin.sleep enable row level security;
alter table garmin.hrv enable row level security;
alter table garmin.training_readiness enable row level security;
alter table garmin.training_status enable row level security;
alter table garmin.intraday_series enable row level security;

-- ---------------------------------------------------------------- consumer views
-- Field names match the Notion "Garmin Wellness Log" collector (wellness_report.py), so
-- prompts and parity checks carry over unchanged (FR-3).
create view garmin.v_daily_wellness with (security_invoker = true) as
with latest_readiness as (
    select distinct on (account_id, calendar_date) *
    from garmin.training_readiness
    order by account_id, calendar_date, reading_utc desc
), morning_readiness as (
    select distinct on (account_id, calendar_date) account_id, calendar_date, score
    from garmin.training_readiness
    where is_morning
    order by account_id, calendar_date, reading_utc desc
), days as (
    select account_id, calendar_date from garmin.daily_summary
    union select account_id, calendar_date from garmin.sleep
    union select account_id, calendar_date from garmin.training_readiness
)
select
    d.account_id,
    d.calendar_date                                   as date,
    r.score                                           as readiness_score,
    r.level                                           as readiness_level,
    r.feedback_short                                  as readiness_feedback,
    r.feedback_long                                   as readiness_feedback_long,
    mr.score                                          as readiness_morning_score,
    r.recovery_time_min,
    round(r.recovery_time_min / 60.0, 1)              as recovery_hours,
    s.score                                           as sleep_score,
    s.score_qualifier                                 as sleep_qualifier,
    s.sleep_s                                         as sleep_seconds,
    s.deep_s                                          as deep_seconds,
    s.light_s                                         as light_seconds,
    s.rem_s                                           as rem_seconds,
    s.awake_s                                         as awake_seconds,
    round(s.awake_s / 60.0)                           as awake_min,
    s.deep_pct, s.light_pct, s.rem_pct,
    to_char(s.sleep_start_utc + make_interval(mins => s.utc_offset_min), 'HH24:MI') as sleep_start,
    to_char(s.sleep_end_utc + make_interval(mins => s.utc_offset_min), 'HH24:MI')   as sleep_end,
    s.avg_respiration                                 as respiration_avg,
    coalesce(s.avg_overnight_hrv, h.last_night_avg)   as avg_overnight_hrv,
    h.status                                          as hrv_status,
    h.weekly_avg                                      as hrv_weekly_avg,
    coalesce(ds.resting_hr, s.resting_hr)             as resting_hr,
    s.body_battery_change,
    ds.bb_high                                        as body_battery_high,
    ds.bb_low                                         as body_battery_low,
    ds.avg_stress,
    round(ds.high_stress_s / 60.0)                    as high_stress_min,
    ds.total_steps                                    as steps,
    round(ds.floors_ascended)                         as floors,
    round(ds.active_kcal)                             as active_calories,
    case when ds.moderate_intensity_min is null and ds.vigorous_intensity_min is null then null
         else coalesce(ds.moderate_intensity_min, 0) + coalesce(ds.vigorous_intensity_min, 0) end
                                                      as intensity_min,
    coalesce(ts.vo2max, ts.vo2max_latest)             as vo2max,
    initcap(split_part(ts.status_phrase, '_', 1))     as training_status,
    r.sleep_factor_pct,
    r.recovery_factor_pct,
    r.hrv_factor_pct,
    r.stress_history_factor_pct                       as stress_factor_pct,
    r.sleep_history_factor_pct,
    r.acwr_factor_pct,
    coalesce(ts.acute_load, r.acute_load)             as acute_load,
    ts.chronic_load,
    ts.acwr,
    ts.acwr_pct,
    ts.acwr_status,
    ts.weekly_training_load,
    initcap(replace(ts.load_balance_phrase, '_', ' ')) as load_focus,
    ds.utc_offset_min
from days d
left join garmin.daily_summary ds using (account_id, calendar_date)
left join garmin.sleep s using (account_id, calendar_date)
left join garmin.hrv h using (account_id, calendar_date)
left join latest_readiness r using (account_id, calendar_date)
left join morning_readiness mr using (account_id, calendar_date)
left join garmin.training_status ts using (account_id, calendar_date);

create view garmin.v_intraday_points with (security_invoker = true) as
select
    i.account_id,
    i.calendar_date,
    i.metric,
    i.start_utc + make_interval(secs => p.ts_offset)                                  as ts_utc,
    (i.start_utc + make_interval(secs => p.ts_offset) + make_interval(mins => i.utc_offset_min))
        at time zone 'UTC'                                                            as ts_local,
    p.value
from garmin.intraday_series i
cross join lateral unnest(i.ts_offsets_s, i.vals) as p(ts_offset, value);

-- Bucketed averages for one metric/day, ignoring Garmin's negative "no data" markers.
create function garmin.intraday_bucketed(p_account uuid, p_metric text, p_date date, p_bucket_min integer default 60)
returns table (bucket_local timestamp, value real, samples integer)
language sql stable set search_path = '' as $$
    select
        date_bin(make_interval(mins => p_bucket_min), ts_local, p_date::timestamp) as bucket_local,
        case when p_metric in ('steps', 'floors') then sum(value)::real else avg(value)::real end,
        count(*)::integer
    from garmin.v_intraday_points
    where account_id = p_account and metric = p_metric and calendar_date = p_date and value >= 0
    group by 1
    order by 1;
$$;

create view garmin.v_sync_health with (security_invoker = true) as
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
    (select pg_size_pretty(sum(bytes_gz)) from garmin.raw_objects o where o.account_id = a.id) as raw_size
from garmin.accounts a;

-- ---------------------------------------------------------------- read-only role
-- NOLOGIN: used via SET ROLE by query tools (M4). No password involved.
do $$ begin
    if not exists (select 1 from pg_roles where rolname = 'gds_reader') then
        create role gds_reader nologin;
    end if;
end $$;
grant usage on schema garmin to gds_reader;
grant select on garmin.v_daily_wellness, garmin.v_intraday_points, garmin.v_sync_health,
                garmin.daily_summary, garmin.sleep, garmin.hrv, garmin.training_readiness,
                garmin.training_status, garmin.intraday_series to gds_reader;
grant execute on function garmin.intraday_bucketed(uuid, text, date, integer) to gds_reader;

-- RLS is on everywhere; the reader gets explicit read policies (security_invoker views
-- evaluate RLS as the caller). anon/authenticated get none.
grant select on garmin.accounts, garmin.sync_runs, garmin.work_units, garmin.raw_objects to gds_reader;
do $$
declare t text;
begin
    foreach t in array array['accounts', 'sync_runs', 'work_units', 'raw_objects', 'daily_summary',
                             'sleep', 'hrv', 'training_readiness', 'training_status', 'intraday_series']
    loop
        execute format('create policy gds_reader_select on garmin.%I for select to gds_reader using (true)', t);
    end loop;
end $$;
