-- M2: activities and their sub-resources (design §4.3), plus original FIT files.

create table garmin.activities (
    account_id            uuid not null references garmin.accounts (id) on delete cascade,
    activity_id           bigint not null,
    activity_uuid         text,
    name                  text,
    description           text,
    type_key              text,
    parent_type_id        integer,
    event_type            text,
    sport_type_id         integer,
    start_utc             timestamptz,
    start_local           timestamp,
    utc_offset_min        integer,
    end_utc               timestamptz,
    duration_s            real,
    elapsed_s             real,
    moving_s              real,
    distance_m            real,
    elevation_gain_m      real,
    elevation_loss_m      real,
    min_elevation_m       real,
    max_elevation_m       real,
    avg_speed_mps         real,
    max_speed_mps         real,
    avg_gap_speed_mps     real,
    avg_hr                real,
    max_hr                real,
    min_hr                real,
    calories              real,
    bmr_calories          real,
    steps                 integer,
    avg_cadence           real,
    max_cadence           real,
    avg_power             real,
    max_power             real,
    norm_power            real,
    aerobic_te            real,
    anaerobic_te          real,
    te_label              text,
    training_load         real,
    vo2max                real,
    moderate_im           integer,
    vigorous_im           integer,
    body_battery_diff     integer,
    water_estimated_ml    real,
    lap_count             integer,
    location_name         text,
    start_lat             double precision,
    start_lon             double precision,
    end_lat               double precision,
    end_lon               double precision,
    device_id             bigint,
    manufacturer          text,
    is_manual             boolean,
    is_favorite           boolean,
    is_pr                 boolean,
    rpe                   integer,   -- directWorkoutRpe (Garmin scale 10-100)
    feel                  integer,   -- directWorkoutFeel (0-100)
    fastest_1k_s          real,
    fastest_mile_s        real,
    hr_zone_s             real[],    -- seconds in HR zones 1..5 (from the list summary)
    power_zone_s          real[],
    gear                  jsonb,     -- [{gear_pk, name, type}]
    updated_at            timestamptz not null default now(),
    primary key (account_id, activity_id)
);
create index activities_by_start on garmin.activities (account_id, start_utc desc);

create table garmin.activity_laps (
    account_id         uuid not null references garmin.accounts (id) on delete cascade,
    activity_id        bigint not null,
    lap_index          integer not null,
    start_utc          timestamptz,
    duration_s         real,
    moving_s           real,
    distance_m         real,
    avg_speed_mps      real,
    max_speed_mps      real,
    avg_gap_speed_mps  real,
    avg_hr             real,
    max_hr             real,
    avg_cadence        real,
    max_cadence        real,
    avg_power          real,
    max_power          real,
    norm_power         real,
    elevation_gain_m   real,
    elevation_loss_m   real,
    calories           real,
    intensity_type     text,
    stride_length_cm   real,
    ground_contact_ms  real,
    vert_oscillation_cm real,
    vert_ratio_pct     real,
    updated_at         timestamptz not null default now(),
    primary key (account_id, activity_id, lap_index)
);

create table garmin.activity_zones (
    account_id    uuid not null references garmin.accounts (id) on delete cascade,
    activity_id   bigint not null,
    zone_type     text not null check (zone_type in ('hr', 'power')),
    zone          integer not null,
    low_boundary  real,
    seconds       real,
    updated_at    timestamptz not null default now(),
    primary key (account_id, activity_id, zone_type, zone)
);

create table garmin.activity_exercise_sets (
    account_id      uuid not null references garmin.accounts (id) on delete cascade,
    activity_id     bigint not null,
    set_index       integer not null,
    set_type        text,
    start_utc       timestamptz,
    duration_s      real,
    reps            integer,
    weight_raw      real,     -- as reported by Garmin (unit not confirmed yet)
    category        text,     -- top-probability exercise guess
    exercise_name   text,
    probability     real,
    candidates      jsonb,    -- all exercise guesses with probabilities
    updated_at      timestamptz not null default now(),
    primary key (account_id, activity_id, set_index)
);

create table garmin.activity_weather (
    account_id         uuid not null references garmin.accounts (id) on delete cascade,
    activity_id        bigint not null,
    observed_utc       timestamptz,
    temp_f             real,      -- Garmin reports weather in imperial units
    apparent_temp_f    real,
    dew_point_f        real,
    temp_c             real generated always as (round(((temp_f - 32) * 5 / 9)::numeric, 1)) stored,
    apparent_temp_c    real generated always as (round(((apparent_temp_f - 32) * 5 / 9)::numeric, 1)) stored,
    humidity_pct       real,
    wind_dir_deg       real,
    wind_dir_compass   text,
    wind_speed_mph     real,
    wind_gust_mph      real,
    conditions         text,
    station_id         text,
    updated_at         timestamptz not null default now(),
    primary key (account_id, activity_id)
);

-- Original activity files (Garmin "original" download = zip containing the FIT file).
create table garmin.activity_files (
    account_id   uuid not null references garmin.accounts (id) on delete cascade,
    activity_id  bigint not null,
    format       text not null,
    bytes        integer not null,
    sha256       text not null,
    content      bytea not null,
    updated_at   timestamptz not null default now(),
    primary key (account_id, activity_id, format)
);
alter table garmin.activity_files alter column content set storage external;  -- already compressed

alter table garmin.activities enable row level security;
alter table garmin.activity_laps enable row level security;
alter table garmin.activity_zones enable row level security;
alter table garmin.activity_exercise_sets enable row level security;
alter table garmin.activity_weather enable row level security;
alter table garmin.activity_files enable row level security;

create view garmin.v_activities with (security_invoker = true) as
select
    a.account_id,
    a.activity_id,
    a.start_local,
    a.start_utc,
    a.type_key,
    a.name,
    a.description,
    round((a.distance_m / 1000)::numeric, 2)                         as distance_km,
    round((a.duration_s / 60)::numeric, 1)                           as duration_min,
    case when a.distance_m > 0 and a.type_key like '%running%'
         then to_char(make_interval(secs => a.duration_s / (a.distance_m / 1000)), 'MI:SS') end
                                                                     as pace_per_km,
    a.avg_hr, a.max_hr,
    a.avg_power,
    a.aerobic_te, a.anaerobic_te, a.te_label,
    a.training_load,
    a.calories,
    a.elevation_gain_m,
    a.rpe, a.feel,
    a.hr_zone_s,
    w.temp_c, w.conditions,
    (select string_agg(g ->> 'name', ', ') from jsonb_array_elements(a.gear) g) as gear,
    a.location_name,
    exists (select 1 from garmin.activity_files f
            where f.account_id = a.account_id and f.activity_id = a.activity_id) as has_fit
from garmin.activities a
left join garmin.activity_weather w using (account_id, activity_id);

grant select on garmin.activities, garmin.activity_laps, garmin.activity_zones,
                garmin.activity_exercise_sets, garmin.activity_weather, garmin.v_activities to gds_reader;
-- File metadata only; the reader never needs the bytes themselves.
grant select (account_id, activity_id, format, bytes, sha256, updated_at) on garmin.activity_files to gds_reader;
do $$
declare t text;
begin
    foreach t in array array['activities', 'activity_laps', 'activity_zones', 'activity_exercise_sets', 'activity_weather',
                             'activity_files']
    loop
        execute format('create policy gds_reader_select on garmin.%I for select to gds_reader using (true)', t);
    end loop;
end $$;
