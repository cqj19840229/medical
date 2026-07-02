from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path


THREAD_ENV_VARS = {
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "ARROW_NUM_THREADS": "1",
}


def configure_thread_env() -> None:
    for name, value in THREAD_ENV_VARS.items():
        os.environ[name] = value


configure_thread_env()


def _optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def default_base_dir() -> Path:
    if platform.system().lower() == "windows":
        return Path(__file__).resolve().parents[1] / "drug_fragment"
    return Path("/mnt/datadisk/drug_fragment")


@dataclass(frozen=True)
class PipelineConfig:
    build_id: str
    base_dir: Path
    input_path: Path
    max_workers: int
    chunk_rows: int
    min_atoms: int
    max_atoms: int | None
    limit: int | None
    parquet_compression: str
    parquet_compression_level: int | None
    debug_fields: bool
    trust_fragment_canonical: bool
    gc_collect_every_n_chunks: int
    memory_soft_limit_gb: int | None
    adaptive_workers: bool
    min_workers: int
    memory_low_limit_gb: int | None
    memory_high_limit_gb: int | None
    enable_checkpoint: bool
    checkpoint_after_chunks: int
    checkpoint_every_chunks: int

    @property
    def build_dir(self) -> Path:
        return self.base_dir / self.build_id


def load_config() -> PipelineConfig:
    configure_thread_env()
    return PipelineConfig(
        build_id=os.getenv("DRUG_FRAGMENT_BUILD_ID", "build_001"),
        base_dir=Path(os.getenv("DRUG_FRAGMENT_BASE_DIR", str(default_base_dir()))),
        input_path=Path(os.getenv("DRUG_FRAGMENT_INPUT", "molecules.csv")),
        max_workers=int(os.getenv("DRUG_FRAGMENT_MAX_WORKERS", "16")),
        chunk_rows=int(os.getenv("DRUG_FRAGMENT_CHUNK_ROWS", "200000")),
        min_atoms=int(os.getenv("DRUG_FRAGMENT_MIN_ATOMS", "3")),
        max_atoms=_optional_int("DRUG_FRAGMENT_MAX_ATOMS"),
        limit=_optional_int("DRUG_FRAGMENT_LIMIT"),
        parquet_compression=os.getenv("DRUG_FRAGMENT_PARQUET_COMPRESSION", "zstd"),
        parquet_compression_level=_optional_int("DRUG_FRAGMENT_PARQUET_COMPRESSION_LEVEL") or 1,
        debug_fields=_bool_env("DRUG_FRAGMENT_DEBUG_FIELDS", False),
        trust_fragment_canonical=_bool_env("DRUG_FRAGMENT_TRUST_FRAGMENT_CANONICAL", False),
        gc_collect_every_n_chunks=int(os.getenv("DRUG_FRAGMENT_GC_COLLECT_EVERY_N_CHUNKS", "10")),
        memory_soft_limit_gb=_optional_int("DRUG_FRAGMENT_MEMORY_SOFT_LIMIT_GB"),
        adaptive_workers=_bool_env("DRUG_FRAGMENT_ADAPTIVE_WORKERS", False),
        min_workers=int(os.getenv("DRUG_FRAGMENT_MIN_WORKERS", "1")),
        memory_low_limit_gb=_optional_int("DRUG_FRAGMENT_MEMORY_LOW_LIMIT_GB"),
        memory_high_limit_gb=_optional_int("DRUG_FRAGMENT_MEMORY_HIGH_LIMIT_GB"),
        enable_checkpoint=_bool_env("DRUG_FRAGMENT_ENABLE_CHECKPOINT", False),
        checkpoint_after_chunks=int(os.getenv("DRUG_FRAGMENT_CHECKPOINT_AFTER_CHUNKS", "1")),
        checkpoint_every_chunks=int(os.getenv("DRUG_FRAGMENT_CHECKPOINT_EVERY_CHUNKS", "5")),
    )
