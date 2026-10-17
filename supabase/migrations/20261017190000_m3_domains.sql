-- M3: body measurements, performance metrics, entities (snapshot docs), calendar, Body Battery events.

create table garmin.weigh_ins (
    account_id       uuid not null references garmin.accounts (id) on delete cascade,
    sample_pk        bigint not null,
    measured_utc     timestamptz,
    calendar_date    date,
    weight_kg        real,      -- Garmin reports grams
    bmi              real,
    body_fat_pct     real,
    body_water_pct   real,
    bone_mass_kg     real,
    muscle_mass_kg   real,
    visceral_fat     real,
    metabolic_age    real,
    physique_rating  real,
    source_type      text,
    updated_at       timestamptz not null default now(),
    primary key (account_id, sample_pk)
);

create table garmin.blood_pressure (
    account_id     uuid not null references garmin.accounts (id) on delete cascade,
    measured_utc   timestamptz not null,
    calendar_date  date,
    utc_offset_min integer,
    systolic       integer,
    diastolic      integer,
    pulse          integer,
    category       text,
    notes          text,
    source_type    text,
    sample_version bigint,
    updated_at     timestamptz not null default now(),
    primary key (account_id, measured_utc)
);

-- Long-tail scalar metrics, one row per (day, metric). Units are in the metric name.
create table garmin.performance_metrics (
    account_id     uuid not null references garmin.accounts (id) on delete cascade,
    calendar_date  date not null,
    metric         text not null,
    value          real,
    detail         jsonb,
    updated_at     timestamptz not null default now(),
    primary key (account_id, calendar_date, metric)
);

-- Snapshot documents (devices, gear, PRs, goals, badges, workouts, plans, courses, profile...).
create table garmin.entities (
    account_id  uuid not null references garmin.accounts (id) on delete cascade,
    kind        text not null,
    entity_id   text not null,
    name        text,
    data        jsonb not null,
    updated_at  timestamptz not null default now(),
    primary key (account_id, kind, entity_id)
);

create table garmin.calendar_items (
    account_id      uuid not null references garmin.accounts (id) on delete cascade,
    item_type       text not null,      -- workout, activity, nap, event, ...
    item_id         bigint not null,
    month           text not null,      -- YYYY-MM it was listed under (for replace-on-refresh)
    item_date       date,
    title           text,
    sport_type_key  text,
    duration_s      real,
    distance_m      real,
    workout_id      bigint,
    training_plan_id bigint,
    data            jsonb,
    updated_at      timestamptz not null default now(),
    primary key (account_id, item_type, item_id)
);
create index calendar_items_by_date on garmin.calendar_items (account_id, item_date);

create table garmin.body_battery_events (
    account_id       uuid not null references garmin.accounts (id) on delete cascade,
    event_start_utc  timestamptz not null,
    event_type       text not null,
    calendar_date    date,
    utc_offset_min   integer,
    duration_s       real,
    impact           integer,
    feedback_type    text,
    short_feedback   text,
    activity_id      bigint,
    activity_name    text,
    activity_type    text,
    avg_stress       real,
    updated_at       timestamptz not null default now(),
    primary key (account_id, event_start_utc, event_type)
);

alter table garmin.weigh_ins enable row level security;
alter table garmin.blood_pressure enable row level security;
alter table garmin.performance_metrics enable row level security;
alter table garmin.entities enable row level security;
alter table garmin.calendar_items enable row level security;
alter table garmin.body_battery_events enable row level security;

create view garmin.v_body with (security_invoker = true) as
select account_id, coalesce(calendar_date, (measured_utc at time zone 'UTC')::date) as date, 'weigh_in' as kind,
       measured_utc, weight_kg, bmi, body_fat_pct, muscle_mass_kg, null::integer as systolic,
       null::integer as diastolic, null::integer as pulse, source_type
from garmin.weigh_ins
union all
select account_id, calendar_date, 'blood_pressure', measured_utc, null, null, null, null, systolic, diastolic, pulse,
       source_type
from garmin.blood_pressure;

-- Latest value per metric, with the date it was measured.
create view garmin.v_latest_performance with (security_invoker = true) as
select distinct on (account_id, metric) account_id, metric, value, calendar_date, detail
from garmin.performance_metrics
where value is not null
order by account_id, metric, calendar_date desc;

grant select on garmin.weigh_ins, garmin.blood_pressure, garmin.performance_metrics, garmin.entities,
                garmin.calendar_items, garmin.body_battery_events, garmin.v_body, garmin.v_latest_performance
    to gds_reader;
do $$
declare t text;
begin
    foreach t in array array['weigh_ins', 'blood_pressure', 'performance_metrics', 'entities', 'calendar_items',
                             'body_battery_events']
    loop
        execute format('create policy gds_reader_select on garmin.%I for select to gds_reader using (true)', t);
    end loop;
end $$;
