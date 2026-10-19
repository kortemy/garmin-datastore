-- M5b: link an activity to the planned workout it was executed from (W-9 composes Strava
-- titles from it). From get_activity: metadataDTO.associatedWorkoutId and
-- summaryDTO.directWorkoutComplianceScore (0-100). Filled by `gds renormalize activity_summary`.

alter table garmin.activities
    add column workout_id bigint,
    add column workout_compliance real;

-- Same view, two columns appended (create or replace may only add at the end).
create or replace view garmin.v_activities with (security_invoker = true) as
select a.account_id,
    a.activity_id,
    a.start_local,
    a.start_utc,
    a.type_key,
    a.name,
    a.description,
    round((a.distance_m / 1000::double precision)::numeric, 2) as distance_km,
    round((a.duration_s / 60::double precision)::numeric, 1) as duration_min,
    case
        when a.distance_m > 0::double precision and a.type_key ~~ '%running%'::text
        then to_char(make_interval(secs => a.duration_s / (a.distance_m / 1000::double precision)), 'MI:SS'::text)
        else null::text
    end as pace_per_km,
    a.avg_hr,
    a.max_hr,
    a.avg_power,
    a.aerobic_te,
    a.anaerobic_te,
    a.te_label,
    a.training_load,
    a.calories,
    a.elevation_gain_m,
    a.rpe,
    a.feel,
    a.hr_zone_s,
    w.temp_c,
    w.conditions,
    (select string_agg(g.value ->> 'name'::text, ', '::text) from jsonb_array_elements(a.gear) g(value)) as gear,
    a.location_name,
    (exists (select 1 from garmin.activity_files f
             where f.account_id = a.account_id and f.activity_id = a.activity_id)) as has_fit,
    a.workout_id,
    (select e.name from garmin.entities e
     where e.account_id = a.account_id and e.kind = 'workout' and e.entity_id = a.workout_id::text) as workout_name,
    a.workout_compliance
from garmin.activities a
left join garmin.activity_weather w using (account_id, activity_id);
