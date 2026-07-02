#!/usr/bin/env python
"""Import successful molecule parquet files into ClickHouse.

The script discovers molecule directories that contain a _SUCCESS marker under
the root directory, then imports each selected parquet file with clickhouse-client
via stdin. Successfully imported files are tracked in SQLite so repeated runs can
resume safely.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple, TypeVar


DEFAULT_ROOT = "/mnt/datadisk/drug_fragment/build_001"
DEFAULT_DATABASE = "drug_fragment"
DEFAULT_TABLE = "fragments_small"
DEFAULT_HOST = "36.151.241.14"
DEFAULT_PORT = 9000 
DEFAULT_STATE_DB = "/mnt/datadisk/drug_fragment/.clickhouse_import_state.sqlite"

MOLECULE_ID_RE = re.compile(r"(?:^|[\\/])molecule_id=([^\\/]+)(?:[\\/]|$)")
T = TypeVar("T")


class MoleculeDir(NamedTuple):
    molecule_id: str
    path: Path


class ImportFile(NamedTuple):
    molecule_id: str
    path: Path
    size: int
    mtime_ns: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import successful molecule parquet files into ClickHouse."
    )
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--table", default=DEFAULT_TABLE)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--user", default="default")
    parser.add_argument(
        "--password",
        default=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        help="ClickHouse password. Defaults to CLICKHOUSE_PASSWORD or empty string.",
    )
    parser.add_argument("--state-db", default=DEFAULT_STATE_DB)
    parser.add_argument("--clickhouse-client", default="clickhouse-client")
    parser.add_argument("--secure", action="store_true")
    parser.add_argument("--config-file")
    parser.add_argument("--max-insert-threads", type=int, default=4)
    parser.add_argument("--receive-timeout", type=int, default=3600)
    parser.add_argument("--send-timeout", type=int, default=3600)
    parser.add_argument("--include-shard-parts", action="store_true")
    parser.add_argument("--reimport", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--limit-molecules", type=int)
    parser.add_argument("--limit-files", type=int)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def shell_quote_identifier(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def molecule_sort_key(item: MoleculeDir) -> tuple[int, int | str]:
    molecule_id = item.molecule_id
    if molecule_id.isdigit():
        return (0, int(molecule_id))
    return (1, molecule_id)


def find_success_molecules(root: Path) -> list[MoleculeDir]:
    seen: dict[tuple[str, str], MoleculeDir] = {}
    if not root.exists():
        logging.warning("root does not exist: %s", root)
        return []

    for success_path in root.rglob("_SUCCESS"):
        if not success_path.is_file():
            continue
        match = MOLECULE_ID_RE.search(str(success_path))
        if not match:
            continue
        molecule_id = match.group(1)
        molecule_dir = success_path.parent
        seen[(molecule_id, str(molecule_dir))] = MoleculeDir(molecule_id, molecule_dir)

    return sorted(seen.values(), key=molecule_sort_key)


def iter_parquet_files(
    molecules: Iterable[MoleculeDir], include_shard_parts: bool
) -> Iterator[ImportFile]:
    for molecule in molecules:
        paths: list[Path] = []
        paths.extend(molecule.path.glob("chunk_*.parquet"))
        if include_shard_parts:
            paths.extend(molecule.path.glob("shard-*.chunk-*.parquet"))

        for parquet_path in sorted(set(paths), key=lambda p: p.name):
            if parquet_path.name.endswith(".tmp") or ".tmp" in parquet_path.suffixes:
                continue
            if not parquet_path.is_file():
                continue
            stat = parquet_path.stat()
            yield ImportFile(
                molecule_id=molecule.molecule_id,
                path=parquet_path,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )


def infer_build_name(path: Path) -> str:
    for part in reversed(path.parts):
        if part.startswith("build_"):
            return part
    return ""


def ensure_state_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imported_files
        (
            file_path TEXT PRIMARY KEY,
            file_size INTEGER NOT NULL,
            file_mtime_ns INTEGER NOT NULL,
            molecule_id TEXT NOT NULL,
            table_fullname TEXT NOT NULL DEFAULT '',
            imported_at TEXT NOT NULL
        )
        """
    )
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(imported_files)").fetchall()
    }
    migrations = {
        "file_path": "ALTER TABLE imported_files ADD COLUMN file_path TEXT",
        "file_size": "ALTER TABLE imported_files ADD COLUMN file_size INTEGER",
        "size_bytes": "ALTER TABLE imported_files ADD COLUMN size_bytes INTEGER",
        "file_mtime_ns": "ALTER TABLE imported_files ADD COLUMN file_mtime_ns INTEGER",
        "molecule_id": "ALTER TABLE imported_files ADD COLUMN molecule_id TEXT",
        "table_fullname": (
            "ALTER TABLE imported_files ADD COLUMN "
            "table_fullname TEXT NOT NULL DEFAULT ''"
        ),
        "imported_at": "ALTER TABLE imported_files ADD COLUMN imported_at TEXT",
    }
    for column_name, sql in migrations.items():
        if column_name not in existing_columns:
            logging.warning("state db missing column %s; adding it", column_name)
            conn.execute(sql)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_imported_files_path_size_mtime
        ON imported_files(file_path, file_size, file_mtime_ns)
        """
    )
    conn.commit()


def get_state_columns(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("PRAGMA table_info(imported_files)").fetchall()
    finally:
        conn.row_factory = None


def already_imported(conn: sqlite3.Connection, import_file: ImportFile) -> bool:
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(imported_files)").fetchall()
    }
    size_columns = [
        column for column in ("file_size", "size_bytes") if column in existing_columns
    ]
    mtime_columns = [
        column for column in ("file_mtime_ns", "mtime_ns") if column in existing_columns
    ]
    size_where = " OR ".join(
        f"{shell_quote_identifier(column)} = ?" for column in size_columns
    )
    mtime_where = " OR ".join(
        f"{shell_quote_identifier(column)} = ?" for column in mtime_columns
    )
    row = conn.execute(
        f"""
        SELECT 1
        FROM imported_files
        WHERE file_path = ?
          AND ({size_where})
          AND ({mtime_where})
        """,
        (
            str(import_file.path),
            *([import_file.size] * len(size_columns)),
            *([import_file.mtime_ns] * len(mtime_columns)),
        ),
    ).fetchone()
    return row is not None


def record_imported(
    conn: sqlite3.Connection, import_file: ImportFile, table_fullname: str
) -> None:
    imported_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    known_values = {
        "file_path": str(import_file.path),
        "path": str(import_file.path),
        "file_size": import_file.size,
        "size_bytes": import_file.size,
        "file_mtime_ns": import_file.mtime_ns,
        "mtime_ns": import_file.mtime_ns,
        "molecule_id": import_file.molecule_id,
        "build_name": infer_build_name(import_file.path),
        "table_fullname": table_fullname,
        "imported_at": imported_at,
        "created_at": imported_at,
        "updated_at": imported_at,
    }
    state_columns = get_state_columns(conn)
    insert_columns: list[str] = []
    insert_values: list[object] = []
    for column in state_columns:
        name = column["name"]
        if name in known_values:
            insert_columns.append(name)
            insert_values.append(known_values[name])
        elif column["notnull"] and column["dflt_value"] is None and not column["pk"]:
            logging.warning(
                "state db column %s is NOT NULL with no default; filling empty string",
                name,
            )
            insert_columns.append(name)
            insert_values.append("")

    conn.execute("DELETE FROM imported_files WHERE file_path = ?", (str(import_file.path),))
    quoted_columns = ", ".join(shell_quote_identifier(column) for column in insert_columns)
    placeholders = ", ".join("?" for _ in insert_columns)
    conn.execute(
        f"INSERT INTO imported_files ({quoted_columns}) VALUES ({placeholders})",
        insert_values,
    )
    conn.commit()


def build_insert_sql(database: str, table: str) -> str:
    db = shell_quote_identifier(database)
    tbl = shell_quote_identifier(table)
    columns = [
        "molecule_id",
        "fragment_key",
        "fragment_hash256",
        "canonical_smiles",
        "atom_count",
        "bond_count",
    ]
    quoted_columns = ",\n    ".join(shell_quote_identifier(column) for column in columns)
    return (
        f"INSERT INTO {db}.{tbl}\n"
        f"(\n"
        f"    {quoted_columns}\n"
        f")\n"
        f"FORMAT Parquet"
    )


def build_clickhouse_command(args: argparse.Namespace, insert_sql: str) -> list[str]:
    command = [
        args.clickhouse_client,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--user",
        args.user,
        "--password",
        args.password,
        "--query",
        insert_sql,
        f"--max_insert_threads={args.max_insert_threads}",
        f"--receive_timeout={args.receive_timeout}",
        f"--send_timeout={args.send_timeout}",
    ]
    if args.secure:
        command.append("--secure")
    if args.config_file:
        command.extend(["--config-file", args.config_file])
    return command


def import_one_file(args: argparse.Namespace, import_file: ImportFile, insert_sql: str) -> None:
    command = build_clickhouse_command(args, insert_sql)
    with import_file.path.open("rb") as parquet_stdin:
        result = subprocess.run(
            command,
            stdin=parquet_stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )

    if result.returncode != 0:
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        hint = ""
        if args.port == 8123:
            hint = (
                "\nhint: clickhouse-client uses the native TCP protocol; "
                "port 8123 is usually the HTTP port. Try --port 9000, or "
                "--port 9440 --secure if your server uses secure native TCP."
            )
        raise RuntimeError(
            "clickhouse-client failed for "
            f"{import_file.path} with exit code {result.returncode}\n"
            f"stdout: {stdout}\n"
            f"stderr: {stderr}"
            f"{hint}"
        )


def maybe_limit(items: list[T], limit: int | None) -> list[T]:
    if limit is None:
        return items
    if limit < 0:
        raise ValueError("limit values must be non-negative")
    return items[:limit]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)

    fill_src_file = False
    root = Path(args.root)
    target = f"{args.database}.{args.table}"

    logging.info(
        "start root=%s database=%s table=%s host=%s port=%s dry_run=%s fill_src_file=%s",
        args.root,
        args.database,
        args.table,
        args.host,
        args.port,
        args.dry_run,
        fill_src_file,
    )
    if not args.dry_run and args.port == 8123:
        logging.warning(
            "port 8123 is usually ClickHouse HTTP; clickhouse-client normally "
            "needs the native TCP port, commonly 9000 or 9440 with --secure"
        )

    try:
        molecules = find_success_molecules(root)
        logging.info("found successful molecules: %d", len(molecules))
        molecules = maybe_limit(molecules, args.limit_molecules)
        import_files = list(iter_parquet_files(molecules, args.include_shard_parts))
        import_files = maybe_limit(import_files, args.limit_files)
        logging.info("parquet files to consider: %d", len(import_files))
    except Exception:
        logging.exception("failed while scanning root: %s", root)
        return 1

    if args.dry_run:
        print(f"target_table={target}")
        print(f"fill_src_file={fill_src_file}")
        print(f"successful_molecules={len(molecules)}")
        print(f"parquet_files={len(import_files)}")
        for import_file in import_files:
            print(
                f"{import_file.molecule_id}\t{import_file.size}\t{import_file.path}"
            )
        return 0

    insert_sql = build_insert_sql(args.database, args.table)
    conn = sqlite3.connect(args.state_db)
    ensure_state_db(conn)

    imported = 0
    skipped = 0
    failed = 0

    try:
        for import_file in import_files:
            logging.info(
                "import molecule_id=%s file=%s size=%d",
                import_file.molecule_id,
                import_file.path,
                import_file.size,
            )

            if not args.reimport and already_imported(conn, import_file):
                skipped += 1
                logging.info("skipped already imported: %s", import_file.path)
                continue

            try:
                import_one_file(args, import_file, insert_sql)
                record_imported(conn, import_file, target)
                imported += 1
            except Exception as exc:
                failed += 1
                logging.error("failed importing %s: %s", import_file.path, exc)
                if not args.continue_on_error:
                    logging.info(
                        "completed imported=%d skipped=%d failed=%d",
                        imported,
                        skipped,
                        failed,
                    )
                    return 1
    finally:
        conn.close()

    logging.info(
        "completed imported=%d skipped=%d failed=%d", imported, skipped, failed
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
