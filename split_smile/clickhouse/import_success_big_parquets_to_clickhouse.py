#!/usr/bin/env python
"""Import successful molecule parquet files into drug_fragment.fragments_big.

This script is separate from import_success_parquets_to_clickhouse.py on purpose.
It targets the partitioned large-molecule table by default and uses its own
SQLite state database so imports into fragments_small and fragments_big do not
interfere with each other.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple, TypeVar


DEFAULT_ROOT = "/mnt/datadisk/drug_fragment/build_002"
DEFAULT_DATABASE = "drug_fragment"
DEFAULT_TABLE = "fragments_big"
DEFAULT_HOST = "36.151.241.14"
DEFAULT_PORT = 9000
DEFAULT_STATE_DB = "/mnt/datadisk/drug_fragment/.clickhouse_big_import_state.sqlite"

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
        description="Import successful molecule parquet files into fragments_big."
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


def quote_identifier(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def molecule_sort_key(item: MoleculeDir) -> tuple[int, int | str]:
    if item.molecule_id.isdigit():
        return (0, int(item.molecule_id))
    return (1, item.molecule_id)


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
        paths = list(molecule.path.glob("chunk_*.parquet"))
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


def maybe_limit(items: list[T], limit: int | None) -> list[T]:
    if limit is None:
        return items
    if limit < 0:
        raise ValueError("limit values must be non-negative")
    return items[:limit]


def ensure_state_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imported_files
        (
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            file_mtime_ns INTEGER NOT NULL,
            molecule_id TEXT NOT NULL,
            table_fullname TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY (file_path, table_fullname)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_imported_big_files_resume
        ON imported_files(file_path, table_fullname, file_size, file_mtime_ns)
        """
    )
    conn.commit()


def already_imported(
    conn: sqlite3.Connection, import_file: ImportFile, table_fullname: str
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM imported_files
        WHERE file_path = ?
          AND table_fullname = ?
          AND file_size = ?
          AND file_mtime_ns = ?
        """,
        (
            str(import_file.path),
            table_fullname,
            import_file.size,
            import_file.mtime_ns,
        ),
    ).fetchone()
    return row is not None


def record_imported(
    conn: sqlite3.Connection, import_file: ImportFile, table_fullname: str
) -> None:
    imported_at = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO imported_files
            (file_path, file_size, file_mtime_ns, molecule_id, table_fullname, imported_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_path, table_fullname) DO UPDATE SET
            file_size = excluded.file_size,
            file_mtime_ns = excluded.file_mtime_ns,
            molecule_id = excluded.molecule_id,
            imported_at = excluded.imported_at
        """,
        (
            str(import_file.path),
            import_file.size,
            import_file.mtime_ns,
            import_file.molecule_id,
            table_fullname,
            imported_at,
        ),
    )
    conn.commit()


def build_insert_sql(database: str, table: str) -> str:
    columns = [
        "molecule_id",
        "fragment_key",
        "fragment_hash256",
        "canonical_smiles",
        "atom_count",
        "bond_count",
    ]
    quoted_columns = ",\n    ".join(quote_identifier(column) for column in columns)
    return (
        f"INSERT INTO {quote_identifier(database)}.{quote_identifier(table)}\n"
        "(\n"
        f"    {quoted_columns}\n"
        ")\n"
        "FORMAT Parquet"
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
        raise RuntimeError(
            "clickhouse-client failed for "
            f"{import_file.path} with exit code {result.returncode}\n"
            f"stdout: {stdout}\n"
            f"stderr: {stderr}"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)

    root = Path(args.root)
    table_fullname = f"{args.database}.{args.table}"
    fill_src_file = False

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
        print(f"target_table={table_fullname}")
        print(f"state_db={args.state_db}")
        print(f"fill_src_file={fill_src_file}")
        print(f"successful_molecules={len(molecules)}")
        print(f"parquet_files={len(import_files)}")
        for import_file in import_files:
            print(f"{import_file.molecule_id}\t{import_file.size}\t{import_file.path}")
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
            if (
                not args.reimport
                and already_imported(conn, import_file, table_fullname)
            ):
                skipped += 1
                logging.info("skipped already imported: %s", import_file.path)
                continue

            try:
                import_one_file(args, import_file, insert_sql)
                record_imported(conn, import_file, table_fullname)
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
