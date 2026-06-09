"""Back up the live swipefile.db to the cloud-synced folder (one command).

Run from the project directory:
    python backup_database.py

Creates a consistent, timestamped snapshot of the database using SQLite's
online backup API (safe even if the app is mid-write, and folds in any WAL),
writes it to the iCloud-synced backups folder, then re-opens the snapshot to
verify it before declaring success. Override the destination with --dest.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

from config import DB_PATH

# Cloud-synced destination. This is the REAL path of the iCloud-synced Documents
# folder. Note: Finder *displays* ~/Documents as "Documents - <Mac name>", but
# that label is NOT a real subfolder — using it as a path creates a phantom dir.
DEFAULT_DEST_DIR = os.path.expanduser("~/Documents/database-backups")

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


class BackupError(Exception):
    """A backup failed in a way we can explain to the user (no traceback)."""


def resolve_db_path():
    """DB_PATH is relative to the project root; resolve it so this script works
    no matter which directory it is invoked from."""
    return DB_PATH if os.path.isabs(DB_PATH) else os.path.join(PROJECT_DIR, DB_PATH)


def _connect_ro(path):
    """Open a SQLite DB strictly read-only — never creates or locks the file."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _video_count(conn):
    return conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


def backup(db_path, dest_dir):
    """Write a verified, consistent snapshot of db_path into dest_dir and return
    its path. Raises BackupError with an actionable message on any failure."""
    if not os.path.exists(db_path):
        raise BackupError(f"database not found: {db_path}")

    # Warn if the destination looks like a Finder display label rather than a
    # real folder (the "Documents - <Mac name>" trap that creates phantom dirs).
    if any(c.startswith("Documents - ") for c in os.path.normpath(dest_dir).split(os.sep)):
        print(
            "warning: destination contains a 'Documents - <Mac name>' segment, which is "
            "usually a Finder display label, not a real folder. This may create a phantom "
            "directory — verify the backup actually lands where you expect.",
            file=sys.stderr,
        )

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        raise BackupError(
            f"cannot create destination '{dest_dir}': {e}. Check the path, or grant your "
            "terminal Full Disk Access in System Settings > Privacy & Security."
        )
    if not os.access(dest_dir, os.W_OK):
        raise BackupError(f"destination is not writable: {dest_dir}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_path = os.path.join(dest_dir, f"swipefile.db.bak-{stamp}")

    # Read the source (read-only) and snapshot it with the online backup API ->
    # one consistent file, no -wal/-shm sidecars.
    try:
        src = _connect_ro(db_path)
    except sqlite3.Error as e:
        raise BackupError(f"cannot open database {db_path}: {e}")
    try:
        expected_videos = _video_count(src)
        dst = sqlite3.connect(dest_path)
        try:
            with dst:
                src.backup(dst)
            # The snapshot inherits the source's WAL journal mode, which leaves
            # -wal/-shm sidecars. Collapse to a single self-contained file so the
            # backup is one portable artifact.
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    except sqlite3.Error as e:
        _safe_remove(dest_path)
        raise BackupError(f"snapshot failed: {e}")
    finally:
        src.close()

    _verify(dest_path, expected_videos)
    return dest_path, expected_videos


def _verify(dest_path, expected_videos):
    """Re-open the written snapshot and confirm it is a sound copy. Removes the
    file and raises BackupError if anything is off."""
    try:
        if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
            raise BackupError("snapshot file is missing or empty after write")
        conn = _connect_ro(dest_path)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise BackupError(f"integrity check failed: {integrity}")
            got = _video_count(conn)
            if got != expected_videos:
                raise BackupError(
                    f"row count mismatch: snapshot has {got} videos, source had {expected_videos}"
                )
        finally:
            conn.close()
    except sqlite3.Error as e:
        _safe_remove(dest_path)
        raise BackupError(f"could not verify snapshot: {e}")
    except BackupError:
        _safe_remove(dest_path)
        raise


def _safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Back up swipefile.db to the cloud-synced folder (verified)."
    )
    parser.add_argument(
        "--dest",
        default=DEFAULT_DEST_DIR,
        help="Destination directory for the backup (default: ~/Documents/database-backups).",
    )
    args = parser.parse_args()

    try:
        db_path = resolve_db_path()
        dest_path, videos = backup(db_path, args.dest)
    except BackupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    size = os.path.getsize(dest_path)
    print(f"Backed up {db_path}")
    print(f"       -> {dest_path}")
    print(f"       {size:,} bytes — verified: integrity ok, {videos} videos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
