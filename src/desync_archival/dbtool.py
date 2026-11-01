"""Database commands: status, migrate, backup and restore. Also manages the secrets file."""

import argparse
import getpass
import sqlite3
import sys
from pathlib import Path

from desync_archival import backup, migrations, settings
from desync_archival.services import archive_peeker, catalog_levels, login, secret_store


def set_password() -> int:
    if sys.stdin.isatty():
        password = getpass.getpass("New app password: ")
        if password != getpass.getpass("Repeat it: "):
            print("error: the passwords did not match", file=sys.stderr)
            return 1
    else:
        password = sys.stdin.readline().rstrip("\n")
    try:
        print(f"password saved in {login.set_password(password)}")
    except login.WeakPasswordError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def move_secrets(args: argparse.Namespace) -> int:
    conn = sqlite3.connect(settings.db_path())
    try:
        if args.command == "export-secrets":
            print(f"copied to {settings.secrets_file()}: {secret_store.export_from_database(conn)}")
        elif not args.confirm:
            print(f"would blank: {sorted(secret_store.stored_in_database(conn))}. Add --confirm to do it.")
        else:
            print(f"blanked: {secret_store.blank_database(conn)}")
    except secret_store.SecretsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--confirm-production", action="store_true")
    build = commands.add_parser("build-models")
    build.add_argument("--confirm-production", action="store_true")
    build.add_argument("--rebuild", action="store_true", help="redo every unreviewed proposal")
    repeek = commands.add_parser("repeek-broken")
    repeek.add_argument("--confirm-production", action="store_true")
    commands.add_parser("backup")
    restore = commands.add_parser("restore")
    restore.add_argument("backup_file", type=Path)
    restore.add_argument("target", type=Path)
    commands.add_parser("set-password")
    commands.add_parser("export-secrets")
    blank = commands.add_parser("blank-secrets")
    blank.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    if args.command == "set-password":
        return set_password()
    if args.command in ("export-secrets", "blank-secrets"):
        return move_secrets(args)

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
        elif args.command == "build-models":
            if settings.is_production() and not args.confirm_production:
                raise migrations.MigrationError("Refusing to build models in production without --confirm-production")
            print(f"backup written: {backup.create_backup(db_path)}")
            conn = sqlite3.connect(db_path)
            # Off by default in SQLite, and a rebuild deletes rows that others hang from.
            conn.execute("PRAGMA foreign_keys = ON;")
            try:
                print(catalog_levels.rebuild_unreviewed(conn) if args.rebuild else catalog_levels.build_archive_models(conn))
                print(f"orphan links cleared: {catalog_levels.clean_orphan_links(conn)}")
            finally:
                conn.close()
        elif args.command == "repeek-broken":
            if settings.is_production() and not args.confirm_production:
                raise migrations.MigrationError("Refusing to repeek in production without --confirm-production")
            print(f"backup written: {backup.create_backup(db_path)}")
            print(archive_peeker.repeek_broken(db_path))
        elif args.command == "backup":
            print(f"backup written: {backup.create_backup(db_path)}")
        else:
            # Resolved because restore opens these as URIs, and those can't be relative.
            counts = backup.restore(args.backup_file.resolve(), args.target.resolve())
            print(f"restored {sum(counts.values())} rows to {args.target}")
    except (backup.BackupError, migrations.MigrationError, settings.SettingsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
