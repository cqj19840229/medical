from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Iterator

from .config import configure_thread_env, load_config


configure_thread_env()

from .molecule_worker import process_molecule  # noqa: E402


logger = logging.getLogger(__name__)
BYTES_PER_GIB = 1024**3
MAX_BROKEN_POOL_RETRIES = 3
MoleculeJob = tuple[int, str]


def iter_molecules(input_path: Path) -> Iterator[tuple[int, str]]:
    with input_path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if (
            reader.fieldnames is None
            or "molecule_id" not in reader.fieldnames
            or "smiles" not in reader.fieldnames
        ):
            raise ValueError("Input CSV must contain molecule_id and smiles columns.")
        for row in reader:
            molecule_id = int(str(row["molecule_id"]).strip())
            smiles = str(row["smiles"]).strip()
            if not smiles:
                raise ValueError(f"Empty smiles for molecule_id={molecule_id}")
            yield molecule_id, smiles


def _memory_used_gb() -> float | None:
    meminfo_path = Path("/proc/meminfo")
    if not meminfo_path.exists():
        return None

    values: dict[str, int] = {}
    with meminfo_path.open("r", encoding="utf-8") as file:
        for line in file:
            parts = line.split()
            if len(parts) >= 2:
                values[parts[0].rstrip(":")] = int(parts[1]) * 1024

    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        return None
    return (total - available) / BYTES_PER_GIB


def _memory_throttled(memory_soft_limit_gb: int | None) -> bool:
    if memory_soft_limit_gb is None:
        return False

    used_gb = _memory_used_gb()
    if used_gb is None:
        return False

    if used_gb >= memory_soft_limit_gb:
        logger.warning(
            "memory throttle active used_gb=%.2f soft_limit_gb=%s",
            used_gb,
            memory_soft_limit_gb,
        )
        return True
    return False


def _adjust_target_workers(config: Any, current_target: int) -> int:
    if not config.adaptive_workers:
        return config.max_workers

    used_gb = _memory_used_gb()
    if used_gb is None:
        return current_target

    min_workers = max(1, min(config.min_workers, config.max_workers))
    next_target = current_target

    if (
        config.memory_high_limit_gb is not None
        and used_gb >= config.memory_high_limit_gb
    ):
        next_target = max(min_workers, current_target - 1)
    elif (
        config.memory_low_limit_gb is not None
        and used_gb <= config.memory_low_limit_gb
    ):
        next_target = min(config.max_workers, current_target + 1)

    if next_target != current_target:
        logger.warning(
            "adaptive workers target changed from %s to %s used_gb=%.2f low_gb=%s high_gb=%s",
            current_target,
            next_target,
            used_gb,
            config.memory_low_limit_gb,
            config.memory_high_limit_gb,
        )
    return next_target


def _log_and_accumulate(
    result: dict[str, Any],
    summary: dict[str, Any],
    failed: list[dict[str, Any]],
) -> None:
    status = str(result.get("status"))
    rows = int(result.get("fragment_rows", 0))
    elapsed = float(result.get("elapsed_seconds", 0.0))
    fps = float(result.get("fragments_per_second", 0.0))
    molecule_id = result.get("molecule_id")

    if status == "success":
        summary["success_count"] += 1
        summary["total_fragment_rows"] += rows
    elif status == "skipped":
        summary["skipped_count"] += 1
        summary["total_fragment_rows"] += rows
    else:
        summary["failed_count"] += 1
        failed.append(result)

    logger.info(
        "molecule_id=%s status=%s fragment_rows=%s elapsed_seconds=%.3f fragments_per_second=%.2f",
        molecule_id,
        status,
        rows,
        elapsed,
        fps,
    )


def _failed_result(
    molecule_id: int,
    error_message: str,
) -> dict[str, Any]:
    return {
        "molecule_id": molecule_id,
        "status": "failed",
        "fragment_rows": 0,
        "elapsed_seconds": 0.0,
        "fragments_per_second": 0.0,
        "error_message": error_message,
    }


def _drain_completed(
    futures: dict[Future[dict[str, Any]], MoleculeJob],
    running_ids: set[int],
    summary: dict[str, Any],
    failed: list[dict[str, Any]],
    return_when: str,
) -> None:
    if not futures:
        return
    done, _pending = wait(futures.keys(), return_when=return_when)
    for future in done:
        molecule_id, _smiles = futures.pop(future)
        running_ids.discard(molecule_id)
        try:
            result = future.result()
        except BrokenProcessPool:
            futures[future] = (molecule_id, _smiles)
            running_ids.add(molecule_id)
            raise
        except Exception as exc:
            result = _failed_result(molecule_id, str(exc))
        _log_and_accumulate(result, summary, failed)


def _requeue_after_broken_pool(
    jobs: list[MoleculeJob],
    retry_jobs: deque[MoleculeJob],
    broken_pool_retries: dict[int, int],
    summary: dict[str, Any],
    failed: list[dict[str, Any]],
) -> None:
    for molecule_id, smiles in jobs:
        retry_count = broken_pool_retries.get(molecule_id, 0) + 1
        broken_pool_retries[molecule_id] = retry_count
        if retry_count > MAX_BROKEN_POOL_RETRIES:
            _log_and_accumulate(
                _failed_result(
                    molecule_id,
                    (
                        "Process pool broke while this molecule was running; "
                        f"not retrying after {MAX_BROKEN_POOL_RETRIES} retries."
                    ),
                ),
                summary,
                failed,
            )
        else:
            retry_jobs.append((molecule_id, smiles))


def _restart_executor_after_broken_pool(
    executor: ProcessPoolExecutor,
    futures: dict[Future[dict[str, Any]], MoleculeJob],
    running_ids: set[int],
    retry_jobs: deque[MoleculeJob],
    broken_pool_retries: dict[int, int],
    summary: dict[str, Any],
    failed: list[dict[str, Any]],
    config: Any,
    target_workers: int,
    reason: BaseException,
) -> tuple[ProcessPoolExecutor, int]:
    inflight_jobs = list(futures.values())
    futures.clear()
    running_ids.clear()
    _requeue_after_broken_pool(
        inflight_jobs,
        retry_jobs,
        broken_pool_retries,
        summary,
        failed,
    )
    executor.shutdown(wait=False, cancel_futures=True)

    min_workers = max(1, min(config.min_workers, config.max_workers))
    next_target_workers = max(min_workers, target_workers - 1)
    used_gb = _memory_used_gb()
    logger.warning(
        "process pool broken; requeued_inflight=%s target_workers=%s next_target_workers=%s used_gb=%s reason=%s",
        len(inflight_jobs),
        target_workers,
        next_target_workers,
        f"{used_gb:.2f}" if used_gb is not None else "unknown",
        reason,
    )
    return ProcessPoolExecutor(max_workers=config.max_workers), next_target_workers


def main() -> None:
    parser = argparse.ArgumentParser(description="Build SMILES fragment parquet dataset.")
    parser.add_argument("--input", type=Path, default=None, help="Input CSV with molecule_id,smiles.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    input_path = args.input or config.input_path
    build_dir = config.build_dir
    build_dir.mkdir(parents=True, exist_ok=True)

    start = time.perf_counter()
    summary: dict[str, Any] = {
        "total_molecules": 0,
        "success_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "total_fragment_rows": 0,
        "total_elapsed_seconds": 0.0,
    }
    failed: list[dict[str, Any]] = []
    max_pending = config.max_workers
    target_workers = config.max_workers
    retry_jobs: deque[MoleculeJob] = deque()
    broken_pool_retries: dict[int, int] = {}
    molecule_iter = iter_molecules(input_path)
    input_exhausted = False

    executor = ProcessPoolExecutor(max_workers=config.max_workers)
    futures: dict[Future[dict[str, Any]], MoleculeJob] = {}
    running_ids: set[int] = set()
    try:
        while True:
            if retry_jobs:
                molecule_id, smiles = retry_jobs.popleft()
            elif not input_exhausted:
                try:
                    molecule_id, smiles = next(molecule_iter)
                    summary["total_molecules"] += 1
                except StopIteration:
                    input_exhausted = True
                    continue
            elif futures:
                try:
                    _drain_completed(
                        futures,
                        running_ids,
                        summary,
                        failed,
                        FIRST_COMPLETED,
                    )
                    target_workers = _adjust_target_workers(config, target_workers)
                except BrokenProcessPool as exc:
                    executor, target_workers = _restart_executor_after_broken_pool(
                        executor,
                        futures,
                        running_ids,
                        retry_jobs,
                        broken_pool_retries,
                        summary,
                        failed,
                        config,
                        target_workers,
                        exc,
                    )
                    time.sleep(5)
                continue
            else:
                break

            target_workers = _adjust_target_workers(config, target_workers)
            current_job = (molecule_id, smiles)
            requeued_current = False
            while (
                len(futures) >= max_pending
                or len(futures) >= target_workers
                or molecule_id in running_ids
                or _memory_throttled(config.memory_soft_limit_gb)
            ):
                if futures:
                    try:
                        _drain_completed(
                            futures,
                            running_ids,
                            summary,
                            failed,
                            FIRST_COMPLETED,
                        )
                        target_workers = _adjust_target_workers(config, target_workers)
                    except BrokenProcessPool as exc:
                        retry_jobs.appendleft(current_job)
                        requeued_current = True
                        executor, target_workers = _restart_executor_after_broken_pool(
                            executor,
                            futures,
                            running_ids,
                            retry_jobs,
                            broken_pool_retries,
                            summary,
                            failed,
                            config,
                            target_workers,
                            exc,
                        )
                        time.sleep(5)
                        break
                else:
                    time.sleep(5)
                    target_workers = _adjust_target_workers(config, target_workers)

            if requeued_current:
                continue

            try:
                future = executor.submit(
                    process_molecule,
                    molecule_id,
                    smiles,
                    build_dir,
                    config.min_atoms,
                    config.max_atoms,
                    config.limit,
                    config.chunk_rows,
                    config.debug_fields,
                )
            except BrokenProcessPool as exc:
                retry_jobs.appendleft(current_job)
                executor, target_workers = _restart_executor_after_broken_pool(
                    executor,
                    futures,
                    running_ids,
                    retry_jobs,
                    broken_pool_retries,
                    summary,
                    failed,
                    config,
                    target_workers,
                    exc,
                )
                time.sleep(5)
                continue

            futures[future] = current_job
            running_ids.add(molecule_id)
    finally:
        executor.shutdown(wait=True, cancel_futures=False)

    summary["total_elapsed_seconds"] = time.perf_counter() - start
    (build_dir / "failed_molecules.json").write_text(
        json.dumps(failed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (build_dir / "build_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("build_summary=%s", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
