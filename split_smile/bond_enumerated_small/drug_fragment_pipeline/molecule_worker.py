from __future__ import annotations

import ctypes
import ctypes.util
import gc
import json
import logging
import pickle
import shutil
import time
from array import array
from pathlib import Path
from typing import Any, MutableSequence

from .config import load_config
from .hash_utils import canonicalize_fragment_smiles, canonicalize_smiles, hash_fragment
from .parquet_writer import verify_checksum, write_chunk_parquet
from .smiles_fragmenter import (
    BondFragmentRowIterator,
    heavy_atom_indices,
    parse_smiles,
)


logger = logging.getLogger(__name__)
ColumnBuffer = dict[str, MutableSequence[Any]]
_MALLOC_TRIM = None
_MALLOC_TRIM_LOADED = False
CHECKPOINT_VERSION = 1
ALGORITHM_VERSION = "bond_fragment_row_iterator_v1"


def _chunk_path(molecule_dir: Path, chunk_index: int) -> Path:
    return molecule_dir / f"chunk_{chunk_index:06d}.parquet"


def _checkpoint_path(molecule_dir: Path, chunk_index: int) -> Path:
    return molecule_dir / f"checkpoint_{chunk_index:06d}.pkl"


def _checkpoint_index(path: Path) -> int:
    return int(path.stem.split("_", 1)[1])


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _write_records(
    records: ColumnBuffer,
    molecule_dir: Path,
    chunk_index: int,
) -> None:
    config = load_config()
    write_chunk_parquet(
        records,
        _chunk_path(molecule_dir, chunk_index),
        compression=config.parquet_compression,
        compression_level=config.parquet_compression_level,
    )


def _checkpoint_params(
    *,
    molecule_id: int,
    smiles: str,
    min_atoms: int,
    max_atoms: int,
    limit: int | None,
    chunk_rows: int,
    debug_fields: bool,
    trust_fragment_canonical: bool,
) -> dict[str, Any]:
    return {
        "molecule_id": molecule_id,
        "smiles": smiles,
        "min_atoms": min_atoms,
        "max_atoms": max_atoms,
        "limit": limit,
        "chunk_rows": chunk_rows,
        "debug_fields": debug_fields,
        "trust_fragment_canonical": trust_fragment_canonical,
    }


def _write_checkpoint(
    molecule_dir: Path,
    chunk_count: int,
    params: dict[str, Any],
    iterator: BondFragmentRowIterator,
    fragment_rows: int,
    unsanitized_canonical_rows: int,
    trusted_canonical_rows: int,
    start: float,
) -> None:
    from .parquet_writer import write_checksum

    checkpoint_path = _checkpoint_path(molecule_dir, chunk_count)
    tmp_path = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
    payload = {
        "checkpoint_version": CHECKPOINT_VERSION,
        "algorithm_version": ALGORITHM_VERSION,
        "params": params,
        "chunk_count": chunk_count,
        "fragment_rows": fragment_rows,
        "elapsed_seconds": time.perf_counter() - start,
        "unsanitized_canonical_rows": unsanitized_canonical_rows,
        "trusted_canonical_rows": trusted_canonical_rows,
        "iterator_state": iterator.snapshot(),
    }
    try:
        with tmp_path.open("wb") as file:
            pickle.dump(payload, file, protocol=pickle.HIGHEST_PROTOCOL)
            file.flush()
        tmp_path.replace(checkpoint_path)
        write_checksum(checkpoint_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _load_checkpoint(checkpoint_path: Path) -> dict[str, Any]:
    with checkpoint_path.open("rb") as file:
        payload = pickle.load(file)
    if payload.get("checkpoint_version") != CHECKPOINT_VERSION:
        raise RuntimeError(f"Unsupported checkpoint version: {checkpoint_path}")
    if payload.get("algorithm_version") != ALGORITHM_VERSION:
        raise RuntimeError(f"Unsupported checkpoint algorithm: {checkpoint_path}")
    return payload


def _latest_valid_checkpoint(molecule_dir: Path) -> Path | None:
    checkpoints = sorted(
        molecule_dir.glob("checkpoint_*.pkl"),
        key=_checkpoint_index,
        reverse=True,
    )
    for checkpoint_path in checkpoints:
        if verify_checksum(checkpoint_path):
            return checkpoint_path
    return None


def _verify_chunks_through(molecule_dir: Path, chunk_count: int) -> None:
    for chunk_index in range(1, chunk_count + 1):
        parquet_path = _chunk_path(molecule_dir, chunk_index)
        if not parquet_path.exists():
            raise RuntimeError(f"Missing checkpointed chunk file: {parquet_path}")
        if not verify_checksum(parquet_path):
            raise RuntimeError(f"Checksum verification failed: {parquet_path}")


def _prune_after_checkpoint(molecule_dir: Path, chunk_count: int) -> None:
    for parquet_path in molecule_dir.glob("chunk_*.parquet"):
        if _checkpoint_index(parquet_path) > chunk_count:
            parquet_path.unlink(missing_ok=True)
            parquet_path.with_suffix(".checksum").unlink(missing_ok=True)
    for checkpoint_path in molecule_dir.glob("checkpoint_*.pkl"):
        if _checkpoint_index(checkpoint_path) > chunk_count:
            checkpoint_path.unlink(missing_ok=True)
            checkpoint_path.with_suffix(".checksum").unlink(missing_ok=True)


def _should_checkpoint(
    chunk_count: int,
    checkpoint_after_chunks: int,
    checkpoint_every_chunks: int,
) -> bool:
    if checkpoint_every_chunks <= 0:
        return False
    if chunk_count < checkpoint_after_chunks:
        return False
    return (chunk_count - checkpoint_after_chunks) % checkpoint_every_chunks == 0


def _new_buffer(debug_fields: bool) -> ColumnBuffer:
    buffer: ColumnBuffer = {
        "molecule_id": array("H"),
        "fragment_key": [],
        "fragment_hash256": [],
        "canonical_smiles": [],
        "atom_count": array("H"),
        "bond_count": array("H"),
    }
    if debug_fields:
        buffer.update(
            {
                "fragment_smiles": [],
                "atom_indices_json": [],
                "bond_indices_json": [],
                "protected_units_hit_json": [],
                "non_chon_hetero_atoms_json": [],
            }
        )
    return buffer


def _maybe_collect_garbage(chunk_count: int, gc_every_n_chunks: int) -> None:
    if gc_every_n_chunks > 0 and chunk_count % gc_every_n_chunks == 0:
        gc.collect()
        _malloc_trim()


def _malloc_trim() -> None:
    malloc_trim = _load_malloc_trim()
    if malloc_trim is not None:
        malloc_trim(0)


def _load_malloc_trim() -> Any | None:
    global _MALLOC_TRIM, _MALLOC_TRIM_LOADED
    if _MALLOC_TRIM_LOADED:
        return _MALLOC_TRIM

    _MALLOC_TRIM_LOADED = True
    libc_name = ctypes.util.find_library("c")
    if not libc_name:
        return None

    try:
        libc = ctypes.CDLL(libc_name)
        malloc_trim = libc.malloc_trim
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        _MALLOC_TRIM = malloc_trim
    except (AttributeError, OSError):
        _MALLOC_TRIM = None
    return _MALLOC_TRIM


def _log_chunk_progress(
    molecule_id: int,
    chunk_count: int,
    fragment_rows: int,
    start: float,
) -> None:
    elapsed = time.perf_counter() - start
    logger.info(
        "molecule_id=%s chunk=%s fragment_rows=%s elapsed_seconds=%.3f fragments_per_second=%.2f",
        molecule_id,
        chunk_count,
        fragment_rows,
        elapsed,
        fragment_rows / elapsed if elapsed > 0 else 0.0,
    )


def _buffer_len(buffer: ColumnBuffer) -> int:
    return len(buffer["molecule_id"])


def _append_record(
    buffer: ColumnBuffer,
    molecule_id: int,
    fragment_key: str,
    fragment_hash256: str,
    canonical_smiles: str,
    atom_count: int,
    bond_count: int,
    fragment_smiles: str,
    atom_indices: list[int] | None,
    bond_indices: list[int] | None,
    protected_units_hit: tuple[int, ...] | None,
    non_chon_hetero_atoms: list[str] | None,
    debug_fields: bool,
) -> None:
    buffer["molecule_id"].append(molecule_id)
    buffer["fragment_key"].append(fragment_key)
    buffer["fragment_hash256"].append(fragment_hash256)
    buffer["canonical_smiles"].append(canonical_smiles)
    buffer["atom_count"].append(atom_count)
    buffer["bond_count"].append(bond_count)
    if debug_fields:
        buffer["fragment_smiles"].append(fragment_smiles)
        buffer["atom_indices_json"].append(_json_dumps(atom_indices or []))
        buffer["bond_indices_json"].append(_json_dumps(bond_indices or []))
        buffer["protected_units_hit_json"].append(_json_dumps(protected_units_hit or ()))
        buffer["non_chon_hetero_atoms_json"].append(
            _json_dumps(non_chon_hetero_atoms or [])
        )


def process_molecule(
    molecule_id: int,
    smiles: str,
    build_dir: Path,
    min_atoms: int,
    max_atoms: int | None,
    limit: int | None,
    chunk_rows: int,
    debug_fields: bool,
) -> dict[str, Any]:
    start = time.perf_counter()
    molecule_dir = build_dir / f"molecule_id={molecule_id}"
    success_path = molecule_dir / "_SUCCESS"

    try:
        config = load_config()
        if success_path.exists():
            metadata = json.loads(success_path.read_text(encoding="utf-8"))
            metadata["status"] = "skipped"
            return metadata

        mol = parse_smiles(smiles)
        effective_max_atoms = max_atoms if max_atoms is not None else len(heavy_atom_indices(mol))
        params = _checkpoint_params(
            molecule_id=molecule_id,
            smiles=smiles,
            min_atoms=min_atoms,
            max_atoms=effective_max_atoms,
            limit=limit,
            chunk_rows=chunk_rows,
            debug_fields=debug_fields,
            trust_fragment_canonical=config.trust_fragment_canonical,
        )

        restored_payload: dict[str, Any] | None = None
        if molecule_dir.exists():
            for tmp_path in molecule_dir.glob("*.tmp"):
                tmp_path.unlink(missing_ok=True)
            if config.enable_checkpoint:
                checkpoint_path = _latest_valid_checkpoint(molecule_dir)
                if checkpoint_path is not None:
                    try:
                        candidate_payload = _load_checkpoint(checkpoint_path)
                        if candidate_payload.get("params") != params:
                            raise RuntimeError(
                                f"Checkpoint parameters changed: {checkpoint_path}"
                            )
                        chunk_count_to_restore = int(candidate_payload["chunk_count"])
                        _verify_chunks_through(molecule_dir, chunk_count_to_restore)
                        _prune_after_checkpoint(molecule_dir, chunk_count_to_restore)
                        restored_payload = candidate_payload
                        start = time.perf_counter() - float(
                            restored_payload.get("elapsed_seconds", 0.0)
                        )
                        logger.info(
                            "molecule_id=%s status=resumed checkpoint_chunk=%s fragment_rows=%s",
                            molecule_id,
                            restored_payload["chunk_count"],
                            restored_payload["fragment_rows"],
                        )
                    except Exception:
                        logger.exception(
                            "Molecule %s checkpoint restore failed; restarting molecule",
                            molecule_id,
                        )
                        shutil.rmtree(molecule_dir)
                else:
                    shutil.rmtree(molecule_dir)
            else:
                shutil.rmtree(molecule_dir)

        molecule_dir.mkdir(parents=True, exist_ok=True)
        for tmp_path in molecule_dir.glob("*.tmp"):
            tmp_path.unlink(missing_ok=True)

        buffer = _new_buffer(debug_fields)
        if restored_payload is None:
            iterator_state = None
            fragment_rows = 0
            chunk_count = 0
            unsanitized_canonical_rows = 0
            trusted_canonical_rows = 0
        else:
            iterator_state = restored_payload["iterator_state"]
            fragment_rows = int(restored_payload["fragment_rows"])
            chunk_count = int(restored_payload["chunk_count"])
            unsanitized_canonical_rows = int(
                restored_payload["unsanitized_canonical_rows"]
            )
            trusted_canonical_rows = int(restored_payload["trusted_canonical_rows"])

        iterator = BondFragmentRowIterator(
            mol,
            min_atoms=min_atoms,
            max_atoms=effective_max_atoms,
            limit=limit,
            debug_fields=debug_fields,
            state=iterator_state,
        )

        for (
            fragment_smiles,
            atom_count,
            bond_count,
            atom_indices,
            bond_indices,
            protected_units_hit,
            non_chon_hetero_atoms,
        ) in iterator:
            if config.trust_fragment_canonical:
                canonical_smiles = fragment_smiles
                trusted_canonical_rows += 1
            else:
                try:
                    canonical_smiles = canonicalize_smiles(fragment_smiles)
                except ValueError:
                    canonical_smiles = canonicalize_fragment_smiles(fragment_smiles)
                    unsanitized_canonical_rows += 1
            fragment_key, fragment_hash256 = hash_fragment(canonical_smiles)
            _append_record(
                buffer,
                molecule_id,
                fragment_key,
                fragment_hash256,
                canonical_smiles,
                atom_count,
                bond_count,
                fragment_smiles,
                atom_indices,
                bond_indices,
                protected_units_hit,
                non_chon_hetero_atoms,
                debug_fields,
            )
            fragment_rows += 1

            if _buffer_len(buffer) >= chunk_rows:
                chunk_count += 1
                _write_records(buffer, molecule_dir, chunk_count)
                _log_chunk_progress(molecule_id, chunk_count, fragment_rows, start)
                buffer = _new_buffer(debug_fields)
                if config.enable_checkpoint and _should_checkpoint(
                    chunk_count,
                    config.checkpoint_after_chunks,
                    config.checkpoint_every_chunks,
                ):
                    _write_checkpoint(
                        molecule_dir,
                        chunk_count,
                        params,
                        iterator,
                        fragment_rows,
                        unsanitized_canonical_rows,
                        trusted_canonical_rows,
                        start,
                    )
                _maybe_collect_garbage(chunk_count, config.gc_collect_every_n_chunks)

        if _buffer_len(buffer) > 0:
            chunk_count += 1
            _write_records(buffer, molecule_dir, chunk_count)
            _log_chunk_progress(molecule_id, chunk_count, fragment_rows, start)
            if config.enable_checkpoint:
                _write_checkpoint(
                    molecule_dir,
                    chunk_count,
                    params,
                    iterator,
                    fragment_rows,
                    unsanitized_canonical_rows,
                    trusted_canonical_rows,
                    start,
                )
            _maybe_collect_garbage(chunk_count, config.gc_collect_every_n_chunks)
        elif config.enable_checkpoint and chunk_count > 0:
            _write_checkpoint(
                molecule_dir,
                chunk_count,
                params,
                iterator,
                fragment_rows,
                unsanitized_canonical_rows,
                trusted_canonical_rows,
                start,
            )

        for parquet_path in sorted(molecule_dir.glob("chunk_*.parquet")):
            if not verify_checksum(parquet_path):
                raise RuntimeError(f"Checksum verification failed: {parquet_path}")

        elapsed = time.perf_counter() - start
        result: dict[str, Any] = {
            "molecule_id": molecule_id,
            "status": "success",
            "chunk_count": chunk_count,
            "fragment_rows": fragment_rows,
            "elapsed_seconds": elapsed,
            "fragments_per_second": fragment_rows / elapsed if elapsed > 0 else 0.0,
            "min_atoms": min_atoms,
            "max_atoms": effective_max_atoms,
            "unsanitized_canonical_rows": unsanitized_canonical_rows,
            "trusted_canonical_rows": trusted_canonical_rows,
        }
        success_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return result
    except Exception as exc:
        elapsed = time.perf_counter() - start
        logger.exception("Molecule %s failed", molecule_id)
        return {
            "molecule_id": molecule_id,
            "status": "failed",
            "fragment_rows": 0,
            "elapsed_seconds": elapsed,
            "fragments_per_second": 0.0,
            "error_message": str(exc),
        }
