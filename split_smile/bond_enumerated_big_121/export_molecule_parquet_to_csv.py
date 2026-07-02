from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path


LOGGER = logging.getLogger("export_molecule_parquet_to_csv")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def natural_chunk_key(path: Path) -> tuple[int, str]:
    match = re.search(r"chunk_(\d+)\.parquet$", path.name)
    if match:
        return int(match.group(1)), path.name
    return sys.maxsize, path.name


def default_output_path(molecule_dir: Path) -> Path:
    molecule_id = molecule_dir.name
    if molecule_id.startswith("molecule_id="):
        molecule_id = molecule_id.split("=", 1)[1]
    return molecule_dir / f"{molecule_id}.csv"


def parse_columns(value: str | None) -> list[str] | None:
    if not value:
        return None
    columns = [column.strip() for column in value.split(",") if column.strip()]
    return columns or None


def export_parquet_dir_to_csv(
    molecule_dir: Path,
    output_path: Path,
    columns: list[str] | None,
    batch_size: int,
    pattern: str,
    overwrite: bool,
    limit_rows: int | None,
    max_files: int | None,
) -> tuple[int, int]:
    import pyarrow.csv as pacsv
    import pyarrow.parquet as pq

    parquet_files = sorted(molecule_dir.glob(pattern), key=natural_chunk_key)
    parquet_files = [path for path in parquet_files if path.is_file()]
    if max_files is not None:
        parquet_files = parquet_files[:max_files]
    if not parquet_files:
        raise FileNotFoundError(f"No parquet files matched {pattern!r} under {molecule_dir}")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Output CSV already exists: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    tmp_path.unlink(missing_ok=True)

    writer = None
    total_rows = 0
    written_files = 0
    try:
        with tmp_path.open("wb") as sink:
            for file_index, parquet_path in enumerate(parquet_files, start=1):
                parquet_file = pq.ParquetFile(parquet_path)
                file_rows = 0
                for batch in parquet_file.iter_batches(batch_size=batch_size, columns=columns):
                    if limit_rows is not None:
                        remaining = limit_rows - total_rows
                        if remaining <= 0:
                            break
                        if batch.num_rows > remaining:
                            batch = batch.slice(0, remaining)
                    if writer is None:
                        writer = pacsv.CSVWriter(sink, batch.schema)
                    writer.write_batch(batch)
                    total_rows += batch.num_rows
                    file_rows += batch.num_rows
                written_files += 1
                LOGGER.info(
                    "file=%s index=%d/%d rows=%d total_rows=%d",
                    parquet_path.name,
                    file_index,
                    len(parquet_files),
                    file_rows,
                    total_rows,
                )
                if limit_rows is not None and total_rows >= limit_rows:
                    break
            if writer is not None:
                writer.close()
        tmp_path.replace(output_path)
    except Exception:
        if writer is not None:
            writer.close()
        tmp_path.unlink(missing_ok=True)
        raise
    return written_files, total_rows


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export one molecule_id directory of chunk_*.parquet files to a single streaming CSV."
    )
    parser.add_argument(
        "molecule_dir",
        type=Path,
        help="Directory like /mnt/datadisk/drug_fragment/build_002/molecule_id=937",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="CSV output path. Defaults to <molecule_dir>/<molecule_id>.csv.",
    )
    parser.add_argument(
        "--columns",
        default=None,
        help="Comma-separated columns to export. Defaults to all columns in the parquet files.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100000,
        help="Arrow rows per read/write batch.",
    )
    parser.add_argument(
        "--pattern",
        default="chunk_*.parquet",
        help="Parquet glob pattern under molecule_dir. Defaults to finalized chunk_*.parquet.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite output CSV if it exists.")
    parser.add_argument("--limit-rows", type=int, default=None, help="Optional row limit for testing.")
    parser.add_argument("--max-files", type=int, default=None, help="Optional parquet file limit for testing.")
    return parser


def main() -> None:
    configure_logging()
    parser = build_arg_parser()
    args = parser.parse_args()
    molecule_dir = args.molecule_dir
    if not molecule_dir.is_dir():
        parser.error(f"molecule_dir is not a directory: {molecule_dir}")
    if args.batch_size <= 0:
        parser.error("--batch-size must be > 0")
    output_path = args.output or default_output_path(molecule_dir)
    start = time.time()
    file_count, row_count = export_parquet_dir_to_csv(
        molecule_dir=molecule_dir,
        output_path=output_path,
        columns=parse_columns(args.columns),
        batch_size=args.batch_size,
        pattern=args.pattern,
        overwrite=bool(args.overwrite),
        limit_rows=args.limit_rows,
        max_files=args.max_files,
    )
    elapsed = time.time() - start
    LOGGER.info(
        "done files=%d rows=%d output=%s elapsed_seconds=%.3f rows_per_second=%.2f",
        file_count,
        row_count,
        output_path,
        elapsed,
        row_count / elapsed if elapsed else 0.0,
    )


if __name__ == "__main__":
    main()
