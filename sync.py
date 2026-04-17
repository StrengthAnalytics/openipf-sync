#!/usr/bin/env python3
"""
OpenIPF to Supabase Sync Script

Downloads the latest OpenIPF data and upserts it to Supabase.

Default (incremental) mode:
  Upserts every row within the rolling ROLLING_WINDOW_DAYS-day window.
  This picks up late-published meets (whose publication date lags the meet
  date by weeks) which the previous `date > latest_in_db` filter silently
  dropped.

--full mode:
  Upserts every row since MIN_DATE. Safe to run repeatedly because the
  unique index `lifter_records_natural_key_unique` (strength-hub migration
  096) makes the upsert idempotent — no duplicates regardless of how many
  times it runs. No explicit delete.

After a successful record upsert, the script calls
public.refresh_lifter_summary() so the lifter_summary aggregate reflects
new records immediately (see strength-hub migration 097). Previously a
separate Supabase cron rebuilt it on a Monday schedule; this sync now
owns that trigger.

Finally, each run writes its outcome (rows, duration, status, error) to
the `ingestion_metadata` table keyed by 'lifter_records' so the app and
ops can see when data was last refreshed.
"""

import os
import sys
import time
import zipfile
import logging
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from typing import Optional

import pandas as pd
import requests
from supabase import create_client, Client
from supabase.client import ClientOptions

# ─── Configuration ──────────────────────────────────────────────────────────

OPENIPF_ZIP_URL = "https://openpowerlifting.gitlab.io/opl-csv/files/openipf-latest.zip"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gjkzotolfunbvfgfcljh.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
TABLE_NAME = "lifter_records"
METADATA_TABLE_NAME = "ingestion_metadata"
REFRESH_SUMMARY_RPC = "refresh_lifter_summary"

# Oldest date we keep in the database.
MIN_DATE = date(2022, 1, 1)

# Incremental mode upserts every row within this rolling window. Chosen to
# comfortably cover OpenPowerlifting's meet-publication lag (which is usually
# a few weeks but can be months for small federations).
ROLLING_WINDOW_DAYS = 180

# Number of records to upsert per batch.
BATCH_SIZE = 1000

# Natural-key columns for ON CONFLICT upsert. Must match the unique index
# `lifter_records_natural_key_unique` defined in strength-hub migration 096.
# Result fields (total_kg, place, best3_*_kg) are deliberately NOT part of
# the key so OpenPowerlifting corrections flow through as UPDATEs.
NATURAL_KEY_COLUMNS = (
    "name,date,meet_name,federation,event,equipment,division,weight_class_kg"
)

# HTTP timeout for the Supabase client. Needs to be longer than the
# refresh_lifter_summary() execution time (currently ~90s on 1.4M rows).
POSTGREST_TIMEOUT_SECONDS = 300

# Column mapping from OpenIPF CSV to Supabase table.
COLUMN_MAPPING = {
    "Name": "name",
    "Sex": "sex",
    "Event": "event",
    "Equipment": "equipment",
    "Age": "age",
    "AgeClass": "age_class",
    "BirthYearClass": "birth_year_class",
    "Division": "division",
    "BodyweightKg": "bodyweight_kg",
    "WeightClassKg": "weight_class_kg",
    "Squat1Kg": "squat1_kg",
    "Squat2Kg": "squat2_kg",
    "Squat3Kg": "squat3_kg",
    "Squat4Kg": "squat4_kg",
    "Best3SquatKg": "best3_squat_kg",
    "Bench1Kg": "bench1_kg",
    "Bench2Kg": "bench2_kg",
    "Bench3Kg": "bench3_kg",
    "Bench4Kg": "bench4_kg",
    "Best3BenchKg": "best3_bench_kg",
    "Deadlift1Kg": "deadlift1_kg",
    "Deadlift2Kg": "deadlift2_kg",
    "Deadlift3Kg": "deadlift3_kg",
    "Deadlift4Kg": "deadlift4_kg",
    "Best3DeadliftKg": "best3_deadlift_kg",
    "TotalKg": "total_kg",
    "Place": "place",
    "Dots": "dots",
    "Wilks": "wilks",
    "Glossbrenner": "glossbrenner",
    "Goodlift": "goodlift",
    "Tested": "tested",
    "Country": "country",
    "State": "state",
    "Federation": "federation",
    "ParentFederation": "parent_federation",
    "Date": "date",
    "MeetCountry": "meet_country",
    "MeetState": "meet_state",
    "MeetName": "meet_name",
    "Sanctioned": "sanctioned",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─── Client ─────────────────────────────────────────────────────────────────

def get_supabase_client() -> Client:
    """Create a Supabase client with a long PostgREST timeout.

    The long timeout is needed because the sync calls
    public.refresh_lifter_summary() via RPC, which currently takes ~90s on
    the full dataset. The default httpx timeout would abort the request
    before the server finished, even though the rebuild would still have
    completed server-side.
    """
    if not SUPABASE_KEY:
        raise ValueError("SUPABASE_SERVICE_KEY environment variable is required")

    options = ClientOptions(postgrest_client_timeout=POSTGREST_TIMEOUT_SECONDS)
    return create_client(SUPABASE_URL, SUPABASE_KEY, options)


# ─── CSV load ───────────────────────────────────────────────────────────────

def download_and_extract_csv() -> pd.DataFrame:
    """Download the OpenIPF ZIP file and extract the CSV."""
    logger.info(f"Downloading OpenIPF data from {OPENIPF_ZIP_URL}")

    response = requests.get(OPENIPF_ZIP_URL, timeout=300)
    response.raise_for_status()

    logger.info("Download complete, extracting CSV...")

    with zipfile.ZipFile(BytesIO(response.content)) as zf:
        csv_files = [f for f in zf.namelist() if f.endswith(".csv")]
        if not csv_files:
            raise ValueError("No CSV file found in the ZIP archive")

        csv_filename = csv_files[0]
        logger.info(f"Found CSV file: {csv_filename}")

        with zf.open(csv_filename) as csv_file:
            df = pd.read_csv(csv_file, low_memory=False)

    logger.info(f"Loaded {len(df)} total records from CSV")
    return df


def transform_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Transform the CSV dataframe to match the database schema."""
    available_columns = [col for col in COLUMN_MAPPING.keys() if col in df.columns]
    df = df[available_columns].copy()
    df = df.rename(columns={k: v for k, v in COLUMN_MAPPING.items() if k in available_columns})

    # Parse dates and drop anything before MIN_DATE.
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d").dt.date
    df = df[df["date"] >= MIN_DATE]

    # Booleans.
    if "tested" in df.columns:
        df["tested"] = df["tested"].map({"Yes": True, "No": False, True: True, False: False})
    if "sanctioned" in df.columns:
        df["sanctioned"] = df["sanctioned"].map({"Yes": True, "No": False, True: True, False: False})

    # Normalise NaN / NaT to None for JSON serialisation.
    df = df.replace({pd.NA: None, pd.NaT: None})
    df = df.where(pd.notnull(df), None)

    # Coerce numeric columns.
    numeric_columns = [
        "age", "bodyweight_kg",
        "squat1_kg", "squat2_kg", "squat3_kg", "squat4_kg", "best3_squat_kg",
        "bench1_kg", "bench2_kg", "bench3_kg", "bench4_kg", "best3_bench_kg",
        "deadlift1_kg", "deadlift2_kg", "deadlift3_kg", "deadlift4_kg", "best3_deadlift_kg",
        "total_kg", "dots", "wilks", "glossbrenner", "goodlift",
    ]
    for col in numeric_columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def prepare_records_for_upsert(df: pd.DataFrame) -> list[dict]:
    """Convert a dataframe to JSON-serialisable records for Supabase upsert."""
    records = df.to_dict(orient="records")
    for record in records:
        for key, value in record.items():
            if isinstance(value, date):
                record[key] = value.isoformat()
            elif pd.isna(value):
                record[key] = None
    return records


# ─── Upsert ─────────────────────────────────────────────────────────────────

def upsert_records_batch(client: Client, records: list[dict]) -> int:
    """Upsert records in batches on the natural-key unique index.

    On batch failure, falls back to per-row upsert so one bad row doesn't
    sink the whole batch. The natural-key index makes this idempotent: a
    row already in the DB will be updated with any changed result fields,
    not duplicated.
    """
    total_upserted = 0

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        try:
            client.table(TABLE_NAME).upsert(batch, on_conflict=NATURAL_KEY_COLUMNS).execute()
            total_upserted += len(batch)
            logger.info(f"Upserted batch {batch_num}: {len(batch)} records (total: {total_upserted})")
        except Exception as batch_error:
            logger.error(f"Batch {batch_num} upsert failed: {batch_error}. Retrying row-by-row...")
            for j, record in enumerate(batch):
                try:
                    client.table(TABLE_NAME).upsert(record, on_conflict=NATURAL_KEY_COLUMNS).execute()
                    total_upserted += 1
                except Exception as row_error:
                    logger.error(
                        f"Failed row {i + j} ({record.get('name', 'unknown')} "
                        f"on {record.get('date', 'unknown')}): {row_error}"
                    )

    return total_upserted


# ─── Summary rebuild ────────────────────────────────────────────────────────

def refresh_lifter_summary(client: Client) -> None:
    """Trigger the lifter_summary aggregate rebuild via RPC.

    The function is defined in strength-hub migration 097. It returns
    (rows_upserted, duration_ms) and writes its own row to
    ingestion_metadata for lifter_summary, so we don't need to record
    anything extra here.
    """
    logger.info("Triggering lifter_summary rebuild...")
    result = client.rpc(REFRESH_SUMMARY_RPC).execute()
    if result.data:
        logger.info(f"lifter_summary rebuild: {result.data}")
    else:
        logger.warning("lifter_summary RPC returned no data")


# ─── Metadata ───────────────────────────────────────────────────────────────

def write_ingestion_metadata(
    client: Client,
    status: str,
    rows_inserted: int,
    duration_seconds: float,
    error: Optional[str] = None,
) -> None:
    """Upsert a run summary into ingestion_metadata. Never raises."""
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        client.table(METADATA_TABLE_NAME).upsert({
            "table_name": TABLE_NAME,
            "last_ingested_at": now_iso,
            "rows_inserted": rows_inserted,
            "rows_updated": 0,
            "rows_skipped": 0,
            "last_run_status": status,
            "last_error": error,
            "notes": f"openipf-sync run completed in {duration_seconds:.1f}s",
            "updated_at": now_iso,
        }, on_conflict="table_name").execute()
        logger.info(f"ingestion_metadata updated (status={status}, rows={rows_inserted})")
    except Exception as e:
        logger.error(f"Failed to write ingestion_metadata: {e}")


# ─── Main sync entry point ──────────────────────────────────────────────────

def sync(full: bool = False) -> int:
    """Upsert OpenIPF records to Supabase.

    full=False (default): upsert every row in the last ROLLING_WINDOW_DAYS days.
    full=True: upsert every row since MIN_DATE. Safe to run repeatedly
               thanks to the natural-key unique index.
    """
    mode_label = "FULL" if full else "incremental"
    logger.info(f"Starting OpenIPF -> Supabase sync ({mode_label} mode)")

    client = get_supabase_client()
    started_at = datetime.now(timezone.utc)
    status = "failure"
    rows_upserted = 0
    error_msg: Optional[str] = None

    try:
        df = download_and_extract_csv()
        df = transform_dataframe(df)
        logger.info(f"Records after MIN_DATE filter: {len(df)}")

        if not full:
            cutoff = date.today() - timedelta(days=ROLLING_WINDOW_DAYS)
            df = df[df["date"] >= cutoff]
            logger.info(f"Records in rolling {ROLLING_WINDOW_DAYS}-day window: {len(df)}")

        if len(df) == 0:
            logger.info("No records to upsert.")
        else:
            records = prepare_records_for_upsert(df)
            rows_upserted = upsert_records_batch(client, records)
            logger.info(f"Upserted {rows_upserted} records.")

        # Always refresh the summary — even on a zero-row run, corrections
        # elsewhere in the pipeline might have updated rows we didn't touch.
        refresh_lifter_summary(client)

        status = "success"
        logger.info(f"Sync complete. {rows_upserted} records upserted ({mode_label} mode).")
        return rows_upserted

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)[:500]}"
        logger.exception("Sync failed")
        raise
    finally:
        duration_s = (datetime.now(timezone.utc) - started_at).total_seconds()
        write_ingestion_metadata(
            client,
            status=status,
            rows_inserted=rows_upserted,
            duration_seconds=duration_s,
            error=error_msg,
        )


if __name__ == "__main__":
    full_mode = len(sys.argv) > 1 and sys.argv[1] == "--full"
    sync(full=full_mode)
