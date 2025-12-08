-- Optional: Create this function in Supabase for faster summary regeneration
-- Run this SQL in Supabase SQL Editor (one-time setup)
-- The sync script will use this function if available, otherwise falls back to Python

CREATE OR REPLACE FUNCTION regenerate_lifter_summary()
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    -- Clear existing summaries
    DELETE FROM lifter_summary;

    -- Insert regenerated summaries
    INSERT INTO lifter_summary (
        name,
        sex,
        country,
        total_competitions,
        first_competition_date,
        last_competition_date,
        best_squat_kg,
        best_squat_date,
        best_squat_meet,
        best_bench_kg,
        best_bench_date,
        best_bench_meet,
        best_deadlift_kg,
        best_deadlift_date,
        best_deadlift_meet,
        best_total_kg,
        best_total_date,
        best_total_meet,
        weight_classes,
        equipment_types,
        updated_at
    )
    SELECT
        lr.name,
        (ARRAY_AGG(lr.sex ORDER BY lr.date DESC))[1] as sex,
        (ARRAY_AGG(lr.country ORDER BY lr.date DESC))[1] as country,
        COUNT(*) as total_competitions,
        MIN(lr.date) as first_competition_date,
        MAX(lr.date) as last_competition_date,
        -- Best squat
        MAX(lr.best3_squat_kg) as best_squat_kg,
        (SELECT date FROM lifter_records lr2
         WHERE lr2.name = lr.name AND lr2.best3_squat_kg = MAX(lr.best3_squat_kg)
         LIMIT 1) as best_squat_date,
        (SELECT meet_name FROM lifter_records lr2
         WHERE lr2.name = lr.name AND lr2.best3_squat_kg = MAX(lr.best3_squat_kg)
         LIMIT 1) as best_squat_meet,
        -- Best bench
        MAX(lr.best3_bench_kg) as best_bench_kg,
        (SELECT date FROM lifter_records lr2
         WHERE lr2.name = lr.name AND lr2.best3_bench_kg = MAX(lr.best3_bench_kg)
         LIMIT 1) as best_bench_date,
        (SELECT meet_name FROM lifter_records lr2
         WHERE lr2.name = lr.name AND lr2.best3_bench_kg = MAX(lr.best3_bench_kg)
         LIMIT 1) as best_bench_meet,
        -- Best deadlift
        MAX(lr.best3_deadlift_kg) as best_deadlift_kg,
        (SELECT date FROM lifter_records lr2
         WHERE lr2.name = lr.name AND lr2.best3_deadlift_kg = MAX(lr.best3_deadlift_kg)
         LIMIT 1) as best_deadlift_date,
        (SELECT meet_name FROM lifter_records lr2
         WHERE lr2.name = lr.name AND lr2.best3_deadlift_kg = MAX(lr.best3_deadlift_kg)
         LIMIT 1) as best_deadlift_meet,
        -- Best total
        MAX(lr.total_kg) as best_total_kg,
        (SELECT date FROM lifter_records lr2
         WHERE lr2.name = lr.name AND lr2.total_kg = MAX(lr.total_kg)
         LIMIT 1) as best_total_date,
        (SELECT meet_name FROM lifter_records lr2
         WHERE lr2.name = lr.name AND lr2.total_kg = MAX(lr.total_kg)
         LIMIT 1) as best_total_meet,
        -- Arrays
        JSONB_AGG(DISTINCT lr.weight_class_kg) FILTER (WHERE lr.weight_class_kg IS NOT NULL) as weight_classes,
        JSONB_AGG(DISTINCT lr.equipment) FILTER (WHERE lr.equipment IS NOT NULL) as equipment_types,
        NOW() as updated_at
    FROM lifter_records lr
    GROUP BY lr.name;
END;
$$;

-- Grant execute permission to authenticated users (adjust as needed)
GRANT EXECUTE ON FUNCTION regenerate_lifter_summary() TO service_role;
