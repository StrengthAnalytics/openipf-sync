# OpenIPF to Supabase Sync

Automatically syncs powerlifting data from [OpenIPF](https://www.openipf.org/) (via the [OpenPowerlifting](https://openpowerlifting.gitlab.io/opl-csv/bulk-csv.html) `openipf-latest.zip` dump) to a Supabase database.

Consumed by the Strength Hub application.

## Features

- **Weekly automated sync** via GitHub Actions (runs every Sunday at 06:00 UTC).
- **Idempotent upsert** on a natural-key unique index — safe to re-run, safe to run concurrently, safe to bulk-reload.
- **Rolling-window incremental mode** (180 days) so late-published meets are picked up rather than silently dropped.
- **Full resync** option for one-off backfills.
- **Date filtering** — only includes records from 2022-01-01 onwards.
- **Automatic summary rebuild** — calls `public.refresh_lifter_summary()` after each run so `lifter_summary` is always fresh.
- **Run metadata** — writes each run's outcome (rows upserted, duration, status, last error) to `ingestion_metadata` so the app and ops can see sync health.

## Prerequisites

This sync depends on schema objects defined in the [Strength Hub](https://github.com/StrengthAnalytics/Strength-Hub) repository:

| Object | Migration | Purpose |
|--------|-----------|---------|
| `lifter_records` table | (bulk-loaded; schema in this README below) | Target table |
| `lifter_records_natural_key_unique` index | `096_lifter_records_dedup_and_unique.sql` | Enables `ON CONFLICT` upsert; prevents duplicates |
| `ingestion_metadata` table | `096_lifter_records_dedup_and_unique.sql` | Run outcome log |
| `public.refresh_lifter_summary()` | `097_refresh_lifter_summary_function.sql` | Rebuilds `lifter_summary` aggregate |

Migrations 096 and 097 **must** be applied before running this sync.

## Setup

### 1. Configure GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|--------|-------------|
| `SUPABASE_URL` | Project URL (e.g. `https://xxxxx.supabase.co`) |
| `SUPABASE_SERVICE_KEY` | `service_role` key (Settings → API). Needed for write access. |

### 2. Database tables

The sync expects `lifter_records` with the schema:

```sql
CREATE TABLE lifter_records (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    sex TEXT,
    event TEXT,
    equipment TEXT,
    age NUMERIC,
    age_class TEXT,
    birth_year_class TEXT,
    division TEXT,
    bodyweight_kg NUMERIC,
    weight_class_kg TEXT,
    squat1_kg NUMERIC, squat2_kg NUMERIC, squat3_kg NUMERIC, squat4_kg NUMERIC, best3_squat_kg NUMERIC,
    bench1_kg NUMERIC, bench2_kg NUMERIC, bench3_kg NUMERIC, bench4_kg NUMERIC, best3_bench_kg NUMERIC,
    deadlift1_kg NUMERIC, deadlift2_kg NUMERIC, deadlift3_kg NUMERIC, deadlift4_kg NUMERIC, best3_deadlift_kg NUMERIC,
    total_kg NUMERIC,
    place TEXT,
    dots NUMERIC, wilks NUMERIC, glossbrenner NUMERIC, goodlift NUMERIC,
    tested BOOLEAN,
    country TEXT, state TEXT,
    federation TEXT, parent_federation TEXT,
    date DATE NOT NULL,
    meet_country TEXT, meet_state TEXT, meet_name TEXT,
    sanctioned BOOLEAN,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

Plus the unique index, `ingestion_metadata` table, and `refresh_lifter_summary()` function — all created by the migrations in the Strength Hub repo.

## How it works

1. Downloads the latest `openipf-latest.zip` from OpenPowerlifting.
2. Extracts and parses the CSV file.
3. Filters records to dates >= 2022-01-01.
4. Further filters to the rolling 180-day window (incremental) or keeps everything (full).
5. Upserts records on the natural key `(name, date, meet_name, federation, event, equipment, division, weight_class_kg)` in batches of 1,000.
   - Existing rows get updated with the latest result values — this is how OpenPowerlifting corrections (retroactive DQs, reviewed lifts, place changes) flow through.
   - New rows get inserted.
   - The `NULLS NOT DISTINCT` unique index ensures rows with NULL division / weight_class don't bypass the constraint.
6. Calls `public.refresh_lifter_summary()` so `lifter_summary` reflects the new/updated records.
7. Writes the run outcome to `ingestion_metadata` with `table_name = 'lifter_records'`.

## Incremental vs full

| Mode | When to use | What it upserts |
|------|-------------|-----------------|
| Incremental (default) | Weekly scheduled runs | Last 180 days of records |
| `--full` | One-off backfill, schema changes, correcting historic gaps | All records since `MIN_DATE` (2022-01-01) |

Both modes are safe to run repeatedly — the unique index guarantees no duplicates.

## Why a rolling window instead of "date > latest_in_db"?

Previously the sync skipped any row with `date <= latest_in_db`. But OpenPowerlifting does not publish results in strict date order — a meet held on 2026-01-25 might be published weeks later, by which time `latest_in_db` is already in February, causing the January meet to be silently dropped forever. The rolling 180-day window upserts everything recent on every run, so late publications are always picked up.

## Usage

### Scheduled run
The GitHub Action fires every Sunday at 06:00 UTC. No action required once secrets are configured.

### Manual trigger
1. Actions → OpenIPF Sync → Run workflow.
2. Choose full resync or incremental.

### Local development
```bash
pip install -r requirements.txt

export SUPABASE_URL="https://your-project.supabase.co"
export SUPABASE_SERVICE_KEY="your-service-role-key"

python sync.py          # incremental (180-day window)
python sync.py --full   # full, all records since 2022-01-01
```

## Monitoring sync health

Query the `ingestion_metadata` table:

```sql
SELECT * FROM ingestion_metadata WHERE table_name = 'lifter_records';
```

Look for:
- `last_ingested_at` — how fresh is the data
- `last_run_status` — `success` / `failure`
- `last_error` — set on failure
- `rows_inserted` — rows touched in the most recent run
- `notes` — includes duration

## Data source

Data is sourced from the [OpenPowerlifting project](https://openpowerlifting.gitlab.io/opl-csv/bulk-csv.html), specifically the `openipf-latest.zip` which contains IPF-affiliated federation data. OpenPowerlifting data is contributed to the Public Domain.
