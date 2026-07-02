from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Iterator

from .config import configure_thread_env
from .parquet_writer import verify_checksum


configure_thread_env()


def iter_expected_ids(input_path: Path) -> Iterator[int]:
    with input_path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None or "molecule_id" not in reader.fieldnames:
            raise ValueError("Input CSV must contain molecule_id column.")
        for row in reader:
            yield int(str(row["molecule_id"]).strip())


def validate(input_path: Path, build_dir: Path) -> tuple[dict[str, int], bool]:
    expected_molecule_count = 0
    success_molecule_count = 0
    missing_success_count = 0

    for molecule_id in iter_expected_ids(input_path):
        expected_molecule_count += 1
        molecule_dir = build_dir / f"molecule_id={molecule_id}"
        if (molecule_dir / "_SUCCESS").exists():
            success_molecule_count += 1
        else:
            missing_success_count += 1

    tmp_file_count = 0
    for _tmp_path in build_dir.glob("molecule_id=*/*.tmp"):
        tmp_file_count += 1

    bad_checksum_count = 0
    total_parquet_files = 0
    total_parquet_size_bytes = 0
    for parquet_path in build_dir.glob("molecule_id=*/*.parquet"):
        total_parquet_files += 1
        total_parquet_size_bytes += parquet_path.stat().st_size
        if not verify_checksum(parquet_path):
            bad_checksum_count += 1

    metrics = {
        "expected_molecule_count": expected_molecule_count,
        "success_molecule_count": success_molecule_count,
        "missing_success_count": missing_success_count,
        "tmp_file_count": tmp_file_count,
        "bad_checksum_count": bad_checksum_count,
        "total_parquet_files": total_parquet_files,
        "total_parquet_size_bytes": total_parquet_size_bytes,
    }
    ok = (
        missing_success_count == 0
        and tmp_file_count == 0
        and bad_checksum_count == 0
    )
    return metrics, ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a fragment parquet build.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    args = parser.parse_args()

    metrics, ok = validate(args.input, args.build_dir)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

