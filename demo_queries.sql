-- demo_queries.sql — four queries to run live against the telemetry database.
--
--   hotdata databases query -d $HOTDATA_TELEMETRY_DB_ID "<query>" -o table
--
-- Paste the SELECT only, NOT the -- comment above it: the CLI's argument parser
-- sees a string starting with "--" as a flag and rejects it. (A "--" separator
-- does not help; it is consumed as the SQL argument.) All four were verified
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
