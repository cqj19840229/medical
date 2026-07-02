from __future__ import annotations

import argparse
import ctypes
import csv
import gc
import json
import math
import os
import socket
import subprocess
import sys
import time
import traceback
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from multiprocessing import Manager
from pathlib import Path
from typing import Any, Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
PARENT_DIR = SCRIPT_DIR.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

try:
    from bond_enumerated_non_induced_fragments import (
        DedupeStore,
        LOGGER,
        RunStats,
        atom_bond_masks_for_state,
        build_compressed_units,
        close_state,
        configure_logging,
        done_marker_path,
        enumerate_compressed_fragment_records,
        existing_chunk_rows,
        heavy_atom_indices,
        parse_optional_positive_int,
        parse_smiles,
        peak_memory_mb,
        print_stats,
        read_csv_rows,
        shard_done_marker_path,
        shard_error_path,
        shard_plan_path,
        shard_residue_paths,
        validate_small,
    )
    from parquet_writer import calculate_file_checksum, write_chunk_parquet
except ImportError:  # pragma: no cover - supports package-style execution.
    from .bond_enumerated_non_induced_fragments import (
        DedupeStore,
        LOGGER,
        RunStats,
        atom_bond_masks_for_state,
        build_compressed_units,
        close_state,
        configure_logging,
        done_marker_path,
        enumerate_compressed_fragment_records,
        existing_chunk_rows,
        heavy_atom_indices,
        parse_optional_positive_int,
        parse_smiles,
        peak_memory_mb,
        print_stats,
        read_csv_rows,
        shard_done_marker_path,
        shard_error_path,
        shard_plan_path,
        shard_residue_paths,
        validate_small,
    )
    from .parquet_writer import calculate_file_checksum, write_chunk_parquet


try:
    from fast_fragment_bridge import HAS_FAST_CORE, enumerate_compressed_fragment_records_fast
except Exception:  # pragma: no cover - optional C++ extension path.
    HAS_FAST_CORE = False
    enumerate_compressed_fragment_records_fast = None  # type: ignore[assignment]


DEFAULT_OUT_DIR = Path("/mnt/datadisk/drug_fragment/build_005")
SHARDING_VERSION = 4
SUMMARY_FIELDS = [
    "molecule_id",
    "wall_hours",
    "wall_elapsed_seconds",
    "worker_elapsed_seconds_sum",
    "average_effective_parallelism",
    "emitted_fragment_count",
    "visited_state_count",
    "chunk_count",
    "fragments_per_wall_second",
    "started_at",
    "finished_at",
]

_LAST_TAIL_ENABLED_LOG_AT = 0.0
_LAST_TAIL_BLOCKED_LOG_AT = 0.0


def trim_process_memory() -> None:
    gc.collect()
    if os.name != "posix":
        return
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        return


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def molecule_output_dir(out_dir: Path, molecule_id: str) -> Path:
    return out_dir / f"molecule_id={molecule_id}"


def molecule_lock_path(out_dir: Path, molecule_id: str) -> Path:
    return molecule_output_dir(out_dir, molecule_id) / "_RUNNING.lock"


def molecule_error_json_path(out_dir: Path, molecule_id: str) -> Path:
    return molecule_output_dir(out_dir, molecule_id) / "molecule.error.json"


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lock_payload(molecule_id: str) -> dict[str, object]:
    return {
        "molecule_id": molecule_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": utc_now_iso(),
        "argv": sys.argv,
    }


def acquire_molecule_lock(out_dir: Path, molecule_id: str, stale_lock_minutes: float) -> bool:
    molecule_dir = molecule_output_dir(out_dir, molecule_id)
    molecule_dir.mkdir(parents=True, exist_ok=True)
    path = molecule_lock_path(out_dir, molecule_id)
    payload = json.dumps(lock_payload(molecule_id), ensure_ascii=False, indent=2)
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(payload)
            return True
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
            pid = int(existing.get("pid") or 0)
            if pid_is_running(pid):
                LOGGER.info("molecule_id=%s skipped=running_lock pid=%s", molecule_id, pid)
                return False
            if pid:
                LOGGER.info("molecule_id=%s cleanup=dead_lock pid=%s", molecule_id, pid)
                path.unlink(missing_ok=True)
                continue
            age_seconds = time.time() - path.stat().st_mtime
            stale_seconds = max(0.0, stale_lock_minutes) * 60.0
            if age_seconds < stale_seconds:
                LOGGER.info(
                    "molecule_id=%s skipped=recent_stale_lock age_seconds=%.1f",
                    molecule_id,
                    age_seconds,
                )
                return False
            LOGGER.info(
                "molecule_id=%s cleanup=stale_lock pid=%s age_seconds=%.1f",
                molecule_id,
                pid,
                age_seconds,
            )
            path.unlink(missing_ok=True)


def release_molecule_lock(out_dir: Path, molecule_id: str) -> None:
    molecule_lock_path(out_dir, molecule_id).unlink(missing_ok=True)


def cleanup_molecule_outputs_keep_lock(out_dir: Path, molecule_id: str) -> None:
    molecule_dir = molecule_output_dir(out_dir, molecule_id)
    if not molecule_dir.exists():
        return
    lock_name = molecule_lock_path(out_dir, molecule_id).name
    for path in molecule_dir.glob("*"):
        if path.name == lock_name:
            continue
        if path.is_file():
            path.unlink()
    for path in sorted(molecule_dir.glob("*"), reverse=True):
        if path.name == lock_name:
            continue
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def cleanup_shard_outputs(out_dir: Path, molecule_id: str, shard_index: int) -> None:
    for path in shard_residue_paths(out_dir, molecule_id, shard_index):
        path.unlink()


def build_shard_plan(molecule_id: str, unit_count: int, requested_shards: int) -> dict[str, object]:
    effective_shards = max(int(requested_shards), unit_count)
    root_bucket_count = max(1, math.ceil(effective_shards / max(1, unit_count)))
    shard_count = unit_count * root_bucket_count
    return {
        "sharding_version": SHARDING_VERSION,
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
                f"{plan_path.name}. Use --force to rebuild this molecule from scratch."
            )
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    previous = json.loads(plan_path.read_text(encoding="utf-8"))
    locked_keys = [
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
            "Keep --shards unchanged for unfinished molecules, or use --force to rebuild from scratch. "
            f"mismatches={json.dumps(mismatches, ensure_ascii=False)}"
        )
    if previous.get("sharding_version") != SHARDING_VERSION:
        raise RuntimeError(
            f"Cannot resume molecule_id={molecule_id} with a different shard plan. "
            "Keep --shards unchanged for unfinished molecules, or use --force to rebuild from scratch."
        )


def completed_shard_indices(out_dir: Path, molecule_id: str) -> set[int]:
    indices: set[int] = set()
    for path in molecule_output_dir(out_dir, molecule_id).glob("shard-*.done"):
        try:
            indices.add(int(path.stem.split("-", 1)[1]))
        except ValueError:
            continue
    return indices


def load_completed_shard_results(out_dir: Path, molecule_id: str) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for shard_index in sorted(completed_shard_indices(out_dir, molecule_id)):
        done_path = shard_done_marker_path(out_dir, molecule_id, shard_index)
        try:
            stats = json.loads(done_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        results.append(
            {
                "molecule_id": molecule_id,
                "shard_index": shard_index,
                "skipped": True,
                "reason": "shard done marker exists",
                "stats": stats,
            }
        )
    return results


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
            f"but {len(missing)} shard done markers are missing ({sample}). "
            "Restore the missing shard-*.done markers or use --force."
        )


class ColumnarParquetSink:
    def __init__(
        self,
        out_dir: Path,
        shard_label: str,
        batch_size: int,
        compression: str | None,
        compression_level: int | None,
        checksum: bool,
        force: bool,
        debug_fields: bool,
        log_molecule_id: str,
        log_start_time: float,
        chunk_counter: Any,
        row_counter: Any,
        counter_lock: Any,
    ) -> None:
        self.out_dir = out_dir
        self.shard_label = shard_label
        self.batch_size = batch_size
        self.compression = compression
        self.compression_level = compression_level
        self.checksum = checksum
        self.force = force
        self.debug_fields = debug_fields
        self.log_molecule_id = log_molecule_id
        self.log_start_time = log_start_time
        self.chunk_counter = chunk_counter
        self.row_counter = row_counter
        self.counter_lock = counter_lock
        self.written_parts = 0
        self.columns: dict[str, list[Any]] = {
            "molecule_id": [],
            "fragment_key": [],
            "fragment_hash256": [],
            "canonical_smiles": [],
            "atom_count": [],
            "bond_count": [],
        }
        if debug_fields:
            self.columns.update(
                {
                    "fragment_smiles": [],
                    "atom_indices_json": [],
                    "bond_indices_json": [],
                    "protected_units_hit_json": [],
                    "non_chon_hetero_atoms_json": [],
                }
            )
        out_dir.mkdir(parents=True, exist_ok=True)

    def add(self, record: dict[str, object]) -> None:
        for key in self.columns:
            self.columns[key].append(record.get(key, ""))
        if len(self.columns["molecule_id"]) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        record_count = len(self.columns["molecule_id"])
        if record_count == 0:
            return
        chunk_index, fragment_rows = self._reserve_chunk(record_count)
        output_path = self.out_dir / f"{self.shard_label}.chunk-{chunk_index:06d}.parquet"
        if output_path.exists() and not self.force:
            raise FileExistsError(f"Refusing to overwrite existing parquet file: {output_path}")
        write_chunk_parquet(
            self.columns,
            output_path,
            compression=self.compression,
            compression_level=self.compression_level,
            checksum=self.checksum,
            overwrite=self.force,
        )
        for values in self.columns.values():
            values.clear()
        self.written_parts += 1
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

    def _reserve_chunk(self, record_count: int) -> tuple[int, int]:
        with self.counter_lock:
            chunk_index = int(self.chunk_counter.get()) + 1
            fragment_rows = int(self.row_counter.get()) + record_count
            self.chunk_counter.set(chunk_index)
            self.row_counter.set(fragment_rows)
        return chunk_index, fragment_rows


def process_molecule_shard(task: dict[str, object]) -> dict[str, object]:
    configure_logging()
    molecule_id = str(task["molecule_id"])
    smiles = str(task["smiles"])
    out_dir = Path(str(task["out_dir"]))
    shard_index = int(task["shard_index"])
    shard_count = int(task["shard_count"])
    root_unit_index = int(task["root_unit_index"])
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
    sink = ColumnarParquetSink(
        out_dir=molecule_dir,
        shard_label=shard_label,
        batch_size=int(task["batch_size"]),
        compression=task["compression"],
        compression_level=task["compression_level"],
        checksum=bool(task["checksum"]),
        force=force,
        debug_fields=bool(task["debug_fields"]),
        log_molecule_id=molecule_id,
        log_start_time=float(task["log_start_time"]),
        chunk_counter=task["chunk_counter"],
        row_counter=task["row_counter"],
        counter_lock=task["counter_lock"],
    )
    try:
        mol = parse_smiles(smiles)
        stats.heavy_atom_count = len(heavy_atom_indices(mol))
        fast_core_mode = str(task.get("fast_core", "auto"))
        use_cpp_core = (
            fast_core_mode in {"auto", "cpp"}
            and HAS_FAST_CORE
            and enumerate_compressed_fragment_records_fast is not None
        )
        if fast_core_mode == "cpp" and not use_cpp_core:
            raise RuntimeError(
                "--fast-core cpp was requested, but fast_fragment_core is not built/importable. "
                "Run build_fast_fragment_core.sh first, or use --fast-core python."
            )
        if use_cpp_core:
            LOGGER.info(
                "molecule_id=%s shard_index=%s fast_core=cpp cpp_batch_size=%s",
                molecule_id,
                shard_index,
                task.get("cpp_batch_size"),
            )
            record_iter = enumerate_compressed_fragment_records_fast(
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
                root_unit_index=root_unit_index,
                root_bucket_index=root_bucket_index,
                root_bucket_count=root_bucket_count,
                cpp_batch_size=int(task.get("cpp_batch_size", 8192)),
            )
        else:
            if fast_core_mode == "auto":
                LOGGER.info(
                    "molecule_id=%s shard_index=%s fast_core=python reason=cpp_extension_unavailable",
                    molecule_id,
                    shard_index,
                )
            record_iter = enumerate_compressed_fragment_records(
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
                root_unit_index=root_unit_index,
                root_bucket_index=root_bucket_index,
                root_bucket_count=root_bucket_count,
            )
        for record in record_iter:
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
        del record_iter
        del mol
        trim_process_memory()
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
        trim_process_memory()
        raise


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
            part_path.with_suffix(".checksum").unlink(missing_ok=True)
            if checksum and not chunk_path.with_suffix(".checksum").exists():
                chunk_path.with_suffix(".checksum").write_text(
                    calculate_file_checksum(chunk_path) + "\n",
                    encoding="utf-8",
                )
            continue
        part_path.replace(chunk_path)
        part_path.with_suffix(".checksum").unlink(missing_ok=True)
        if checksum:
            chunk_path.with_suffix(".checksum").write_text(
                calculate_file_checksum(chunk_path) + "\n",
                encoding="utf-8",
            )
    for path in molecule_dir.glob("shard-*.dedupe.sqlite*"):
        path.unlink()
    return len(sorted(molecule_dir.glob("chunk_*.parquet")))


def write_molecule_success(
    context: "MoleculeContext",
    args: argparse.Namespace,
    checksum: bool,
) -> None:
    stats_list = [result["stats"] for result in context.results if "stats" in result]
    skipped = [result for result in context.results if result.get("skipped")]
    worker_elapsed = sum(float(stats.get("elapsed_seconds", 0.0)) for stats in stats_list)
    emitted = sum(int(stats.get("emitted_fragment_count", 0)) for stats in stats_list)
    visited = sum(int(stats.get("visited_state_count", 0)) for stats in stats_list)
    written = sum(int(stats.get("written_part_count", 0)) for stats in stats_list)
    finished_at = utc_now_iso()
    wall_elapsed = time.time() - context.wall_start
    chunk_count = finalize_molecule_chunks(context.out_dir, context.molecule_id, checksum=checksum)
    payload = {
        "molecule_id": context.molecule_id,
        "wall_elapsed_seconds": wall_elapsed,
        "wall_hours": wall_elapsed / 3600.0,
        "worker_elapsed_seconds_sum": worker_elapsed,
        "average_effective_parallelism": worker_elapsed / wall_elapsed if wall_elapsed else 0.0,
        "visited_state_count": visited,
        "emitted_fragment_count": emitted,
        "written_part_count": written,
        "chunk_count": chunk_count,
        "fragments_per_wall_second": emitted / wall_elapsed if wall_elapsed else 0.0,
        "total_workers": args.total_workers,
        "active_molecules": args.active_molecules,
        "batch_size": args.batch_size,
        "smiles_mode": "no-smiles" if args.no_smiles else "with-smiles",
        "canonical_smiles_enabled": not args.no_smiles,
        "hash_source": "state_key" if args.no_smiles else "canonical_smiles",
        "fast_core": args.fast_core,
        "cpp_core_available": bool(HAS_FAST_CORE),
        "cpp_batch_size": args.cpp_batch_size,
        "checksum": bool(checksum),
        "started_at": context.started_at,
        "finished_at": finished_at,
        "shard_count": context.shard_count,
        "completed_shards": len(context.results),
        "skipped_shards": len(skipped),
        "shard_plan": context.shard_plan,
        "shards": sorted(context.results, key=lambda item: int(item["shard_index"])),
    }
    done_marker_path(context.out_dir, context.molecule_id).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_molecule_error(context: "MoleculeContext", message: str) -> None:
    molecule_error_json_path(context.out_dir, context.molecule_id).write_text(
        json.dumps(
            {
                "molecule_id": context.molecule_id,
                "error": message,
                "traceback": context.error_traceback,
                "failed_at": utc_now_iso(),
                "completed_shards": len(context.results),
                "remaining_shards": len(context.tasks),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@dataclass
class MoleculeContext:
    molecule_id: str
    smiles: str
    out_dir: Path
    shard_plan: dict[str, object]
    tasks: deque[dict[str, object]]
    chunk_counter: Any
    row_counter: Any
    counter_lock: Any
    wall_start: float
    started_at: str
    shard_count: int
    results: list[dict[str, object]] = field(default_factory=list)
    submitted: int = 0
    in_flight: int = 0
    failed: bool = False
    error_message: str = ""
    error_traceback: str = ""

    @property
    def done(self) -> bool:
        return not self.failed and self.in_flight == 0 and not self.tasks

    @property
    def failed_done(self) -> bool:
        return self.failed and self.in_flight == 0


@dataclass
class TailActiveState:
    total_missing: int
    tail_molecule_count: int
    non_tail_molecule_count: int
    details: list[tuple[str, int, bool]]


def make_shard_tasks(
    molecule_id: str,
    smiles: str,
    out_dir: Path,
    shard_plan: dict[str, object],
    args: argparse.Namespace,
    compression: str | None,
    compression_level: int | None,
    log_start_time: float,
    chunk_counter: Any,
    row_counter: Any,
    counter_lock: Any,
) -> deque[dict[str, object]]:
    completed = completed_shard_indices(out_dir, molecule_id) if not args.force else set()
    root_bucket_count = int(shard_plan["root_bucket_count"])
    unit_count = int(shard_plan["unit_count"])
    tasks: deque[dict[str, object]] = deque()
    shard_index = 0
    for root_unit_index in range(unit_count):
        for root_bucket_index in range(root_bucket_count):
            if shard_index not in completed:
                tasks.append(
                    {
                        "molecule_id": molecule_id,
                        "smiles": smiles,
                        "out_dir": str(out_dir),
                        "shard_index": shard_index,
                        "shard_count": int(shard_plan["shard_count"]),
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
                        "fast_core": args.fast_core,
                        "cpp_batch_size": args.cpp_batch_size,
                        "log_start_time": log_start_time,
                        "chunk_counter": chunk_counter,
                        "row_counter": row_counter,
                        "counter_lock": counter_lock,
                    }
                )
            shard_index += 1
    return tasks


def activate_molecule(
    row: dict[str, str],
    out_dir: Path,
    args: argparse.Namespace,
    manager: Any,
    compression: str | None,
    compression_level: int | None,
) -> MoleculeContext | None:
    molecule_id = str(row["molecule_id"])
    smiles = str(row["smiles"])
    done_path = done_marker_path(out_dir, molecule_id)
    if done_path.exists() and not args.force:
        LOGGER.info("molecule_id=%s skipped=success marker exists", molecule_id)
        return None
    if not acquire_molecule_lock(out_dir, molecule_id, args.stale_lock_minutes):
        return None
    try:
        if args.force:
            cleanup_molecule_outputs_keep_lock(out_dir, molecule_id)
        molecule_dir = molecule_output_dir(out_dir, molecule_id)
        existing_chunk_count = len(list(molecule_dir.glob("chunk_*.parquet")))
        mol = parse_smiles(smiles)
        unit_count = build_compressed_units(mol).unit_count
        shard_plan = build_shard_plan(molecule_id, unit_count, args.shards)
        ensure_shard_plan(out_dir, molecule_id, shard_plan, force=bool(args.force))
        shard_count = int(shard_plan["shard_count"])
        ensure_finalized_chunks_are_resumable(out_dir, molecule_id, shard_count, force=bool(args.force))
        wall_start = time.time()
        started_at = utc_now_iso()
        chunk_counter = manager.Value("i", existing_chunk_count)
        row_counter = manager.Value("q", existing_chunk_rows(molecule_dir))
        counter_lock = manager.Lock()
        tasks = make_shard_tasks(
            molecule_id=molecule_id,
            smiles=smiles,
            out_dir=out_dir,
            shard_plan=shard_plan,
            args=args,
            compression=compression,
            compression_level=compression_level,
            log_start_time=wall_start,
            chunk_counter=chunk_counter,
            row_counter=row_counter,
            counter_lock=counter_lock,
        )
        LOGGER.info(
            "molecule_id=%s total_workers=%d active_molecules=%d shard_count=%d unit_count=%d "
            "requested_shards=%d root_bucket_count=%d pending_shards=%d out_dir=%s",
            molecule_id,
            args.total_workers,
            args.active_molecules,
            shard_count,
            unit_count,
            int(shard_plan["requested_shards"]),
            int(shard_plan["root_bucket_count"]),
            len(tasks),
            out_dir,
        )
        if not tasks:
            context = MoleculeContext(
                molecule_id=molecule_id,
                smiles=smiles,
                out_dir=out_dir,
                shard_plan=shard_plan,
                tasks=tasks,
                chunk_counter=chunk_counter,
                row_counter=row_counter,
                counter_lock=counter_lock,
                wall_start=wall_start,
                started_at=started_at,
                shard_count=shard_count,
                results=[] if args.force else load_completed_shard_results(out_dir, molecule_id),
            )
            write_molecule_success(context, args, checksum=bool(args.checksum))
            release_molecule_lock(out_dir, molecule_id)
            LOGGER.info("molecule_id=%s finalized=already_completed_shards", molecule_id)
            return None
        return MoleculeContext(
            molecule_id=molecule_id,
            smiles=smiles,
            out_dir=out_dir,
            shard_plan=shard_plan,
            tasks=tasks,
            chunk_counter=chunk_counter,
            row_counter=row_counter,
            counter_lock=counter_lock,
            wall_start=wall_start,
            started_at=started_at,
            shard_count=shard_count,
            results=[] if args.force else load_completed_shard_results(out_dir, molecule_id),
        )
    except Exception:
        release_molecule_lock(out_dir, molecule_id)
        raise


def submit_pending_tasks(
    executor: ProcessPoolExecutor,
    active: list[MoleculeContext],
    futures: dict[Future[dict[str, object]], MoleculeContext],
    max_pending_tasks: int,
) -> None:
    while len(futures) < max_pending_tasks:
        submitted_any = False
        for context in list(active):
            if context.failed or not context.tasks or len(futures) >= max_pending_tasks:
                continue
            task = context.tasks.popleft()
            future = executor.submit(process_molecule_shard, task)
            futures[future] = context
            context.submitted += 1
            context.in_flight += 1
            submitted_any = True
        if not submitted_any:
            break


def active_remaining_shards(active: list[MoleculeContext]) -> int:
    return sum(len(context.tasks) + context.in_flight for context in active if not context.failed)


def compute_tail_active_state(active: list[MoleculeContext], args: argparse.Namespace) -> TailActiveState:
    total_missing = 0
    tail_molecule_count = 0
    non_tail_molecule_count = 0
    details: list[tuple[str, int, bool]] = []
    for context in active:
        if context.failed:
            continue
        missing = len(context.tasks) + context.in_flight
        is_tail = missing <= args.tail_molecule_shards
        total_missing += missing
        if is_tail:
            tail_molecule_count += 1
        else:
            non_tail_molecule_count += 1
        details.append((context.molecule_id, missing, is_tail))
    return TailActiveState(
        total_missing=total_missing,
        tail_molecule_count=tail_molecule_count,
        non_tail_molecule_count=non_tail_molecule_count,
        details=details,
    )


def read_mem_available_gb() -> float | None:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) / 1024.0 / 1024.0
    except Exception:
        return None
    return None


def _rss_gb_from_ps_process_group() -> float | None:
    if os.name != "posix":
        return None
    try:
        completed = subprocess.run(
            ["ps", "-o", "rss=", "-g", str(os.getpgrp())],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if completed.returncode != 0:
            return None
        rss_kb = 0
        for line in completed.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            rss_kb += int(line)
        return rss_kb / 1024.0 / 1024.0
    except Exception:
        return None


def _rss_gb_from_current_process_tree() -> float | None:
    try:
        import psutil  # type: ignore[import-not-found]

        process = psutil.Process(os.getpid())
        rss_bytes = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                rss_bytes += child.memory_info().rss
            except Exception:
                continue
        return rss_bytes / 1024.0 / 1024.0 / 1024.0
    except Exception:
        pass

    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) / 1024.0 / 1024.0
    except Exception:
        return None
    return None


def current_process_group_rss_gb() -> float | None:
    rss_gb = _rss_gb_from_ps_process_group()
    if rss_gb is not None:
        return rss_gb
    return _rss_gb_from_current_process_tree()


def _format_optional_gb(value: float | None) -> str:
    return "None" if value is None else f"{value:.1f}"


def _format_tail_details(details: list[tuple[str, int, bool]]) -> str:
    return ",".join(f"{molecule_id}:{missing}:{'T' if is_tail else 'N'}" for molecule_id, missing, is_tail in details)


def _tail_memory_snapshot(args: argparse.Namespace) -> tuple[bool, float | None, float | None]:
    rss_gb = current_process_group_rss_gb()
    mem_available_gb = read_mem_available_gb()
    if args.tail_rss_soft_max_gb > 0 and rss_gb is not None and rss_gb >= args.tail_rss_soft_max_gb:
        return False, rss_gb, mem_available_gb
    if (
        args.tail_mem_available_min_gb > 0
        and mem_available_gb is not None
        and mem_available_gb <= args.tail_mem_available_min_gb
    ):
        return False, rss_gb, mem_available_gb
    return True, rss_gb, mem_available_gb


def memory_ok_for_tail_open(args: argparse.Namespace) -> bool:
    ok, _, _ = _tail_memory_snapshot(args)
    return ok


def effective_active_limit(args: argparse.Namespace, active: list[MoleculeContext]) -> int:
    global _LAST_TAIL_BLOCKED_LOG_AT, _LAST_TAIL_ENABLED_LOG_AT

    base = args.active_molecules
    tail_max = max(base, args.tail_active_molecules)
    if tail_max <= base or len(active) < base:
        return base

    state = compute_tail_active_state(active, args)
    trigger_total = args.total_workers * args.tail_trigger_shards_factor
    trigger_by_total_missing = state.total_missing < trigger_total
    trigger_by_tail_molecules = (
        state.tail_molecule_count >= args.tail_min_tail_molecules
        and state.non_tail_molecule_count >= args.tail_min_nontail_molecules
    )
    if not trigger_by_total_missing and not trigger_by_tail_molecules:
        return base

    if trigger_by_total_missing:
        desired_limit = tail_max
    else:
        desired_limit = min(tail_max, base + state.tail_molecule_count)
    if desired_limit <= base:
        return base

    memory_ok, rss_gb, mem_available_gb = _tail_memory_snapshot(args)
    now = time.time()
    log_interval = max(0, args.tail_log_interval_seconds)
    if not memory_ok:
        if now - _LAST_TAIL_BLOCKED_LOG_AT >= log_interval:
            _LAST_TAIL_BLOCKED_LOG_AT = now
            LOGGER.info(
                "tail_active blocked reason=memory active=%d total_missing=%d tail_molecules=%d "
                "non_tail_molecules=%d rss_gb=%s mem_available_gb=%s",
                len(state.details),
                state.total_missing,
                state.tail_molecule_count,
                state.non_tail_molecule_count,
                _format_optional_gb(rss_gb),
                _format_optional_gb(mem_available_gb),
            )
        return base

    if now - _LAST_TAIL_ENABLED_LOG_AT >= log_interval:
        _LAST_TAIL_ENABLED_LOG_AT = now
        reason = "tail_molecules" if trigger_by_tail_molecules else "total_missing"
        LOGGER.info(
            "tail_active enabled reason=%s active=%d limit=%d total_missing=%d trigger_total=%.0f "
            "tail_molecules=%d non_tail_molecules=%d tail_molecule_shards=%d rss_gb=%s "
            "mem_available_gb=%s details=%s",
            reason,
            len(state.details),
            desired_limit,
            state.total_missing,
            trigger_total,
            state.tail_molecule_count,
            state.non_tail_molecule_count,
            args.tail_molecule_shards,
            _format_optional_gb(rss_gb),
            _format_optional_gb(mem_available_gb),
            _format_tail_details(state.details),
        )
    return desired_limit


def handle_completed_future(
    future: Future[dict[str, object]],
    context: MoleculeContext,
    active: list[MoleculeContext],
    args: argparse.Namespace,
) -> None:
    context.in_flight -= 1
    try:
        result = future.result()
    except Exception as exc:
        context.failed = True
        context.tasks.clear()
        context.error_message = str(exc)
        context.error_traceback = traceback.format_exc()
        LOGGER.error("molecule_id=%s shard_failed error=%s", context.molecule_id, exc)
        return
    context.results.append(result)
    if result.get("skipped"):
        LOGGER.info(
            "molecule_id=%s shard_index=%s skipped=%s",
            result["molecule_id"],
            result["shard_index"],
            result["reason"],
        )
    else:
        print_stats(result["stats"])
    if context.done:
        write_molecule_success(context, args, checksum=bool(args.checksum))
        release_molecule_lock(context.out_dir, context.molecule_id)
        active.remove(context)
        LOGGER.info(
            "molecule_id=%s success shards=%d",
            context.molecule_id,
            len(context.results),
        )


def finish_failed_contexts(active: list[MoleculeContext], args: argparse.Namespace) -> None:
    for context in list(active):
        if not context.failed_done:
            continue
        write_molecule_error(context, context.error_message)
        release_molecule_lock(context.out_dir, context.molecule_id)
        active.remove(context)
        LOGGER.error("molecule_id=%s failed continue_on_error=true", context.molecule_id)


def run_multi(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    rows = read_csv_rows(Path(args.input), smiles_col=args.smiles_col, id_col=args.id_col)
    if args.limit_molecules is not None:
        rows = rows[: args.limit_molecules]
    LOGGER.info(
        "loaded_input_molecules=%d total_workers=%d active_molecules=%d max_pending_tasks=%d out_dir=%s",
        len(rows),
        args.total_workers,
        args.active_molecules,
        args.max_pending_tasks,
        out_dir,
    )
    compression_level = None if args.compression in {"snappy", "none"} else args.compression_level
    compression = None if args.compression == "none" else args.compression
    row_queue: deque[dict[str, str]] = deque(rows)
    active: list[MoleculeContext] = []
    futures: dict[Future[dict[str, object]], MoleculeContext] = {}
    with Manager() as manager:
        with ProcessPoolExecutor(max_workers=args.total_workers) as executor:
            while row_queue or active or futures:
                while row_queue and len(active) < effective_active_limit(args, active):
                    row = row_queue.popleft()
                    LOGGER.info(
                        "input_progress=%d/%d molecule_id=%s activate",
                        len(rows) - len(row_queue),
                        len(rows),
                        row["molecule_id"],
                    )
                    try:
                        context = activate_molecule(
                            row=row,
                            out_dir=out_dir,
                            args=args,
                            manager=manager,
                            compression=compression,
                            compression_level=compression_level,
                        )
                    except Exception as exc:
                        if not args.continue_on_error:
                            raise
                        molecule_id = str(row["molecule_id"])
                        molecule_output_dir(out_dir, molecule_id).mkdir(parents=True, exist_ok=True)
                        molecule_error_json_path(out_dir, molecule_id).write_text(
                            json.dumps(
                                {
                                    "molecule_id": molecule_id,
                                    "error": str(exc),
                                    "traceback": traceback.format_exc(),
                                    "failed_at": utc_now_iso(),
                                },
                                ensure_ascii=False,
                                indent=2,
                            ),
                            encoding="utf-8",
                        )
                        LOGGER.error("molecule_id=%s activation_failed error=%s", molecule_id, exc)
                        continue
                    if context is not None:
                        active.append(context)
                submit_pending_tasks(executor, active, futures, args.max_pending_tasks)
                if not futures:
                    finish_failed_contexts(active, args)
                    if not row_queue and active:
                        break
                    continue
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    context = futures.pop(future)
                    handle_completed_future(future, context, active, args)
                finish_failed_contexts(active, args)
                failed = [context for context in active if context.failed]
                if failed and not args.continue_on_error:
                    for pending in futures:
                        pending.cancel()
                    for context in list(active):
                        if context.failed:
                            write_molecule_error(context, context.error_message)
                        release_molecule_lock(context.out_dir, context.molecule_id)
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise RuntimeError(f"molecule_id={failed[0].molecule_id} failed: {failed[0].error_message}")
    return 0


def summarize_success(base_dir: Path) -> int:
    writer = csv.DictWriter(sys.stdout, fieldnames=SUMMARY_FIELDS, lineterminator="\n")
    writer.writeheader()
    for success_path in sorted(base_dir.glob("molecule_id=*/_SUCCESS")):
        payload = json.loads(success_path.read_text(encoding="utf-8"))
        writer.writerow({field: payload.get(field, "") for field in SUMMARY_FIELDS})
    return 0


def summarize_log(log_path: Path) -> int:
    import re

    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}).*molecule_id=([^\s]+)")
    stats: dict[str, dict[str, Any]] = {}
    with log_path.open("r", encoding="utf-8", errors="ignore") as file:
        for line in file:
            match = pattern.search(line)
            if not match:
                continue
            ts = datetime.strptime(match.group(1) + "." + match.group(2), "%Y-%m-%d %H:%M:%S.%f")
            molecule_id = match.group(3)
            item = stats.setdefault(molecule_id, {"start": ts, "end": ts, "count": 0})
            item["end"] = ts
            item["count"] += 1
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["molecule_id", "wall_hours", "start", "end", "matched_lines"])
    for molecule_id, item in sorted(stats.items()):
        hours = (item["end"] - item["start"]).total_seconds() / 3600.0
        writer.writerow([molecule_id, f"{hours:.6f}", item["start"], item["end"], item["count"]])
    return 0


def collect_canonical_smiles_for_core(
    smiles: str,
    molecule_id: str,
    core: str,
    cpp_batch_size: int,
    requested_shards: int = 8,
) -> set[str]:
    mol = parse_smiles(smiles)
    unit_count = build_compressed_units(mol).unit_count
    shard_plan = build_shard_plan(molecule_id, unit_count, requested_shards)
    root_bucket_count = int(shard_plan["root_bucket_count"])
    stats = RunStats(molecule_id=f"{molecule_id}.{core}")
    canonical_smiles: set[str] = set()

    if core == "cpp" and (not HAS_FAST_CORE or enumerate_compressed_fragment_records_fast is None):
        raise RuntimeError("fast_fragment_core is not built/importable; run build_fast_fragment_core.sh first.")

    for root_unit_index in range(unit_count):
        for root_bucket_index in range(root_bucket_count):
            shard_index = root_unit_index * root_bucket_count + root_bucket_index
            dedupe = DedupeStore("none")
            if core == "cpp":
                assert enumerate_compressed_fragment_records_fast is not None
                record_iter = enumerate_compressed_fragment_records_fast(
                    mol=mol,
                    molecule_id=molecule_id,
                    min_atoms=3,
                    max_atoms=None,
                    key_mode="state",
                    dedupe=dedupe,
                    debug_fields=False,
                    no_smiles=False,
                    limit_states=None,
                    limit_fragments=None,
                    stats=stats,
                    shard_index=shard_index,
                    shard_count=int(shard_plan["shard_count"]),
                    root_unit_index=root_unit_index,
                    root_bucket_index=root_bucket_index,
                    root_bucket_count=root_bucket_count,
                    cpp_batch_size=cpp_batch_size,
                )
            else:
                record_iter = enumerate_compressed_fragment_records(
                    mol=mol,
                    molecule_id=molecule_id,
                    min_atoms=3,
                    max_atoms=None,
                    key_mode="state",
                    dedupe=dedupe,
                    debug_fields=False,
                    no_smiles=False,
                    limit_states=None,
                    limit_fragments=None,
                    stats=stats,
                    shard_index=shard_index,
                    shard_count=int(shard_plan["shard_count"]),
                    root_unit_index=root_unit_index,
                    root_bucket_index=root_bucket_index,
                    root_bucket_count=root_bucket_count,
                )
            for record in record_iter:
                canonical_smiles.add(str(record["canonical_smiles"]))
            dedupe.close()
    return canonical_smiles


def validate_fast_core(cpp_batch_size: int) -> int:
    cases = {
        "benzene": "c1ccccc1",
        "naphthalene": "c1ccc2ccccc2c1",
        "cyclohexane": "C1CCCCC1",
        "phenethylamine": "NCCc1ccccc1",
    }
    for molecule_id, smiles in cases.items():
        python_set = collect_canonical_smiles_for_core(
            smiles=smiles,
            molecule_id=molecule_id,
            core="python",
            cpp_batch_size=cpp_batch_size,
        )
        cpp_set = collect_canonical_smiles_for_core(
            smiles=smiles,
            molecule_id=molecule_id,
            core="cpp",
            cpp_batch_size=cpp_batch_size,
        )
        only_python = sorted(python_set - cpp_set)
        only_cpp = sorted(cpp_set - python_set)
        if only_python or only_cpp:
            raise RuntimeError(
                f"fast-core validation failed for {molecule_id}: "
                f"python_only={only_python[:10]} cpp_only={only_cpp[:10]}"
            )
        LOGGER.info(
            "validate_fast_core molecule_id=%s canonical_smiles_count=%d",
            molecule_id,
            len(python_set),
        )
    LOGGER.info("validate_fast_core passed cases=%d", len(cases))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="split_smile v5 C++/Python global multi-molecule shard scheduler."
    )
    parser.add_argument("--input", help="CSV/TSV task list.")
    parser.add_argument("--smiles-col", default="smiles")
    parser.add_argument("--id-col", default="molecule_id")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--total-workers", type=int, default=14)
    parser.add_argument("--active-molecules", type=int, default=2)
    parser.add_argument("--tail-active-molecules", type=int, default=None)
    parser.add_argument("--tail-trigger-shards-factor", type=float, default=2.0)
    parser.add_argument("--tail-molecule-shards", type=int, default=32)
    parser.add_argument("--tail-min-tail-molecules", type=int, default=1)
    parser.add_argument("--tail-min-nontail-molecules", type=int, default=0)
    parser.add_argument("--tail-rss-soft-max-gb", type=float, default=0.0)
    parser.add_argument("--tail-mem-available-min-gb", type=float, default=0.0)
    parser.add_argument("--tail-log-interval-seconds", type=int, default=60)
    parser.add_argument("--shards", type=int, default=2048)
    parser.add_argument("--max-pending-tasks", type=int, default=64)
    parser.add_argument("--min-atoms", type=int, default=3)
    parser.add_argument("--max-atoms", type=parse_optional_positive_int, default=None)
    parser.add_argument("--batch-size", type=int, default=100000)
    parser.add_argument("--compression", choices=["zstd", "snappy", "none"], default="zstd")
    parser.add_argument("--compression-level", type=int, default=1)
    parser.add_argument("--dedupe", choices=["none", "memory", "sqlite"], default="none")
    parser.add_argument("--key-mode", choices=["state", "hash"], default="state")
    smiles_group = parser.add_mutually_exclusive_group()
    smiles_group.add_argument("--with-smiles", dest="no_smiles", action="store_false", default=False)
    smiles_group.add_argument("--no-smiles", dest="no_smiles", action="store_true")
    parser.add_argument("--fast-core", choices=["auto", "cpp", "python"], default="auto")
    parser.add_argument("--cpp-batch-size", type=int, default=8192)
    parser.add_argument("--debug-fields", action="store_true")
    parser.add_argument("--checksum", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--stale-lock-minutes", type=float, default=1440.0)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--validate-small", action="store_true")
    parser.add_argument("--validate-fast-core", action="store_true")
    parser.add_argument("--summarize-success", type=Path)
    parser.add_argument("--summarize-log", type=Path)
    parser.add_argument("--limit-molecules", type=int, default=None)
    parser.add_argument("--limit-states", type=int, default=None)
    parser.add_argument("--limit-fragments", type=int, default=None)
    return parser


def main() -> None:
    configure_logging()
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.validate_small:
        raise SystemExit(validate_small())
    if args.validate_fast_core:
        raise SystemExit(validate_fast_core(args.cpp_batch_size))
    if args.summarize_success:
        raise SystemExit(summarize_success(args.summarize_success))
    if args.summarize_log:
        raise SystemExit(summarize_log(args.summarize_log))
    if not args.input:
        parser.error(
            "--input is required unless using --validate-small/--validate-fast-core/"
            "--summarize-success/--summarize-log."
        )
    if args.total_workers < 1:
        parser.error("--total-workers must be >= 1.")
    if args.active_molecules < 1:
        parser.error("--active-molecules must be >= 1.")
    if args.tail_active_molecules is None:
        args.tail_active_molecules = args.active_molecules + max(1, args.total_workers // 16)
    if args.tail_active_molecules < 1:
        parser.error("--tail-active-molecules must be >= 1.")
    if args.tail_trigger_shards_factor <= 0:
        parser.error("--tail-trigger-shards-factor must be > 0.")
    if args.tail_molecule_shards < 0:
        parser.error("--tail-molecule-shards must be >= 0.")
    if args.tail_min_tail_molecules < 0:
        parser.error("--tail-min-tail-molecules must be >= 0.")
    if args.tail_min_nontail_molecules < 0:
        parser.error("--tail-min-nontail-molecules must be >= 0.")
    if args.tail_rss_soft_max_gb < 0:
        parser.error("--tail-rss-soft-max-gb must be >= 0.")
    if args.tail_mem_available_min_gb < 0:
        parser.error("--tail-mem-available-min-gb must be >= 0.")
    if args.tail_log_interval_seconds < 0:
        parser.error("--tail-log-interval-seconds must be >= 0.")
    if args.shards < 1:
        parser.error("--shards must be >= 1.")
    if args.max_pending_tasks < 1:
        parser.error("--max-pending-tasks must be >= 1.")
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1.")
    if args.cpp_batch_size < 1:
        parser.error("--cpp-batch-size must be >= 1.")
    if args.no_smiles and args.key_mode == "hash":
        parser.error("--no-smiles cannot be combined with --key-mode hash.")
    if args.no_smiles and args.dedupe in {"memory", "sqlite"}:
        parser.error("--no-smiles with dedupe memory/sqlite would dedupe state keys; use dedupe=none.")
    raise SystemExit(run_multi(args))


if __name__ == "__main__":
    main()
