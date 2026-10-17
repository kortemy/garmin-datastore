-- Some calendar items (naps) have no numeric id; key them by a text id instead.
alter table garmin.calendar_items alter column item_id type text using item_id::text;
