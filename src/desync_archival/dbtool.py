"""Database commands: status, migrate, backup and restore. Also manages the secrets file."""

import argparse
import getpass
import json
import socket
import sys
import tempfile
from pathlib import Path

from desync_archival import backup, migrations, settings
from desync_archival.db.connection import connect
from desync_archival.services import (
    analytics,
    archive_peeker,
    catalog_levels,
    catalog_repair,
    data_bundle,
    grouping_compare,
    grouping_export,
    login,
    secret_store,
    tags,
)
from desync_archival.services.db import get_db_connection


def set_password() -> int:
    """Sets the app password from a prompt, or from stdin for scripted setup."""
    if sys.stdin.isatty():
        password = getpass.getpass("New app password: ")
        if password != getpass.getpass("Repeat it: "):
            print("error: the passwords did not match", file=sys.stderr)
            return 1
    else:
        password = sys.stdin.readline().rstrip("\n")
    try:
        print(f"password saved in {login.set_password(password)}")
    except (login.WeakPasswordError, secret_store.SecretsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def move_secrets(args: argparse.Namespace) -> int:
    """Runs export-secrets or blank-secrets. Blanking is a dry run without --confirm."""
    conn = connect(settings.db_path())
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


def refuse_if_app_running() -> None:
    """Probes the app's port, because a rewrite under a live app can lose its changes.

    Raises:
        MigrationError: The database can't be brought to this schema safely.
    """
    # These rewrite tables the app is using. A stopped app is the only safe moment.
    with socket.socket() as probe:
        probe.settimeout(0.5)
        target = "127.0.0.1" if settings.host() == "0.0.0.0" else settings.host()
        if probe.connect_ex((target, settings.port())) == 0:
            raise migrations.MigrationError(f"The app is running on port {settings.port()}. Stop it first: scripts/server.sh stop")


def repair_models(db_path: Path, apply: bool, confirm_production: bool) -> str:
    """Without apply, repairs a copy and reports, so production can be checked with the app running."""
    if not apply:
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / db_path.name
            backup.copy_database(db_path, copy, "WAL")
            conn = get_db_connection(copy)
            try:
                return catalog_repair.describe(catalog_repair.repair(conn), applied=False)
            finally:
                conn.close()
    if settings.is_production() and not confirm_production:
        raise migrations.MigrationError("Refusing to repair models in production without --confirm-production")
    refuse_if_app_running()
    written = backup.create_backup(db_path)
    conn = get_db_connection(db_path)
    try:
        return f"backup written: {written}\n" + catalog_repair.describe(catalog_repair.repair(conn), applied=True)
    finally:
        conn.close()


def grouping_export_file(db_path: Path, args: argparse.Namespace) -> str:
    """Writes an E54 export to a file, as the screen's Download Export does, for an LLM test kit."""
    if settings.is_production() and not args.confirm_production:
        raise migrations.MigrationError("Refusing to record an export in production without --confirm-production")
    keys = [*args.unit, *(f"archive:{c}" for c in args.collection)]
    conn = get_db_connection(db_path)
    try:
        export_key, blob, summary = grouping_export.build(
            conn, "single" if keys else args.mode, args.scope, args.include_reviewed, args.batch_lines, unit_keys=keys or None,
        )
    finally:
        conn.close()
    args.destination.write_bytes(blob)
    return f"export {export_key}: {summary['units']} units in {summary['batches']} batches, written to {args.destination}"


def grouping_compare_answers(db_path: Path, args: argparse.Namespace) -> str:
    """Checks and compares LLM answers to one export. Reads the database only."""
    def texts(path: Path) -> list[str]:
        # A folder holds one LLM's answers, one file per batch.
        files = sorted(f for f in path.iterdir() if f.suffix.lower() in (".json", ".txt")) if path.is_dir() else [path]
        if not files:
            raise grouping_compare.CompareRefused(f"{path} holds no .json or .txt answers")
        return [f.read_text(encoding="utf-8", errors="replace") for f in files]

    answers: list[tuple[str, str | list[str]]] = [(path.stem if path.is_file() else path.name, texts(path)) for path in args.answers]
    reference = texts(args.reference) if args.reference else None
    expected = json.loads(args.expect.read_text()) if args.expect else None
    conn = connect(db_path, readonly=True)
    try:
        report = grouping_compare.describe(grouping_compare.compare(conn, answers, reference, expected))
    finally:
        conn.close()
    if args.out:
        args.out.write_text(report)
        return f"comparison written to {args.out}"
    return report


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
    repeek_chosen = commands.add_parser("repeek-files")
    repeek_chosen.add_argument("--file-id", type=int, action="append", required=True)
    repeek_chosen.add_argument("--confirm-production", action="store_true")
    commands.add_parser("backup")
    rebuild = commands.add_parser("rebuild-releases")
    rebuild.add_argument("--confirm-production", action="store_true")
    repair = commands.add_parser("repair-models", help="fix B1 to B4 in the catalog levels; a dry run unless --apply")
    repair.add_argument("--apply", action="store_true")
    repair.add_argument("--confirm-production", action="store_true")
    tidy = commands.add_parser("clean-tags")
    tidy.add_argument("--confirm-production", action="store_true")
    export = commands.add_parser("export")
    export.add_argument("destination", type=Path)
    export.add_argument("--with-secrets", action="store_true")
    bring_in = commands.add_parser("import")
    bring_in.add_argument("bundle", type=Path)
    bring_in.add_argument("--map", action="append", default=[], metavar="OLD=NEW", help="rewrite a stored path prefix")
    bring_in.add_argument("--confirm-production", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("backup_file", type=Path)
    restore.add_argument("target", type=Path)
    grouping = commands.add_parser("grouping-export", help="write an E54 export to a file, for testing LLMs")
    grouping.add_argument("destination", type=Path)
    grouping.add_argument("--unit", action="append", default=[], help="a unit key, such as archive:325721; repeat for more")
    grouping.add_argument("--collection", action="append", type=int, default=[], help="an archive collection ID; repeat for more")
    grouping.add_argument("--mode", choices=("delta", "full"), default="delta", help="without units, what to export")
    grouping.add_argument("--scope", choices=grouping_export.SCOPES, default="both")
    grouping.add_argument("--include-reviewed", action="store_true")
    grouping.add_argument("--batch-lines", type=int, default=grouping_export.DEFAULT_BATCH_LINES)
    grouping.add_argument("--confirm-production", action="store_true")
    compare = commands.add_parser("grouping-compare", help="check and compare LLM answers to one export; changes nothing")
    compare.add_argument("answers", type=Path, nargs="+", help="an answer file, or a folder of one LLM's batch answers, named after the LLM")
    compare.add_argument("--reference", type=Path, help="an answer, or folder of answers, taken as right, such as one a person fixed")
    compare.add_argument("--expect", type=Path, help="JSON of expected model counts by unit key or collection name")
    compare.add_argument("--out", type=Path, help="write the Markdown report here instead of printing it")
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
            if not db_path.exists():
                print(f"no database at {db_path} yet. Create it with:")
                print("uv run python -m desync_archival.dbtool migrate")
                return 1
            conn = connect(db_path, readonly=True)
            version = migrations.current_version(conn)
            conn.close()
            print(f"schema version {version}, latest {migrations.latest_version()}")
        elif args.command == "migrate":
            refuse_if_app_running()
            result = migrations.migrate_database(db_path, confirm_production=args.confirm_production)
            print(f"migrated from {result['from']} to {result['to']}, backup: {result['backup'] or 'none needed'}")
        elif args.command == "build-models":
            if settings.is_production() and not args.confirm_production:
                raise migrations.MigrationError("Refusing to build models in production without --confirm-production")
            refuse_if_app_running()
            print(f"backup written: {backup.create_backup(db_path)}")
            # Foreign keys are on through this, and a rebuild deletes rows that others hang from.
            conn = get_db_connection(db_path)
            try:
                print(catalog_levels.rebuild_unreviewed(conn) if args.rebuild else catalog_levels.build_archive_models(conn))
                print(f"orphan links cleared: {catalog_levels.clean_orphan_links(conn)}")
            finally:
                conn.close()
        elif args.command == "repeek-broken":
            if settings.is_production() and not args.confirm_production:
                raise migrations.MigrationError("Refusing to repeek in production without --confirm-production")
            refuse_if_app_running()
            print(f"backup written: {backup.create_backup(db_path)}")
            print(archive_peeker.repeek_broken(db_path))
        elif args.command == "repeek-files":
            if settings.is_production() and not args.confirm_production:
                raise migrations.MigrationError("Refusing to repeek in production without --confirm-production")
            refuse_if_app_running()
            print(f"backup written: {backup.create_backup(db_path)}")
            print(archive_peeker.repeek_files(db_path, set(args.file_id)))
        elif args.command == "backup":
            print(f"backup written: {backup.create_backup(db_path, full=True)}")
        elif args.command == "rebuild-releases":
            if settings.is_production() and not args.confirm_production:
                raise migrations.MigrationError("Refusing to rebuild releases in production without --confirm-production")
            refuse_if_app_running()
            print(f"backup written: {backup.create_backup(db_path)}")
            conn = get_db_connection(db_path)
            try:
                analytics.run_startup_analytics(conn)
                print(dict(conn.execute("SELECT COUNT(*) AS releases, SUM(total_files) AS files FROM releases").fetchone()))
            finally:
                conn.close()
        elif args.command == "repair-models":
            print(repair_models(db_path, args.apply, args.confirm_production))
        elif args.command == "clean-tags":
            if settings.is_production() and not args.confirm_production:
                raise migrations.MigrationError("Refusing to clean tags in production without --confirm-production")
            refuse_if_app_running()
            print(f"backup written: {backup.create_backup(db_path)}")
            conn = get_db_connection(db_path)
            try:
                print(tags.clean_up(conn))
            finally:
                conn.close()
        elif args.command == "export":
            print(data_bundle.export_bundle(db_path, args.destination.resolve(), args.with_secrets))
        elif args.command == "grouping-export":
            print(grouping_export_file(db_path, args))
        elif args.command == "grouping-compare":
            print(grouping_compare_answers(db_path, args))
        elif args.command == "import":
            if settings.is_production() and not args.confirm_production:
                raise migrations.MigrationError("Refusing to import in production without --confirm-production")
            refuse_if_app_running()
            print(data_bundle.import_bundle(args.bundle.resolve(), settings.data_dir(), data_bundle.parse_path_map(args.map)))
        else:
            # Resolved because restore opens these as URIs, and those can't be relative.
            counts = backup.restore(args.backup_file.resolve(), args.target.resolve())
            print(f"restored {sum(counts.values())} rows to {args.target}")
    except (backup.BackupError, data_bundle.BundleError, migrations.MigrationError, settings.SettingsError,
            grouping_export.ExportRefused, grouping_compare.CompareRefused, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
