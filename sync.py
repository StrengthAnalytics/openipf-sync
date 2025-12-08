#!/usr/bin/env python3
"""
OpenIPF to Supabase Sync Script

Downloads the latest OpenIPF data and syncs new records to Supabase.
Only processes records from 2022-01-01 onwards.
"""

import os
import sys
import zipfile
import tempfile
import logging
from datetime import date, datetime
from io import BytesIO
from typing import Optional

import pandas as pd
import requests
from supabase import create_client, Client

# Configuration
OPENIPF_ZIP_URL = "https://openpowerlifting.gitlab.io/opl-csv/openipf-latest.zip"
CSV_FILENAME = "openipf-latest.csv"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gjkzotolfunbvfgfcljh.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
TABLE_NAME = "lifter_records"
SUMMARY_TABLE_NAME = "lifter_summary"
MIN_DATE = date(2022, 1, 1)
BATCH_SIZE = 1000  # Number of records to insert per batch

# Column mapping from OpenIPF CSV to Supabase table
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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def download_and_extract_csv() -> pd.DataFrame:
    """Download the OpenIPF ZIP file and extract the CSV."""
    logger.info(f"Downloading OpenIPF data from {OPENIPF_ZIP_URL}")

    response = requests.get(OPENIPF_ZIP_URL, timeout=300)
    response.raise_for_status()

    logger.info("Download complete, extracting CSV...")

    with zipfile.ZipFile(BytesIO(response.content)) as zf:
        # Find the CSV file in the archive
        csv_files = [f for f in zf.namelist() if f.endswith('.csv')]
        if not csv_files:
            raise ValueError("No CSV file found in the ZIP archive")

        csv_filename = csv_files[0]
        logger.info(f"Found CSV file: {csv_filename}")

        with zf.open(csv_filename) as csv_file:
            df = pd.read_csv(csv_file, low_memory=False)

    logger.info(f"Loaded {len(df)} total records from CSV")
    return df


def get_supabase_client() -> Client:
    """Create and return a Supabase client."""
    if not SUPABASE_KEY:
        raise ValueError("SUPABASE_SERVICE_KEY environment variable is required")

    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_latest_date_in_db(client: Client) -> Optional[date]:
    """Get the most recent date from the database."""
    try:
        result = client.table(TABLE_NAME).select("date").order("date", desc=True).limit(1).execute()

        if result.data and len(result.data) > 0:
            latest_date_str = result.data[0]["date"]
            latest_date = datetime.strptime(latest_date_str, "%Y-%m-%d").date()
            logger.info(f"Latest date in database: {latest_date}")
            return latest_date
    except Exception as e:
        logger.warning(f"Could not get latest date from database: {e}")

    return None


def transform_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Transform the CSV dataframe to match the database schema."""
    # Only keep columns that exist in both CSV and mapping
    available_columns = [col for col in COLUMN_MAPPING.keys() if col in df.columns]
    df = df[available_columns].copy()

    # Rename columns to match database schema
    df = df.rename(columns={k: v for k, v in COLUMN_MAPPING.items() if k in available_columns})

    # Convert Date column to proper date format
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d").dt.date

    # Filter for dates >= MIN_DATE
    df = df[df["date"] >= MIN_DATE]

    # Convert Tested and Sanctioned to boolean
    if "tested" in df.columns:
        df["tested"] = df["tested"].map({"Yes": True, "No": False, True: True, False: False})

    if "sanctioned" in df.columns:
        df["sanctioned"] = df["sanctioned"].map({"Yes": True, "No": False, True: True, False: False})

    # Replace NaN with None for proper JSON serialization
    df = df.replace({pd.NA: None, pd.NaT: None})
    df = df.where(pd.notnull(df), None)

    # Convert numeric columns - handle empty strings and NaN
    numeric_columns = [
        "age", "bodyweight_kg",
        "squat1_kg", "squat2_kg", "squat3_kg", "squat4_kg", "best3_squat_kg",
        "bench1_kg", "bench2_kg", "bench3_kg", "bench4_kg", "best3_bench_kg",
        "deadlift1_kg", "deadlift2_kg", "deadlift3_kg", "deadlift4_kg", "best3_deadlift_kg",
        "total_kg", "dots", "wilks", "glossbrenner", "goodlift"
    ]

    for col in numeric_columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def prepare_records_for_insert(df: pd.DataFrame) -> list[dict]:
    """Convert dataframe to list of dictionaries for Supabase insert."""
    records = df.to_dict(orient="records")

    # Clean up records - convert date objects to strings, handle NaN
    for record in records:
        for key, value in record.items():
            if isinstance(value, date):
                record[key] = value.isoformat()
            elif pd.isna(value):
                record[key] = None

    return records


def insert_records_batch(client: Client, records: list[dict]) -> int:
    """Insert records in batches."""
    total_inserted = 0

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        try:
            client.table(TABLE_NAME).insert(batch).execute()
            total_inserted += len(batch)
            logger.info(f"Inserted batch {i // BATCH_SIZE + 1}: {len(batch)} records (total: {total_inserted})")
        except Exception as e:
            logger.error(f"Error inserting batch {i // BATCH_SIZE + 1}: {e}")
            # Try inserting records one by one to identify problematic records
            for j, record in enumerate(batch):
                try:
                    client.table(TABLE_NAME).insert(record).execute()
                    total_inserted += 1
                except Exception as record_error:
                    logger.error(f"Failed to insert record {i + j}: {record.get('name', 'unknown')} on {record.get('date', 'unknown')}: {record_error}")

    return total_inserted


def regenerate_lifter_summary(client: Client) -> int:
    """
    Regenerate the lifter_summary table by calling the database function.
    Falls back to Python-based regeneration if the function doesn't exist.
    """
    logger.info("Regenerating lifter_summary table...")

    try:
        # Try calling the database function (most efficient)
        client.rpc("regenerate_lifter_summary").execute()
        logger.info("lifter_summary regenerated via database function")

        # Get count of summaries
        result = client.table(SUMMARY_TABLE_NAME).select("id", count="exact").execute()
        count = result.count if result.count else 0
        logger.info(f"lifter_summary now contains {count} lifter summaries")
        return count

    except Exception as e:
        logger.warning(f"Database function not available ({e}), using Python fallback...")
        return regenerate_lifter_summary_python(client)


def regenerate_lifter_summary_python(client: Client) -> int:
    """
    Regenerate lifter_summary using Python/pandas.
    This is a fallback if the database function isn't set up.
    """
    logger.info("Fetching all records for summary calculation...")

    # Fetch all records (paginated)
    all_records = []
    page_size = 10000
    offset = 0

    while True:
        result = client.table(TABLE_NAME).select("*").range(offset, offset + page_size - 1).execute()
        if not result.data:
            break
        all_records.extend(result.data)
        offset += page_size
        logger.info(f"Fetched {len(all_records)} records...")
        if len(result.data) < page_size:
            break

    if not all_records:
        logger.warning("No records found in lifter_records")
        return 0

    logger.info(f"Processing {len(all_records)} records...")
    df = pd.DataFrame(all_records)

    # Convert date strings to dates for comparison
    df["date"] = pd.to_datetime(df["date"]).dt.date

    # Group by lifter name and calculate summaries
    summaries = []

    for name, group in df.groupby("name"):
        # Get the first record for basic info (sex, country)
        first_record = group.iloc[0]

        summary = {
            "name": name,
            "sex": first_record.get("sex"),
            "country": first_record.get("country"),
            "total_competitions": len(group),
            "first_competition_date": group["date"].min().isoformat() if pd.notna(group["date"].min()) else None,
            "last_competition_date": group["date"].max().isoformat() if pd.notna(group["date"].max()) else None,
        }

        # Best squat
        squat_records = group[group["best3_squat_kg"].notna() & (group["best3_squat_kg"] > 0)]
        if not squat_records.empty:
            best_squat_idx = squat_records["best3_squat_kg"].idxmax()
            best_squat_row = group.loc[best_squat_idx]
            summary["best_squat_kg"] = float(best_squat_row["best3_squat_kg"])
            summary["best_squat_date"] = best_squat_row["date"].isoformat() if pd.notna(best_squat_row["date"]) else None
            summary["best_squat_meet"] = best_squat_row.get("meet_name")

        # Best bench
        bench_records = group[group["best3_bench_kg"].notna() & (group["best3_bench_kg"] > 0)]
        if not bench_records.empty:
            best_bench_idx = bench_records["best3_bench_kg"].idxmax()
            best_bench_row = group.loc[best_bench_idx]
            summary["best_bench_kg"] = float(best_bench_row["best3_bench_kg"])
            summary["best_bench_date"] = best_bench_row["date"].isoformat() if pd.notna(best_bench_row["date"]) else None
            summary["best_bench_meet"] = best_bench_row.get("meet_name")

        # Best deadlift
        deadlift_records = group[group["best3_deadlift_kg"].notna() & (group["best3_deadlift_kg"] > 0)]
        if not deadlift_records.empty:
            best_dl_idx = deadlift_records["best3_deadlift_kg"].idxmax()
            best_dl_row = group.loc[best_dl_idx]
            summary["best_deadlift_kg"] = float(best_dl_row["best3_deadlift_kg"])
            summary["best_deadlift_date"] = best_dl_row["date"].isoformat() if pd.notna(best_dl_row["date"]) else None
            summary["best_deadlift_meet"] = best_dl_row.get("meet_name")

        # Best total
        total_records = group[group["total_kg"].notna() & (group["total_kg"] > 0)]
        if not total_records.empty:
            best_total_idx = total_records["total_kg"].idxmax()
            best_total_row = group.loc[best_total_idx]
            summary["best_total_kg"] = float(best_total_row["total_kg"])
            summary["best_total_date"] = best_total_row["date"].isoformat() if pd.notna(best_total_row["date"]) else None
            summary["best_total_meet"] = best_total_row.get("meet_name")

        # Weight classes (unique, as JSON array)
        weight_classes = group["weight_class_kg"].dropna().unique().tolist()
        summary["weight_classes"] = weight_classes if weight_classes else None

        # Equipment types (unique, as JSON array)
        equipment_types = group["equipment"].dropna().unique().tolist()
        summary["equipment_types"] = equipment_types if equipment_types else None

        # Timestamps
        summary["updated_at"] = datetime.utcnow().isoformat()

        summaries.append(summary)

    logger.info(f"Calculated {len(summaries)} lifter summaries")

    # Clear existing summaries
    logger.info("Clearing existing lifter_summary records...")
    client.table(SUMMARY_TABLE_NAME).delete().neq("id", 0).execute()

    # Insert new summaries in batches
    logger.info("Inserting new summaries...")
    total_inserted = 0
    for i in range(0, len(summaries), BATCH_SIZE):
        batch = summaries[i:i + BATCH_SIZE]
        try:
            client.table(SUMMARY_TABLE_NAME).insert(batch).execute()
            total_inserted += len(batch)
            logger.info(f"Inserted summary batch {i // BATCH_SIZE + 1}: {len(batch)} summaries")
        except Exception as e:
            logger.error(f"Error inserting summary batch: {e}")

    logger.info(f"lifter_summary regenerated with {total_inserted} summaries")
    return total_inserted


def sync():
    """Main sync function."""
    logger.info("Starting OpenIPF to Supabase sync")

    # Initialize Supabase client
    client = get_supabase_client()

    # Get the latest date in the database
    latest_db_date = get_latest_date_in_db(client)

    # Download and extract CSV
    df = download_and_extract_csv()

    # Transform dataframe
    df = transform_dataframe(df)
    logger.info(f"Records after filtering (>= {MIN_DATE}): {len(df)}")

    # Filter for new records only (dates after the latest in DB)
    if latest_db_date:
        df = df[df["date"] > latest_db_date]
        logger.info(f"New records to sync (> {latest_db_date}): {len(df)}")
    else:
        logger.info("No existing data in database, will sync all records")

    if len(df) == 0:
        logger.info("No new records to sync")
        return 0

    # Prepare and insert records
    records = prepare_records_for_insert(df)
    inserted_count = insert_records_batch(client, records)

    # Regenerate lifter summary table
    if inserted_count > 0:
        regenerate_lifter_summary(client)

    logger.info(f"Sync complete! Inserted {inserted_count} new records")
    return inserted_count


def full_resync():
    """
    Perform a full resync by clearing the table and reloading all data.
    Use with caution!
    """
    logger.warning("Starting FULL RESYNC - this will replace all data!")

    client = get_supabase_client()

    # Download and extract CSV
    df = download_and_extract_csv()

    # Transform dataframe
    df = transform_dataframe(df)
    logger.info(f"Records after filtering (>= {MIN_DATE}): {len(df)}")

    # Delete all existing records
    logger.warning("Deleting all existing records...")
    client.table(TABLE_NAME).delete().gte("date", "2000-01-01").execute()

    # Prepare and insert records
    records = prepare_records_for_insert(df)
    inserted_count = insert_records_batch(client, records)

    # Regenerate lifter summary table
    regenerate_lifter_summary(client)

    logger.info(f"Full resync complete! Inserted {inserted_count} records")
    return inserted_count


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--full":
        full_resync()
    else:
        sync()
