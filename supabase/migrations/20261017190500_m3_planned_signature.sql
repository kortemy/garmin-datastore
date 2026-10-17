-- The planner only (re)creates work units when this changes (day | backfill start | registry).
alter table garmin.accounts add column planned_signature text;
