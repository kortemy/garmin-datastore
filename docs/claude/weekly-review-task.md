# Weekly review — scheduled task prompt

Use this as the prompt for a **scheduled task** in the Fitness project (suggested: Sunday
~19:00), with the **Garmin Datastore** connector enabled. It runs in the cloud; your PC
can be off.

---

Run my weekly training & recovery review for the last 7 days (ending today).

0. **Data check:** call `get_sync_status`. If the last successful sync is older than 12 h
   or days are missing, say so at the top and work with what's there.

1. **Training:** `list_activities` for the last 7 days and the 7 days before (for comparison).
   Summarise by sport (running, strength, team sports, other): sessions, total distance and
   time, training load, training-effect labels, RPE where logged. For the 1–2 hardest
   sessions, use `get_activity` (laps, HR zones) and `get_activity_streams` (HR and pace) to
   comment on pacing and HR drift. Compare total load with the prior week.

2. **Recovery:** `get_daily_wellness` for the 7 days. Report the trend in readiness (and
   morning readiness), sleep score and duration, overnight HRV vs. its baseline band,
   resting HR, Body Battery and stress. Name the best and worst recovery days and why.
   Use `get_trend` (weekly, last 8 weeks) for HRV, resting HR and sleep to put the week
   in context.

3. **Load vs. recovery:** acute vs. chronic load and ACWR from `get_daily_wellness`. Name
   red flags (hard sessions on low-readiness / below-baseline-HRV / poor-sleep days, ACWR
   > 1.3) and what went well.

4. **Fitness markers:** `get_performance` (no metric) for VO2max, race predictions and
   lactate threshold. Mention changes since last week if any.

5. **Health & nutrition:** 3–5 concrete actions for the coming week: protein and fuelling
   given strength work and body mass, hydration, and sleep (prioritise sleep hygiene if
   sleep scores trend Fair/Poor or REM is low).

6. **Next week's plan:** check `get_calendar` for anything already scheduled. Then give a
   day-by-day plan (Mon–Sun) with session type, intensity (HR zone or pace), and duration/
   distance, balancing running, strength and team sports, with recovery where the data
   says I need it.

Keep it tight and skimmable.
