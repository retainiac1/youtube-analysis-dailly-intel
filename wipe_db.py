"""Delete every row from swipefile.db while keeping the schema intact.

This empties all six tables (keeping table definitions) and resets the
AUTOINCREMENT counters so ids restart at 1. The database stays valid and ready
to use immediately — no re-init needed.

WARNING: swipefile.db is the dashboard seed. The `videos` table holds
hand-entered, unrecoverable columns (user_notes, starred, hook, first_10_sec,
saves). By default this script copies the DB to a timestamped backup before
wiping; pass --no-backup only against a genuinely throwaway database.

Usage:
    python wipe_db.py                 # prompt for confirmation, back up, wipe
    python wipe_db.py --force         # skip the prompt (still backs up)
    python wipe_db.py --no-backup     # skip the backup
"""

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone

import db
from config import DB_PATH

# Order is irrelevant: the schema declares no FOREIGN KEY constraints, so
# foreign_keys=ON is a no-op for DELETE. Fixed list for stable reporting.
TABLES = [
    "stats_snapshots",
    "rankings",
    "run_log",
    "quota_ledger",
    "videos",
    "channels",
]

# Tables with INTEGER PRIMARY KEY AUTOINCREMENT, whose counters live in
# sqlite_sequence and must be cleared so ids restart at 1.
AUTOINCREMENT_TABLES = ["stats_snapshots", "run_log"]


def count_rows(conn):
    """Return {table: row_count} for every table we wipe."""
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def backup_db(db_path):
    """Copy the DB file (plus -wal/-shm sidecars if present) to a timestamped
    file in a `backups/` subfolder beside the DB. Returns the backup path, or
    None if the DB file does not exist yet."""
    if not os.path.exists(db_path):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(backups_dir, exist_ok=True)
    backup_path = os.path.join(backups_dir, f"{os.path.basename(db_path)}.bak-{stamp}")
    shutil.copy2(db_path, backup_path)
    for suffix in ("-wal", "-shm"):
        sidecar = db_path + suffix
        if os.path.exists(sidecar):
            shutil.copy2(sidecar, backup_path + suffix)
    return backup_path


def wipe(conn):
    """Delete all rows and reset AUTOINCREMENT counters, atomically."""
    with db.transaction(conn):
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        # sqlite_sequence only exists once an AUTOINCREMENT table has held rows.
        has_seq = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone()
        if has_seq:
            placeholders = ",".join("?" for _ in AUTOINCREMENT_TABLES)
            conn.execute(
                f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})",
                AUTOINCREMENT_TABLES,
            )


def main():
    parser = argparse.ArgumentParser(
        description="Delete all data from swipefile.db (keeps the schema)."
    )
    parser.add_argument(
        "--force",
        "--yes",
        action="store_true",
        dest="force",
        help="Skip the typed confirmation prompt.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not back up the DB before wiping (throwaway DBs only).",
    )
    args = parser.parse_args()

    abs_path = os.path.abspath(DB_PATH)

    # Guarantee the schema exists so counts/deletes never hit a missing table.
    db.init_db(DB_PATH)

    conn = db.get_connection(DB_PATH)
    try:
        counts = count_rows(conn)
        total = sum(counts.values())

        print(f"Database: {abs_path}")
        for table in TABLES:
            print(f"  {table}: {counts[table]} rows")
        print(f"  total: {total} rows")

        if total == 0:
            print("Database is already empty. Nothing to do.")
            return

        if not args.force:
            answer = input(f"Type YES to delete all data from {abs_path}: ")
            if answer != "YES":
                print("Aborted.")
                return

        if not args.no_backup:
            backup_path = backup_db(DB_PATH)
            if backup_path:
                print(f"Backed up to: {os.path.abspath(backup_path)}")

        # run_with_db_retry is the OUTER wrapper so a transient lock rolls the
        # transaction back (inside wipe) before the retry re-fires.
        db.run_with_db_retry(lambda: wipe(conn))

        print(f"Deleted {total} rows across {len(TABLES)} tables. Database is now empty.")
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
