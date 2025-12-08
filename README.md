# OpenIPF to Supabase Sync

Automatically syncs powerlifting data from [OpenIPF](https://www.openipf.org/) to a Supabase database.

## Features

- **Weekly automated sync** via GitHub Actions (runs every Sunday at 6:00 AM UTC)
- **Incremental updates** - only syncs new records since the last sync
- **Full resync option** - manually trigger a complete data refresh
- **Date filtering** - only includes records from January 1, 2022 onwards
- **Automatic summary regeneration** - updates `lifter_summary` table after each sync

## Setup

### 1. Configure GitHub Secrets

Go to your repository's **Settings > Secrets and variables > Actions** and add:

| Secret Name | Description |
|-------------|-------------|
| `SUPABASE_URL` | Your Supabase project URL (e.g., `https://xxxxx.supabase.co`) |
| `SUPABASE_SERVICE_KEY` | Your Supabase service role key (found in Settings > API) |

**Important:** Use the `service_role` key, not the `anon` key, as we need write access.

### 2. Database Tables

The sync expects two tables in your Supabase database:

#### `lifter_records` (main data table)

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
    squat1_kg NUMERIC,
    squat2_kg NUMERIC,
    squat3_kg NUMERIC,
    squat4_kg NUMERIC,
    best3_squat_kg NUMERIC,
    bench1_kg NUMERIC,
    bench2_kg NUMERIC,
    bench3_kg NUMERIC,
    bench4_kg NUMERIC,
    best3_bench_kg NUMERIC,
    deadlift1_kg NUMERIC,
    deadlift2_kg NUMERIC,
    deadlift3_kg NUMERIC,
    deadlift4_kg NUMERIC,
    best3_deadlift_kg NUMERIC,
    total_kg NUMERIC,
    place TEXT,
    dots NUMERIC,
    wilks NUMERIC,
    glossbrenner NUMERIC,
    goodlift NUMERIC,
    tested BOOLEAN,
    country TEXT,
    state TEXT,
    federation TEXT,
    parent_federation TEXT,
    date DATE NOT NULL,
    meet_country TEXT,
    meet_state TEXT,
    meet_name TEXT,
    sanctioned BOOLEAN,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### `lifter_summary` (aggregated per-lifter stats)

This table is automatically regenerated after each sync with:
- Best squat/bench/deadlift/total (with dates and meet names)
- Total competition count
- First and last competition dates
- Weight classes and equipment types used

### 3. (Optional) Install Database Function

For faster summary regeneration, you can install the SQL function in `sql/regenerate_summary_function.sql`. Run it once in the Supabase SQL Editor. If not installed, the sync will fall back to Python-based regeneration.

## Usage

### Automatic Sync (Recommended)

The GitHub Action runs automatically every Sunday at 6:00 AM UTC. No action required once secrets are configured.

### Manual Sync

1. Go to **Actions** tab in your repository
2. Select **OpenIPF Sync** workflow
3. Click **Run workflow**
4. Choose whether to do a full resync or incremental sync

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export SUPABASE_URL="https://your-project.supabase.co"
export SUPABASE_SERVICE_KEY="your-service-role-key"

# Run incremental sync
python sync.py

# Run full resync (clears all data first)
python sync.py --full
```

## Backup Before Testing

Before running the sync for the first time, it's recommended to backup your tables:

### Option 1: Export to CSV (via Supabase Dashboard)
1. Go to Table Editor
2. Select your table
3. Click Export > Export to CSV

### Option 2: Create Backup Table (via SQL Editor)
```sql
-- Backup lifter_records
CREATE TABLE lifter_records_backup AS SELECT * FROM lifter_records;

-- Backup lifter_summary
CREATE TABLE lifter_summary_backup AS SELECT * FROM lifter_summary;
```

### Restore from Backup
```sql
-- Restore lifter_records (if needed)
DELETE FROM lifter_records;
INSERT INTO lifter_records SELECT * FROM lifter_records_backup;

-- Restore lifter_summary (if needed)
DELETE FROM lifter_summary;
INSERT INTO lifter_summary SELECT * FROM lifter_summary_backup;
```

## Data Source

Data is sourced from the [OpenPowerlifting](https://openpowerlifting.gitlab.io/opl-csv/bulk-csv.html) project, specifically the `openipf-latest.zip` file which contains IPF-affiliated federation data.

## How It Works

1. Downloads the latest `openipf-latest.zip` from OpenPowerlifting
2. Extracts and parses the CSV file
3. Filters records to only include dates >= 2022-01-01
4. Queries the database for the latest date already stored
5. Inserts only records with dates newer than what's in the database
6. Regenerates the `lifter_summary` table with updated aggregations
7. Uses batch inserts (1000 records per batch) for efficiency

## License

This project uses data from OpenPowerlifting, which is contributed to the Public Domain.
