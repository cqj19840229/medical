from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Sequence

ChunkRecords = list[dict[str, Any]] | dict[str, Sequence[Any]]


def calculate_file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum(path: Path) -> None:
    checksum_path = path.with_suffix(".checksum")
    checksum_path.write_text(calculate_file_checksum(path) + "\n", encoding="utf-8")


def verify_checksum(path: Path) -> bool:
    checksum_path = path.with_suffix(".checksum")
    if not checksum_path.exists():
        return False
    expected = checksum_path.read_text(encoding="utf-8").strip()
    return expected == calculate_file_checksum(path)


def _record_count(records: ChunkRecords) -> int:
    if isinstance(records, dict):
        first_column = next(iter(records.values()), [])
        return len(first_column)
    return len(records)


def _has_debug_fields(records: ChunkRecords) -> bool:
    if isinstance(records, dict):
        return "fragment_smiles" in records
    return bool(records and "fragment_smiles" in records[0])


def _schema_for_records(records: ChunkRecords, pa: Any) -> Any:
    fields = [
        pa.field("molecule_id", pa.string()),
        pa.field("fragment_key", pa.string()),
        pa.field("fragment_hash256", pa.string()),
        pa.field("canonical_smiles", pa.string()),
        pa.field("atom_count", pa.uint16()),
        pa.field("bond_count", pa.uint16()),
    ]
    if _has_debug_fields(records):
        fields.extend(
            [
                pa.field("fragment_smiles", pa.string()),
                pa.field("atom_indices_json", pa.string()),
                pa.field("bond_indices_json", pa.string()),
                pa.field("protected_units_hit_json", pa.string()),
                pa.field("non_chon_hetero_atoms_json", pa.string()),
            ]
        )
    return pa.schema(fields)


def write_chunk_parquet(
    records: ChunkRecords,
    output_path: Path,
    compression: str,
    compression_level: int | None,
    checksum: bool = False,
    overwrite: bool = False,
) -> None:
    if _record_count(records) == 0:
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing parquet file: {output_path}")

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        schema = _schema_for_records(records, pa)
        if isinstance(records, dict):
            table = pa.Table.from_pydict(records, schema=schema)
        else:
            table = pa.Table.from_pylist(records, schema=schema)
        pq.write_table(
            table,
            tmp_path,
            compression=compression,
            compression_level=compression_level,
        )
        tmp_path.replace(output_path)
        if checksum:
            write_checksum(output_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
