"""Database commands: status, migrate, backup and restore."""

import argparse
import sqlite3
import sys
from pathlib import Path

from desync_archival import backup, migrations, settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--confirm-production", action="store_true")
    commands.add_parser("backup")
    restore = commands.add_parser("restore")
    restore.add_argument("backup_file", type=Path)
    restore.add_argument("target", type=Path)
    args = parser.parse_args()

    db_path = settings.db_path()
    try:
        if args.command == "status":
            conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
            version = migrations.current_version(conn)
            conn.close()
            print(f"schema version {version}, latest {migrations.latest_version()}")
        elif args.command == "migrate":
            result = migrations.migrate_database(db_path, confirm_production=args.confirm_production)
            print(f"migrated from {result['from']} to {result['to']}, backup: {result['backup'] or 'none needed'}")
        elif args.command == "backup":
            print(f"backup written: {backup.create_backup(db_path)}")
        else:
            counts = backup.restore(args.backup_file, args.target)
            print(f"restored {sum(counts.values())} rows to {args.target}")
    except (backup.BackupError, migrations.MigrationError, settings.SettingsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
