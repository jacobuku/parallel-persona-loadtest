-- demo_queries.sql — five queries to run live against the telemetry database.
--
--   hotdata databases query -d $HOTDATA_TELEMETRY_DB_ID "<query>" -o table
--
-- Paste the SELECT only, NOT the -- comment above it: the CLI's argument parser
-- sees a string starting with "--" as a flag and rejects it. (A "--" separator
-- does not help; it is consumed as the SQL argument.) All five were verified
-- against the live database with the comments stripped.
--
-- Table: loadtest_telemetry.public.persona_runs — one row per persona turn,
-- written by run_v1.py. (loadtest_telemetry.public.runs holds the earlier
-- serial/A/B architecture benchmark.)


-- 1. The headline: what changing two lines of the prompt bought us.
--    v2 fixes both v1 failures' symptoms and cuts the average reply by a quarter.
SELECT prompt_version,
       COUNT(*)                                      AS turns,
       SUM(CASE WHEN rule_pass THEN 1 ELSE 0 END)    AS rule_pass,
       SUM(CASE WHEN llm_pass  THEN 1 ELSE 0 END)    AS llm_pass,
       SUM(CASE WHEN agree     THEN 1 ELSE 0 END)    AS agree,
       ROUND(AVG(reply_words), 1)                    AS avg_words,
       MAX(reply_words)                              AS max_words,
       ROUND(MAX(wall_s), 1)                         AS wall_clock_s,
       ROUND(AVG(pipeline_s), 1)                     AS avg_pipeline_s
FROM loadtest_telemetry.public.persona_runs
GROUP BY prompt_version
ORDER BY prompt_version;


-- 2. Per persona, who actually moved — the pass-rate number above is 8 stories,
--    only p5 changed verdict. Every other persona just got shorter.
SELECT v1.persona_id,
       v1.persona_type,
       v1.llm_pass     AS v1_llm_pass,
       v2.llm_pass     AS v2_llm_pass,
       v1.reply_words  AS v1_words,
       v2.reply_words  AS v2_words
FROM      (SELECT * FROM loadtest_telemetry.public.persona_runs WHERE prompt_version = 'v1') v1
JOIN      (SELECT * FROM loadtest_telemetry.public.persona_runs WHERE prompt_version = 'v2') v2
       ON v1.persona_id = v2.persona_id
ORDER BY v1.persona_id;


-- 3. Why there are two verdicts and why must_not_contain never fails a turn:
--    every row here is a case the cheap rules alone would have got wrong.
--    p3/p4 flags fired on correct replies ("can't promise compensation").
--    v2 p7 passed every rule and still failed the judge, on ordering the
--    rules cannot see.
SELECT prompt_version, persona_id, rule_pass, llm_pass, agree, rule_flags, llm_reason
FROM loadtest_telemetry.public.persona_runs
WHERE agree = false OR rule_flags <> ''
ORDER BY prompt_version, persona_id;


-- 4. The concurrency claim, from the timings themselves: 8 turns that would take
--    ~440s one at a time finish in the time of the slowest single turn.
SELECT prompt_version,
       COUNT(*)                          AS turns,
       MAX(concurrency_effective)        AS concurrency,
       ROUND(SUM(wall_s), 1)             AS one_at_a_time_s,
       ROUND(MAX(wall_s), 1)             AS actual_wall_clock_s,
       ROUND(SUM(wall_s) / MAX(wall_s), 1) AS speedup,
       ROUND(SUM(db_total_s), 1)         AS hotdata_lifecycle_s,
       SUM(queries_run)                  AS queries,
       SUM(rows_retrieved)               AS rows_read,
       SUM(empty_results)                AS empty_results
FROM loadtest_telemetry.public.persona_runs
GROUP BY prompt_version
ORDER BY prompt_version;


-- 5. Handoff rate per persona: who got punted to the events manager, v1 vs v2.
--    7/8 in both versions -- p2 is the only persona the agent answers on its own,
--    and the v1 -> v2 prompt change moved word counts, not handoffs.
--
--    The flags are literals, not a column: persona_runs stores reply_words but not
--    the reply text (run_v1.py writes the reply only into the persona's own
--    throwaway database, which is dropped at the end of the turn), so "did it hand
--    off" cannot be computed in SQL. They are read off transcripts_v1.md /
--    transcripts_v2.md, where every handoff reply contains both "events manager"
--    and "email you today". Joining them to persona_runs still makes the row live:
--    the word counts and the 2-rows-per-persona coverage come from the table, so a
--    persona missing from a run, or a re-run with different replies, shows up here.
SELECT h.persona_id,
       h.persona_type,
       h.v1_handoff,
       h.v2_handoff,
       h.deferred_question,
       MAX(CASE WHEN r.prompt_version = 'v1' THEN r.reply_words END) AS v1_words,
       MAX(CASE WHEN r.prompt_version = 'v2' THEN r.reply_words END) AS v2_words,
       COUNT(*)                                                      AS rows_matched
FROM (VALUES
        ('p1', 'price_ceiling',    true,  true,  'whether any discount exists'),
        ('p2', 'corporate_multi',  false, false, '(none - answered in full)'),
        ('p3', 'broken_promise',   true,  true,  'refund + written response'),
        ('p4', 'burned_before',    true,  true,  'business licence + signed terms'),
        ('p5', 'urgent_blocked',   true,  true,  'is 9/19 available'),
        ('p6', 'byob_gap',         true,  true,  'corkage / BYOB policy'),
        ('p7', 'window_shopper',   true,  true,  'cancellation policy + discounts'),
        ('p8', 'angry_escalation', true,  true,  'parking complaint + remedy')
     ) AS h(persona_id, persona_type, v1_handoff, v2_handoff, deferred_question)
JOIN loadtest_telemetry.public.persona_runs r ON r.persona_id = h.persona_id
GROUP BY h.persona_id, h.persona_type, h.v1_handoff, h.v2_handoff, h.deferred_question
ORDER BY h.persona_id;
