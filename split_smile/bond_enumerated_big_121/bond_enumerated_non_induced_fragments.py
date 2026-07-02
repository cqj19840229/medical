from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import sqlite3
import time
import traceback
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from multiprocessing import Manager
from pathlib import Path
from typing import Any, Iterable, Iterator

from rdkit import Chem
from rdkit import RDLogger

try:
    from parquet_writer import calculate_file_checksum, write_chunk_parquet
except ImportError:  # pragma: no cover - supports package-style execution.
    from .parquet_writer import calculate_file_checksum, write_chunk_parquet


RDLogger.DisableLog("rdApp.*")

FRAGMENT_TYPE = "bond_enumerated_non_induced_aromatic_compressed"
CHON_ATOMIC_NUMBERS = {1, 6, 7, 8}
DEFAULT_OUT_DIR = Path("/mnt/datadisk/drug_fragment/build_002")
LOGGER = logging.getLogger("fragment_build")


class MillisecondFormatter(logging.Formatter):
    default_msec_format = "%s,%03d"


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(MillisecondFormatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


@dataclass(frozen=True)
class CompressedUnits:
    atom_masks: tuple[int, ...]
    bond_masks: tuple[int, ...]
    adjacency_masks: tuple[int, ...]
    closure_masks: tuple[int, ...]
    aromatic_unit_count: int
    ordinary_bond_unit_count: int
    aromatic_systems: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...]
    unit_bond_indices: tuple[int | None, ...]

    @property
    def unit_count(self) -> int:
        return len(self.atom_masks)


@dataclass
class RunStats:
    molecule_id: str
    heavy_atom_count: int = 0
    aromatic_system_count: int = 0
    unit_count: int = 0
    ordinary_bond_unit_count: int = 0
    visited_state_count: int = 0
    emitted_fragment_count: int = 0
    written_part_count: int = 0
    elapsed_seconds: float = 0.0
    fragments_per_second: float = 0.0
    peak_memory_mb: float | None = None


def parse_smiles(smiles: str) -> Chem.Mol:
    mol = Chem.MolFromSmiles((smiles or "").strip())
    if mol is None:
        raise ValueError("SMILES cannot be parsed.")
    return mol


def heavy_atom_indices(mol: Chem.Mol) -> list[int]:
    return [atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum() > 1]


def is_non_chon_hetero_atom(atom: Chem.Atom) -> bool:
    return atom.GetAtomicNum() not in CHON_ATOMIC_NUMBERS


def non_chon_hetero_atom_symbols(mol: Chem.Mol, atom_indices: Iterable[int]) -> list[str]:
    symbols = {
        mol.GetAtomWithIdx(atom_idx).GetSymbol()
        for atom_idx in atom_indices
        if is_non_chon_hetero_atom(mol.GetAtomWithIdx(atom_idx))
    }
    return sorted(symbols)


def iter_bits(mask: int) -> Iterator[int]:
    while mask:
        bit = mask & -mask
        yield bit.bit_length() - 1
        mask ^= bit


def indices_from_mask(mask: int) -> list[int]:
    return list(iter_bits(mask))


def mask_from_indices(indices: Iterable[int]) -> int:
    mask = 0
    for index in indices:
        mask |= 1 << int(index)
    return mask


def build_aromatic_systems(mol: Chem.Mol) -> list[dict[str, set[int]]]:
    aromatic_atoms = {
        atom.GetIdx()
        for atom in mol.GetAtoms()
        if atom.GetIsAromatic() and atom.GetAtomicNum() > 1
    }
    aromatic_bonds = {
        bond.GetIdx()
        for bond in mol.GetBonds()
        if bond.GetIsAromatic()
        and bond.GetBeginAtomIdx() in aromatic_atoms
        and bond.GetEndAtomIdx() in aromatic_atoms
    }
    graph: dict[int, set[int]] = {atom_idx: set() for atom_idx in aromatic_atoms}
    for bond_idx in aromatic_bonds:
        bond = mol.GetBondWithIdx(bond_idx)
        begin_idx = bond.GetBeginAtomIdx()
        end_idx = bond.GetEndAtomIdx()
        graph[begin_idx].add(end_idx)
        graph[end_idx].add(begin_idx)

    systems: list[dict[str, set[int]]] = []
    visited: set[int] = set()
    for atom_idx in sorted(aromatic_atoms):
        if atom_idx in visited:
            continue
        stack = [atom_idx]
        component_atoms: set[int] = set()
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component_atoms.add(current)
            stack.extend(graph[current] - visited)
        component_bonds = {
            bond_idx
            for bond_idx in aromatic_bonds
            if mol.GetBondWithIdx(bond_idx).GetBeginAtomIdx() in component_atoms
            and mol.GetBondWithIdx(bond_idx).GetEndAtomIdx() in component_atoms
        }
        systems.append({"atoms": component_atoms, "bonds": component_bonds})
    return systems


def build_compressed_units(mol: Chem.Mol) -> CompressedUnits:
    aromatic_systems = build_aromatic_systems(mol)
    atom_to_aromatic_unit: dict[int, int] = {}
    atom_masks: list[int] = []
    bond_masks: list[int] = []
    unit_bond_indices: list[int | None] = []

    for unit_idx, system in enumerate(aromatic_systems):
        atom_mask = mask_from_indices(system["atoms"])
        bond_mask = mask_from_indices(system["bonds"])
        atom_masks.append(atom_mask)
        bond_masks.append(bond_mask)
        unit_bond_indices.append(None)
        for atom_idx in system["atoms"]:
            atom_to_aromatic_unit[atom_idx] = unit_idx

    ordinary_start = len(atom_masks)
    aromatic_internal_bonds = set().union(*(system["bonds"] for system in aromatic_systems), set())
    for bond in mol.GetBonds():
        if bond.GetIdx() in aromatic_internal_bonds:
            continue
        if bond.GetBeginAtom().GetAtomicNum() <= 1 or bond.GetEndAtom().GetAtomicNum() <= 1:
            continue
        atom_masks.append((1 << bond.GetBeginAtomIdx()) | (1 << bond.GetEndAtomIdx()))
        bond_masks.append(1 << bond.GetIdx())
        unit_bond_indices.append(bond.GetIdx())

    unit_count = len(atom_masks)
    atom_to_units: dict[int, list[int]] = {}
    for unit_idx, atom_mask in enumerate(atom_masks):
        for atom_idx in iter_bits(atom_mask):
            atom_to_units.setdefault(atom_idx, []).append(unit_idx)

    adjacency = [0] * unit_count
    for units in atom_to_units.values():
        unit_mask = mask_from_indices(units)
        for unit_idx in units:
            adjacency[unit_idx] |= unit_mask & ~(1 << unit_idx)

    closure = [1 << unit_idx for unit_idx in range(unit_count)]
    for unit_idx in range(ordinary_start, unit_count):
        touched = 0
        for atom_idx in iter_bits(atom_masks[unit_idx]):
            aromatic_unit = atom_to_aromatic_unit.get(atom_idx)
            if aromatic_unit is not None:
                touched |= 1 << aromatic_unit
        closure[unit_idx] |= touched

    systems_tuple = tuple(
        (tuple(sorted(system["atoms"])), tuple(sorted(system["bonds"])))
        for system in aromatic_systems
    )
    return CompressedUnits(
        atom_masks=tuple(atom_masks),
        bond_masks=tuple(bond_masks),
        adjacency_masks=tuple(adjacency),
        closure_masks=tuple(closure),
        aromatic_unit_count=ordinary_start,
        ordinary_bond_unit_count=unit_count - ordinary_start,
        aromatic_systems=systems_tuple,
        unit_bond_indices=tuple(unit_bond_indices),
    )


def close_state(state_mask: int, units: CompressedUnits) -> int:
    closed = state_mask
    for unit_idx in iter_bits(state_mask):
        closed |= units.closure_masks[unit_idx]
    return closed


def atom_bond_masks_for_state(state_mask: int, units: CompressedUnits) -> tuple[int, int]:
    atom_mask = 0
    bond_mask = 0
    for unit_idx in iter_bits(state_mask):
        atom_mask |= units.atom_masks[unit_idx]
        bond_mask |= units.bond_masks[unit_idx]
    return atom_mask, bond_mask


def protected_units_hit(state_mask: int, units: CompressedUnits) -> list[int]:
    aromatic_mask = state_mask & ((1 << units.aromatic_unit_count) - 1)
    return indices_from_mask(aromatic_mask)


def fragment_smiles_from_masks(mol: Chem.Mol, atom_mask: int, bond_mask: int) -> str:
    return Chem.MolFragmentToSmiles(
        mol,
        atomsToUse=indices_from_mask(atom_mask),
        bondsToUse=indices_from_mask(bond_mask),
        canonical=True,
        isomericSmiles=True,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DedupeStore:
    def __init__(self, mode: str, path: Path | None = None) -> None:
        self.mode = mode
        self.seen: set[str] = set()
        self.conn: sqlite3.Connection | None = None
        if mode == "sqlite":
            if path is None:
                raise ValueError("sqlite dedupe requires a path.")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path)
            self.conn.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY)")
            self.conn.commit()

    def add(self, key: str) -> bool:
        if self.mode == "none":
            return True
        if self.mode == "memory":
            if key in self.seen:
                return False
            self.seen.add(key)
            return True
        assert self.conn is not None
        try:
            self.conn.execute("INSERT INTO seen(key) VALUES (?)", (key,))
            return True
        except sqlite3.IntegrityError:
            return False

    def close(self) -> None:
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()


class StreamingParquetSink:
    def __init__(
        self,
        out_dir: Path,
        molecule_id: str,
        batch_size: int,
        compression: str,
        compression_level: int | None,
        checksum: bool,
        force: bool,
        debug_fields: bool,
        log_molecule_id: str | None = None,
        log_start_time: float | None = None,
        chunk_counter: Any | None = None,
        row_counter: Any | None = None,
        counter_lock: Any | None = None,
    ) -> None:
        self.out_dir = out_dir
        self.molecule_id = molecule_id
        self.batch_size = batch_size
        self.compression = compression
        self.compression_level = compression_level
        self.checksum = checksum
        self.force = force
        self.debug_fields = debug_fields
        self.part_index = 0
        self.records: list[dict[str, object]] = []
        self.written_parts = 0
        self.log_molecule_id = log_molecule_id
        self.log_start_time = log_start_time
        self.chunk_counter = chunk_counter
        self.row_counter = row_counter
        self.counter_lock = counter_lock
        out_dir.mkdir(parents=True, exist_ok=True)

    def add(self, record: dict[str, object]) -> None:
        self.records.append(record)
        if len(self.records) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self.records:
            return
        record_count = len(self.records)
        chunk_index, fragment_rows = self._reserve_chunk(record_count)
        output_path = self.out_dir / f"{self.molecule_id}.chunk-{chunk_index:06d}.parquet"
        if output_path.exists() and not self.force:
            raise FileExistsError(f"Refusing to overwrite existing parquet file: {output_path}")
        write_chunk_parquet(
            self.records,
            output_path,
            compression=self.compression,
            compression_level=self.compression_level,
            checksum=self.checksum,
            overwrite=self.force,
        )
        self.records.clear()
        self.written_parts += 1
        self._log_chunk(chunk_index, fragment_rows)

    def _reserve_chunk(self, record_count: int) -> tuple[int, int]:
        if self.chunk_counter is None or self.row_counter is None or self.counter_lock is None:
            self.part_index += 1
            return self.part_index, self.written_parts * self.batch_size + record_count
        with self.counter_lock:
            chunk_index = int(self.chunk_counter.get()) + 1
            fragment_rows = int(self.row_counter.get()) + record_count
            self.chunk_counter.set(chunk_index)
            self.row_counter.set(fragment_rows)
        return chunk_index, fragment_rows

    def _log_chunk(self, chunk_index: int, fragment_rows: int) -> None:
        if not self.log_molecule_id or self.log_start_time is None:
            return
        elapsed = time.time() - self.log_start_time
        fps = fragment_rows / elapsed if elapsed else 0.0
        LOGGER.info(
            "molecule_id=%s chunk=%d fragment_rows=%d elapsed_seconds=%.3f fragments_per_second=%.2f",
            self.log_molecule_id,
            chunk_index,
            fragment_rows,
            elapsed,
            fps,
        )


def make_fragment_record(
    mol: Chem.Mol,
    molecule_id: str,
    state_mask: int,
    atom_mask: int,
    bond_mask: int,
    units: CompressedUnits,
    key_mode: str,
    debug_fields: bool,
    no_smiles: bool,
) -> tuple[dict[str, object], str]:
    state_key = format(state_mask, "x")
    canonical_smiles = "" if no_smiles else fragment_smiles_from_masks(mol, atom_mask, bond_mask)
    if key_mode == "state":
        fragment_key = state_key
        hash_source = canonical_smiles or state_key
    else:
        fragment_key = sha256_text(canonical_smiles)
        hash_source = canonical_smiles
    fragment_hash = sha256_text(hash_source)
    record: dict[str, object] = {
        "molecule_id": str(molecule_id),
        "fragment_key": fragment_key,
        "fragment_hash256": fragment_hash,
        "canonical_smiles": canonical_smiles,
        "atom_count": atom_mask.bit_count(),
        "bond_count": bond_mask.bit_count(),
    }
    if debug_fields:
        atom_indices = indices_from_mask(atom_mask)
        bond_indices = indices_from_mask(bond_mask)
        protected = protected_units_hit(state_mask, units)
        record.update(
            {
                "fragment_smiles": canonical_smiles,
                "atom_indices_json": json.dumps(atom_indices, ensure_ascii=False),
                "bond_indices_json": json.dumps(bond_indices, ensure_ascii=False),
                "protected_units_hit_json": json.dumps(protected, ensure_ascii=False),
                "non_chon_hetero_atoms_json": json.dumps(
                    non_chon_hetero_atom_symbols(mol, atom_indices),
                    ensure_ascii=False,
                ),
            }
        )
    return record, canonical_smiles or fragment_key


def enumerate_compressed_fragment_records(
    mol: Chem.Mol,
    molecule_id: str,
    min_atoms: int,
    max_atoms: int | None,
    key_mode: str,
    dedupe: DedupeStore,
    debug_fields: bool,
    no_smiles: bool,
    limit_states: int | None,
    limit_fragments: int | None,
    stats: RunStats,
    shard_index: int = 0,
    shard_count: int = 1,
    root_unit_index: int | None = None,
    root_bucket_index: int = 0,
    root_bucket_count: int = 1,
) -> Iterator[dict[str, object]]:
    units = build_compressed_units(mol)
    stats.aromatic_system_count = units.aromatic_unit_count
    stats.unit_count = units.unit_count
    stats.ordinary_bond_unit_count = units.ordinary_bond_unit_count
    effective_max_atoms = max_atoms or len(heavy_atom_indices(mol))
    visited: set[int] = set()
    queued: set[int] = set()
    queue: deque[int] = deque()

    def lowest_unit_index(state: int) -> int:
        return (state & -state).bit_length() - 1

    def second_unit_bucket(state: int, root_unit: int) -> int:
        without_root = state & ~(1 << root_unit)
        if without_root == 0:
            return 0
        return lowest_unit_index(without_root) % root_bucket_count

    def state_owner_shard(state: int) -> int:
        first_unit_idx = (state & -state).bit_length() - 1
        return first_unit_idx % shard_count

    def enqueue(state: int) -> None:
        closed = close_state(state, units)
        if root_unit_index is None:
            if state_owner_shard(closed) != shard_index:
                return
        else:
            first_unit_idx = lowest_unit_index(closed)
            if first_unit_idx != root_unit_index:
                return
            is_root_singleton = closed == (1 << root_unit_index)
            if not is_root_singleton and second_unit_bucket(closed, root_unit_index) != root_bucket_index:
                return
        if closed not in visited and closed not in queued:
            queued.add(closed)
            queue.append(closed)

    if root_unit_index is None:
        for unit_idx in range(units.unit_count):
            enqueue(1 << unit_idx)
    else:
        enqueue(1 << root_unit_index)

    emitted = 0
    while queue:
        state = queue.popleft()
        queued.discard(state)
        if state in visited:
            continue
        visited.add(state)
        stats.visited_state_count = len(visited)
        if limit_states is not None and len(visited) > limit_states:
            break

        atom_mask, bond_mask = atom_bond_masks_for_state(state, units)
        atom_count = atom_mask.bit_count()
        if atom_count > effective_max_atoms:
            continue

        should_emit = True
        if root_unit_index is not None and state == (1 << root_unit_index) and root_bucket_index != 0:
            should_emit = False
        if should_emit and min_atoms <= atom_count <= effective_max_atoms:
            record, dedupe_key = make_fragment_record(
                mol,
                molecule_id,
                state,
                atom_mask,
                bond_mask,
                units,
                key_mode,
                debug_fields,
                no_smiles,
            )
            if dedupe.add(dedupe_key):
                emitted += 1
                stats.emitted_fragment_count = emitted
                yield record
                if limit_fragments is not None and emitted >= limit_fragments:
                    break

        frontier = 0
        for unit_idx in iter_bits(state):
            frontier |= units.adjacency_masks[unit_idx]
        frontier &= ~state
        for next_unit_idx in iter_bits(frontier):
            enqueue(state | (1 << next_unit_idx))


def peak_memory_mb() -> float | None:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return usage / 1024.0
    except Exception:
        return None


def done_marker_path(out_dir: Path, molecule_id: str) -> Path:
    return molecule_output_dir(out_dir, molecule_id) / "_SUCCESS"


def shard_plan_path(out_dir: Path, molecule_id: str) -> Path:
    return molecule_output_dir(out_dir, molecule_id) / "_SHARD_PLAN.json"


def error_path(out_dir: Path, molecule_id: str) -> Path:
    return molecule_output_dir(out_dir, molecule_id) / f"{molecule_id}.error.txt"


def molecule_output_dir(out_dir: Path, molecule_id: str) -> Path:
    return out_dir / f"molecule_id={molecule_id}"


def shard_error_path(out_dir: Path, molecule_id: str, shard_index: int) -> Path:
    return molecule_output_dir(out_dir, molecule_id) / f"shard-{shard_index:03d}.error.txt"


def cleanup_molecule_outputs(out_dir: Path, molecule_id: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    molecule_dir = molecule_output_dir(out_dir, molecule_id)
    if not molecule_dir.exists():
        return
    for path in molecule_dir.glob("*"):
        if path.is_file():
            path.unlink()
    try:
        molecule_dir.rmdir()
    except OSError:
        pass


def shard_done_marker_path(out_dir: Path, molecule_id: str, shard_index: int) -> Path:
    return molecule_output_dir(out_dir, molecule_id) / f"shard-{shard_index:03d}.done"


def shard_residue_paths(out_dir: Path, molecule_id: str, shard_index: int) -> list[Path]:
    molecule_dir = molecule_output_dir(out_dir, molecule_id)
    shard_label = f"shard-{shard_index:03d}"
    patterns = [
        f"{shard_label}.chunk-*.parquet",
        f"{shard_label}.chunk-*.parquet.tmp",
        f"{shard_label}.chunk-*.checksum",
        f"{shard_label}.dedupe.sqlite*",
        f"{shard_label}.error.txt",
    ]
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(path for path in molecule_dir.glob(pattern) if path.is_file())
    return sorted(paths)


def cleanup_shard_outputs(out_dir: Path, molecule_id: str, shard_index: int) -> None:
    for path in shard_residue_paths(out_dir, molecule_id, shard_index):
        path.unlink()


def finalize_molecule_chunks(out_dir: Path, molecule_id: str, checksum: bool) -> int:
    molecule_dir = molecule_output_dir(out_dir, molecule_id)
    shard_parts = sorted(molecule_dir.glob("shard-*.chunk-*.parquet"))
    for part_path in shard_parts:
        chunk_index = int(part_path.stem.rsplit("-", 1)[1])
        chunk_path = molecule_dir / f"chunk_{chunk_index:06d}.parquet"
        if chunk_path.exists():
            LOGGER.info(
                "molecule_id=%s chunk=%d finalize_skip=chunk_already_exists cleanup_temp=%s",
                molecule_id,
                chunk_index,
                part_path.name,
            )
            part_path.unlink()
            old_checksum = part_path.with_suffix(".checksum")
            if old_checksum.exists():
                old_checksum.unlink()
            if checksum and not chunk_path.with_suffix(".checksum").exists():
                chunk_path.with_suffix(".checksum").write_text(
                    calculate_file_checksum(chunk_path) + "\n",
                    encoding="utf-8",
                )
            continue
        part_path.replace(chunk_path)
        old_checksum = part_path.with_suffix(".checksum")
        if old_checksum.exists():
            old_checksum.unlink()
        if checksum:
            chunk_path.with_suffix(".checksum").write_text(
                calculate_file_checksum(chunk_path) + "\n",
                encoding="utf-8",
            )
    for path in molecule_dir.glob("shard-*.dedupe.sqlite*"):
        path.unlink()
    return len(sorted(molecule_dir.glob("chunk_*.parquet")))


def existing_chunk_rows(molecule_dir: Path) -> int:
    total_rows = 0
    chunks = sorted(molecule_dir.glob("chunk_*.parquet"))
    if not chunks:
        return 0
    try:
        import pyarrow.parquet as pq

        for chunk_path in chunks:
            total_rows += pq.ParquetFile(chunk_path).metadata.num_rows
    except Exception:
        LOGGER.warning("molecule_dir=%s existing_chunk_rows_unavailable=true", molecule_dir)
        return 0
    return total_rows


def build_shard_plan(molecule_id: str, unit_count: int, args: argparse.Namespace) -> dict[str, object]:
    requested_shards = args.shards if args.shards is not None else args.workers * 8
    effective_shards = max(int(requested_shards), unit_count)
    root_bucket_count = max(1, math.ceil(effective_shards / max(1, unit_count)))
    shard_count = unit_count * root_bucket_count
    return {
        "sharding_version": 1,
        "molecule_id": molecule_id,
        "unit_count": unit_count,
        "requested_shards": int(requested_shards),
        "effective_shards": effective_shards,
        "root_bucket_count": root_bucket_count,
        "shard_count": shard_count,
    }


def molecule_has_resume_state(molecule_dir: Path) -> bool:
    if not molecule_dir.exists():
        return False
    patterns = [
        "chunk_*.parquet",
        "shard-*.chunk-*.parquet",
        "shard-*.chunk-*.parquet.tmp",
        "shard-*.done",
        "shard-*.error.txt",
        "shard-*.dedupe.sqlite*",
    ]
    return any(any(molecule_dir.glob(pattern)) for pattern in patterns)


def ensure_shard_plan(out_dir: Path, molecule_id: str, plan: dict[str, object], force: bool) -> None:
    molecule_dir = molecule_output_dir(out_dir, molecule_id)
    plan_path = shard_plan_path(out_dir, molecule_id)
    molecule_dir.mkdir(parents=True, exist_ok=True)
    if force:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    if not plan_path.exists():
        if molecule_has_resume_state(molecule_dir):
            raise RuntimeError(
                f"Cannot safely resume molecule_id={molecule_id}: existing outputs are missing "
                f"{plan_path.name}. Re-run with the original version/configuration or use --force "
                "to rebuild this molecule from scratch."
            )
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    previous = json.loads(plan_path.read_text(encoding="utf-8"))
    locked_keys = [
        "sharding_version",
        "unit_count",
        "requested_shards",
        "effective_shards",
        "root_bucket_count",
        "shard_count",
    ]
    mismatches = {
        key: {"previous": previous.get(key), "current": plan.get(key)}
        for key in locked_keys
        if previous.get(key) != plan.get(key)
    }
    if mismatches:
        raise RuntimeError(
            f"Cannot resume molecule_id={molecule_id} with a different shard plan. "
            f"Keep --shards unchanged for unfinished molecules; --workers may be reduced. "
            f"mismatches={json.dumps(mismatches, ensure_ascii=False)}"
        )


def completed_shard_indices(out_dir: Path, molecule_id: str) -> set[int]:
    molecule_dir = molecule_output_dir(out_dir, molecule_id)
    indices: set[int] = set()
    for path in molecule_dir.glob("shard-*.done"):
        try:
            indices.add(int(path.stem.split("-", 1)[1]))
        except ValueError:
            continue
    return indices


def ensure_finalized_chunks_are_resumable(out_dir: Path, molecule_id: str, shard_count: int, force: bool) -> None:
    if force:
        return
    molecule_dir = molecule_output_dir(out_dir, molecule_id)
    if not any(molecule_dir.glob("chunk_*.parquet")):
        return
    completed = completed_shard_indices(out_dir, molecule_id)
    missing = [index for index in range(shard_count) if index not in completed]
    if missing:
        sample = ", ".join(f"shard-{index:03d}" for index in missing[:10])
        raise RuntimeError(
            f"Cannot safely resume molecule_id={molecule_id}: finalized chunk_*.parquet files exist "
            f"but {len(missing)} shard done markers are missing ({sample}). Formal chunks were kept; "
            "restore the missing shard-*.done markers or use --force to rebuild this molecule."
        )


def process_molecule_shard(task: dict[str, object]) -> dict[str, object]:
    configure_logging()
    molecule_id = str(task["molecule_id"])
    smiles = str(task["smiles"])
    out_dir = Path(str(task["out_dir"]))
    shard_index = int(task["shard_index"])
    shard_count = int(task["shard_count"])
    root_unit_index = task.get("root_unit_index")
    root_bucket_index = int(task.get("root_bucket_index", 0))
    root_bucket_count = int(task.get("root_bucket_count", 1))
    force = bool(task["force"])
    molecule_dir = molecule_output_dir(out_dir, molecule_id)
    shard_label = f"shard-{shard_index:03d}"
    shard_done_path = shard_done_marker_path(out_dir, molecule_id, shard_index)
    if shard_done_path.exists() and not force:
        stats_payload = json.loads(shard_done_path.read_text(encoding="utf-8"))
        return {
            "molecule_id": molecule_id,
            "shard_index": shard_index,
            "skipped": True,
            "reason": "shard done marker exists",
            "stats": stats_payload,
        }
    residue_paths = shard_residue_paths(out_dir, molecule_id, shard_index)
    if residue_paths and not force:
        LOGGER.info(
            "molecule_id=%s shard_index=%s cleanup=partial shard output without done residue_files=%d",
            molecule_id,
            shard_index,
            len(residue_paths),
        )
        cleanup_shard_outputs(out_dir, molecule_id, shard_index)

    start = time.time()
    stats = RunStats(molecule_id=f"{molecule_id}.{shard_label}")
    dedupe = DedupeStore(str(task["dedupe"]), molecule_dir / f"{shard_label}.dedupe.sqlite")
    sink = StreamingParquetSink(
        out_dir=molecule_dir,
        molecule_id=shard_label,
        batch_size=int(task["batch_size"]),
        compression=task["compression"],
        compression_level=task["compression_level"],
        checksum=bool(task["checksum"]),
        force=force,
        debug_fields=bool(task["debug_fields"]),
        log_molecule_id=molecule_id,
        log_start_time=float(task["log_start_time"]),
        chunk_counter=task.get("chunk_counter"),
        row_counter=task.get("row_counter"),
        counter_lock=task.get("counter_lock"),
    )
    try:
        mol = parse_smiles(smiles)
        stats.heavy_atom_count = len(heavy_atom_indices(mol))
        for record in enumerate_compressed_fragment_records(
            mol=mol,
            molecule_id=molecule_id,
            min_atoms=int(task["min_atoms"]),
            max_atoms=task["max_atoms"],
            key_mode=str(task["key_mode"]),
            dedupe=dedupe,
            debug_fields=bool(task["debug_fields"]),
            no_smiles=bool(task["no_smiles"]),
            limit_states=task["limit_states"],
            limit_fragments=task["limit_fragments"],
            stats=stats,
            shard_index=shard_index,
            shard_count=shard_count,
            root_unit_index=None if root_unit_index is None else int(root_unit_index),
            root_bucket_index=root_bucket_index,
            root_bucket_count=root_bucket_count,
        ):
            sink.add(record)
        sink.flush()
        dedupe.close()
        stats.written_part_count = sink.written_parts
        stats.elapsed_seconds = time.time() - start
        stats.fragments_per_second = (
            stats.emitted_fragment_count / stats.elapsed_seconds if stats.elapsed_seconds else 0.0
        )
        stats.peak_memory_mb = peak_memory_mb()
        shard_done_path.write_text(json.dumps(stats.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "molecule_id": molecule_id,
            "shard_index": shard_index,
            "skipped": False,
            "stats": stats.__dict__,
        }
    except Exception:
        dedupe.close()
        molecule_dir.mkdir(parents=True, exist_ok=True)
        shard_error_path(out_dir, molecule_id, shard_index).write_text(
            json.dumps(
                {
                    "molecule_id": molecule_id,
                    "shard_index": shard_index,
                    "shard_count": shard_count,
                    "smiles": smiles,
                    "traceback": traceback.format_exc(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise


def write_molecule_done(
    out_dir: Path,
    molecule_id: str,
    shard_results: list[dict[str, object]],
    checksum: bool,
    shard_plan: dict[str, object],
) -> None:
    stats_list = [result["stats"] for result in shard_results if "stats" in result]
    skipped = [result for result in shard_results if result.get("skipped")]
    elapsed = sum(float(stats.get("elapsed_seconds", 0.0)) for stats in stats_list)
    emitted = sum(int(stats.get("emitted_fragment_count", 0)) for stats in stats_list)
    visited = sum(int(stats.get("visited_state_count", 0)) for stats in stats_list)
    written = sum(int(stats.get("written_part_count", 0)) for stats in stats_list)
    payload = {
        "molecule_id": molecule_id,
        "shard_count": len(shard_results),
        "completed_shards": len(shard_results),
        "skipped_shards": len(skipped),
        "visited_state_count": visited,
        "emitted_fragment_count": emitted,
        "written_part_count": written,
        "worker_elapsed_seconds_sum": elapsed,
        "fragments_per_worker_second": emitted / elapsed if elapsed else 0.0,
        "shard_plan": shard_plan,
        "shards": shard_results,
    }
    payload["chunk_count"] = finalize_molecule_chunks(out_dir, molecule_id, checksum=checksum)
    done_marker_path(out_dir, molecule_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_csv_rows(path: Path, smiles_col: str, id_col: str) -> list[dict[str, str]]:
    delimiter = "\t" if path.suffix.lower() in {".tsv", ".tab"} else ","
    with path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file, delimiter=delimiter)
        if reader.fieldnames is None:
            raise ValueError("Input CSV has no header.")
        missing = {smiles_col, id_col} - set(reader.fieldnames)
        if missing:
            raise ValueError(f"Input CSV is missing columns: {sorted(missing)}")
        rows: list[dict[str, str]] = []
        for row in reader:
            smiles = str(row.get(smiles_col, "")).strip()
            molecule_id = str(row.get(id_col, "")).strip()
            if smiles and molecule_id:
                rows.append({"molecule_id": molecule_id, "smiles": smiles})
        return rows


def run_one_molecule(
    molecule_id: str,
    smiles: str,
    out_dir: Path,
    args: argparse.Namespace,
    compression: str | None,
    compression_level: int | None,
) -> None:
    done_path = done_marker_path(out_dir, molecule_id)
    if done_path.exists() and not args.force:
        LOGGER.info("molecule_id=%s skipped=success marker exists", molecule_id)
        return
    if args.force:
        cleanup_molecule_outputs(out_dir, molecule_id)

    molecule_dir = molecule_output_dir(out_dir, molecule_id)
    existing_chunk_count = len(list(molecule_dir.glob("chunk_*.parquet")))
    mol = parse_smiles(smiles)
    unit_count = build_compressed_units(mol).unit_count
    shard_plan = build_shard_plan(molecule_id, unit_count, args)
    ensure_shard_plan(out_dir, molecule_id, shard_plan, force=bool(args.force))
    root_bucket_count = int(shard_plan["root_bucket_count"])
    shard_count = int(shard_plan["shard_count"])
    ensure_finalized_chunks_are_resumable(out_dir, molecule_id, shard_count, force=bool(args.force))
    log_start_time = time.time()
    with Manager() as manager:
        chunk_counter = manager.Value("i", existing_chunk_count)
        row_counter = manager.Value("q", existing_chunk_rows(molecule_dir))
        counter_lock = manager.Lock()
        tasks: list[dict[str, object]] = []
        shard_index = 0
        for root_unit_index in range(unit_count):
            for root_bucket_index in range(root_bucket_count):
                tasks.append({
                    "molecule_id": molecule_id,
                    "smiles": smiles,
                    "out_dir": str(out_dir),
                    "shard_index": shard_index,
                    "shard_count": shard_count,
                    "root_unit_index": root_unit_index,
                    "root_bucket_index": root_bucket_index,
                    "root_bucket_count": root_bucket_count,
                    "force": args.force,
                    "batch_size": args.batch_size,
                    "compression": compression,
                    "compression_level": compression_level,
                    "checksum": args.checksum,
                    "debug_fields": args.debug_fields,
                    "dedupe": args.dedupe,
                    "key_mode": args.key_mode,
                    "min_atoms": args.min_atoms,
                    "max_atoms": args.max_atoms,
                    "limit_states": args.limit_states,
                    "limit_fragments": args.limit_fragments,
                    "no_smiles": args.no_smiles,
                    "log_start_time": log_start_time,
                    "chunk_counter": chunk_counter,
                    "row_counter": row_counter,
                    "counter_lock": counter_lock,
                })
                shard_index += 1
        LOGGER.info(
            "molecule_id=%s workers=%d shard_count=%d unit_count=%d requested_shards=%d root_bucket_count=%d out_dir=%s",
            molecule_id,
            args.workers,
            shard_count,
            unit_count,
            int(shard_plan["requested_shards"]),
            root_bucket_count,
            out_dir,
        )
        results: list[dict[str, object]] = []
        if args.workers <= 1:
            for task in tasks:
                result = process_molecule_shard(task)
                results.append(result)
                if result.get("skipped"):
                    LOGGER.info(
                        "molecule_id=%s shard_index=%s skipped=%s",
                        result["molecule_id"],
                        result["shard_index"],
                        result["reason"],
                    )
                else:
                    print_stats(result["stats"])
        else:
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                futures = [executor.submit(process_molecule_shard, task) for task in tasks]
                for future in as_completed(futures):
                    result = future.result()
                    results.append(result)
                    if result.get("skipped"):
                        LOGGER.info(
                            "molecule_id=%s shard_index=%s skipped=%s",
                            result["molecule_id"],
                            result["shard_index"],
                            result["reason"],
                        )
                    else:
                        print_stats(result["stats"])
    write_molecule_done(
        out_dir,
        molecule_id,
        sorted(results, key=lambda item: int(item["shard_index"])),
        checksum=bool(args.checksum),
        shard_plan=shard_plan,
    )


def print_stats(stats: dict[str, object]) -> None:
    keys = [
        "molecule_id",
        "heavy_atom_count",
        "aromatic_system_count",
        "unit_count",
        "ordinary_bond_unit_count",
        "visited_state_count",
        "emitted_fragment_count",
        "written_part_count",
        "elapsed_seconds",
        "fragments_per_second",
        "peak_memory_mb",
    ]
    LOGGER.info(" ".join(f"{key}={stats.get(key)}" for key in keys))


def atoms_from_bonds(mol: Chem.Mol, bond_indices: Iterable[int]) -> set[int]:
    atom_indices: set[int] = set()
    for bond_idx in bond_indices:
        bond = mol.GetBondWithIdx(int(bond_idx))
        atom_indices.add(bond.GetBeginAtomIdx())
        atom_indices.add(bond.GetEndAtomIdx())
    return atom_indices


def legacy_fragment_smiles_set(smiles: str, min_atoms: int, max_atoms: int | None) -> set[str]:
    mol = parse_smiles(smiles)
    aromatic_systems = build_aromatic_systems(mol)
    heavy_bonds = [
        bond.GetIdx()
        for bond in mol.GetBonds()
        if bond.GetBeginAtom().GetAtomicNum() > 1 and bond.GetEndAtom().GetAtomicNum() > 1
    ]
    bond_to_neighbors: dict[int, set[int]] = {bond_idx: set() for bond_idx in heavy_bonds}
    atom_to_bonds: dict[int, set[int]] = {}
    for bond_idx in heavy_bonds:
        bond = mol.GetBondWithIdx(bond_idx)
        atom_to_bonds.setdefault(bond.GetBeginAtomIdx(), set()).add(bond_idx)
        atom_to_bonds.setdefault(bond.GetEndAtomIdx(), set()).add(bond_idx)
    for bonds in atom_to_bonds.values():
        for bond_idx in bonds:
            bond_to_neighbors[bond_idx].update(bonds - {bond_idx})

    def close(bonds: Iterable[int]) -> tuple[frozenset[int], frozenset[int]]:
        closed_bonds = set(bonds)
        closed_atoms = atoms_from_bonds(mol, closed_bonds)
        changed = True
        while changed:
            changed = False
            for system in aromatic_systems:
                if closed_atoms & system["atoms"] or closed_bonds & system["bonds"]:
                    before = (len(closed_atoms), len(closed_bonds))
                    closed_atoms.update(system["atoms"])
                    closed_bonds.update(system["bonds"])
                    changed = before != (len(closed_atoms), len(closed_bonds))
        return frozenset(closed_atoms), frozenset(closed_bonds)

    effective_max = max_atoms or len(heavy_atom_indices(mol))
    queue: deque[frozenset[int]] = deque()
    queued: set[frozenset[int]] = set()
    visited: set[frozenset[int]] = set()
    output: set[str] = set()
    for bond_idx in heavy_bonds:
        _, closed_bonds = close({bond_idx})
        queued.add(closed_bonds)
        queue.append(closed_bonds)
    while queue:
        state = queue.popleft()
        queued.discard(state)
        if state in visited:
            continue
        visited.add(state)
        atoms, bonds = close(state)
        if min_atoms <= len(atoms) <= effective_max:
            output.add(fragment_smiles_from_masks(mol, mask_from_indices(atoms), mask_from_indices(bonds)))
        if len(atoms) > effective_max:
            continue
        frontier: set[int] = set()
        for bond_idx in bonds:
            frontier.update(bond_to_neighbors.get(bond_idx, set()))
        for next_bond in frontier - set(bonds):
            _, closed_bonds = close(set(bonds) | {next_bond})
            if closed_bonds not in visited and closed_bonds not in queued:
                queued.add(closed_bonds)
                queue.append(closed_bonds)
    return output


def new_fragment_smiles_set(smiles: str, min_atoms: int, max_atoms: int | None) -> set[str]:
    mol = parse_smiles(smiles)
    stats = RunStats(molecule_id="validation")
    dedupe = DedupeStore("memory")
    records = enumerate_compressed_fragment_records(
        mol=mol,
        molecule_id="validation",
        min_atoms=min_atoms,
        max_atoms=max_atoms,
        key_mode="hash",
        dedupe=dedupe,
        debug_fields=False,
        no_smiles=False,
        limit_states=None,
        limit_fragments=None,
        stats=stats,
    )
    return {str(record["canonical_smiles"]) for record in records}


def validate_small() -> int:
    cases = {
        "benzene": "c1ccccc1",
        "naphthalene": "c1ccc2ccccc2c1",
        "cyclohexane": "C1CCCCC1",
        "phenethylamine": "NCCc1ccccc1",
    }
    ok = True
    for name, smiles in cases.items():
        legacy = legacy_fragment_smiles_set(smiles, min_atoms=3, max_atoms=None)
        new = new_fragment_smiles_set(smiles, min_atoms=3, max_atoms=None)
        missing = legacy - new
        extra = new - legacy
        print(
            f"{name}: legacy={len(legacy)} new={len(new)} missing={len(missing)} extra={len(extra)}",
            flush=True,
        )
        if missing or extra:
            ok = False
            print(f"  missing_sample={sorted(missing)[:5]}", flush=True)
            print(f"  extra_sample={sorted(extra)[:5]}", flush=True)
    return 0 if ok else 1


def parse_optional_positive_int(value: str) -> int | None:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return None if parsed == 0 else parsed


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Enumerate non-induced heavy-atom fragments with aromatic systems compressed "
            "to protected bitmask units. Output keeps canonical_smiles and fragment_hash256 "
            "for offline dedupe by DuckDB/Polars/Spark."
        )
    )
    parser.add_argument("--smiles", help="Single input SMILES to enumerate.")
    parser.add_argument("--molecule-id", default="molecule", help="Molecule id used in records and output files.")
    parser.add_argument("--input", help="CSV/TSV task list. Molecules are processed sequentially, one at a time.")
    parser.add_argument("--smiles-col", default="smiles", help="SMILES column name in CSV/TSV.")
    parser.add_argument("--id-col", default="molecule_id", help="Molecule id column name in CSV/TSV.")
    parser.add_argument("--limit-molecules", type=int, default=None, help="Only process the first N input rows.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory.")
    parser.add_argument("--min-atoms", type=int, default=3, help="Minimum heavy atoms per fragment.")
    parser.add_argument(
        "--max-atoms",
        type=parse_optional_positive_int,
        default=None,
        help="Maximum heavy atoms per fragment. Defaults to full molecule; use 0 for default.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Worker processes for this one SMILES. Each worker owns a disjoint state shard.",
    )
    parser.add_argument(
        "--shards",
        type=int,
        default=None,
        help="Fine-grained shard target per molecule. Defaults to workers * 8.",
    )
    parser.add_argument("--batch-size", type=int, default=500000, help="Rows per Parquet part.")
    parser.add_argument("--compression", choices=["zstd", "snappy", "none"], default="zstd")
    parser.add_argument("--compression-level", type=int, default=1)
    parser.add_argument("--dedupe", choices=["none", "memory", "sqlite"], default="none")
    parser.add_argument("--key-mode", choices=["state", "hash"], default="state")
    parser.add_argument("--debug-fields", action="store_true", help="Write debug JSON columns.")
    parser.add_argument("--checksum", action="store_true", help="Write sha256 checksum files.")
    parser.add_argument("--resume", action="store_true", default=True, help="Skip done molecules.")
    parser.add_argument("--force", action="store_true", help="Re-run even if done marker exists.")
    parser.add_argument("--limit-states", type=int, default=None, help="Stop after N visited states.")
    parser.add_argument("--limit-fragments", type=int, default=None, help="Stop after N emitted fragments.")
    parser.add_argument(
        "--no-smiles",
        action="store_true",
        help=(
            "Fast state-only mode: leave canonical_smiles empty and hash fragment_key/state. "
            "Use only when downstream does not need per-fragment SMILES from this step."
        ),
    )
    parser.add_argument("--validate-small", action="store_true", help="Run small old-vs-new validation.")
    return parser


def main() -> None:
    configure_logging()
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.validate_small:
        raise SystemExit(validate_small())
    if bool(args.input) == bool(args.smiles):
        parser.error("Use exactly one input mode: --smiles or --input.")
    if args.no_smiles and args.key_mode == "hash":
        parser.error("--no-smiles cannot be combined with --key-mode hash.")
    if args.no_smiles and args.dedupe in {"memory", "sqlite"}:
        parser.error("--no-smiles with dedupe memory/sqlite would dedupe state keys, not SMILES; use dedupe=none.")
    if args.workers < 1:
        parser.error("--workers must be >= 1.")
    if args.shards is not None and args.shards < 1:
        parser.error("--shards must be >= 1.")
    if args.workers > 1 and args.dedupe != "none":
        parser.error("--workers > 1 requires --dedupe none; do canonical/global dedupe after shard output.")
    compression_level = None if args.compression in {"snappy", "none"} else args.compression_level
    compression = None if args.compression == "none" else args.compression
    out_dir = Path(args.out_dir)

    if args.smiles:
        run_one_molecule(
            molecule_id=str(args.molecule_id),
            smiles=str(args.smiles),
            out_dir=out_dir,
            args=args,
            compression=compression,
            compression_level=compression_level,
        )
    else:
        rows = read_csv_rows(
            Path(args.input),
            smiles_col=args.smiles_col,
            id_col=args.id_col,
        )
        if args.limit_molecules is not None:
            rows = rows[: args.limit_molecules]
        LOGGER.info(
            "loaded_input_molecules=%d workers_per_molecule=%d out_dir=%s",
            len(rows),
            args.workers,
            out_dir,
        )
        for index, row in enumerate(rows, start=1):
            LOGGER.info(
                "input_progress=%d/%d molecule_id=%s start",
                index,
                len(rows),
                row["molecule_id"],
            )
            run_one_molecule(
                molecule_id=row["molecule_id"],
                smiles=row["smiles"],
                out_dir=out_dir,
                args=args,
                compression=compression,
                compression_level=compression_level,
            )


if __name__ == "__main__":
    main()
