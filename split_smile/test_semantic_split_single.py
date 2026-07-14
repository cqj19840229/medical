from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path


TARGET_SMILES = (
    "COC(=O)OCOc1c2n(ccc1=O)N([C@@H]1c3ccccc3SCc3c1ccc(F)c3F)"
    "[C@@H]1COCCN1C2=O"
)


def ensure_import_paths() -> None:
    project_root = Path(__file__).resolve().parent
    semantic_dir = project_root / "semantic"
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    if str(semantic_dir) not in sys.path:
        sys.path.insert(0, str(semantic_dir))


def ensure_mysql_stub() -> None:
    try:
        import mysql.connector  # type: ignore  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    mysql = types.ModuleType("mysql")
    connector = types.ModuleType("mysql.connector")
    connection_mod = types.ModuleType("mysql.connector.connection")
    errors_mod = types.ModuleType("mysql.connector.errors")

    class MySQLConnection:  # noqa: D401
        """Stub for optional mysql dependency during local split tests."""

    class Error(Exception):
        pass

    connector.connection = connection_mod
    connector.errors = errors_mod
    mysql.connector = connector
    connection_mod.MySQLConnection = MySQLConnection
    errors_mod.Error = Error

    sys.modules["mysql"] = mysql
    sys.modules["mysql.connector"] = connector
    sys.modules["mysql.connector.connection"] = connection_mod
    sys.modules["mysql.connector.errors"] = errors_mod


def main() -> None:
    ensure_import_paths()
    ensure_mysql_stub()

    from semantic.semantic_smiles_splitter import SplitConfig, split_smiles_semantically

    start = time.perf_counter()
    result = split_smiles_semantically(
        TARGET_SMILES,
        indications=[],
        drug_feature_fragments_by_indication={},
        config=SplitConfig(),
    )
    elapsed = time.perf_counter() - start

    print("Input SMILES:")
    print(TARGET_SMILES)
    print()
    print("Canonical SMILES:")
    print(result["canonical_smiles"])
    print()
    print("Fragments:")
    for index, fragment in enumerate(result["fragments"], start=1):
        print(
            json.dumps(
                {
                    "index": index,
                    "fragment_smiles": fragment["fragment_smiles"],
                    "fragment_type": fragment["fragment_type"],
                    "semantic_type": fragment["semantic_type"],
                    "heavy_atoms": fragment["heavy_atoms"],
                    "stop_decompose": fragment["stop_decompose"],
                    "stop_reason": fragment["stop_reason"],
                },
                ensure_ascii=False,
            )
        )
    print()
    print("Counts:")
    print(json.dumps(result["counts"], ensure_ascii=False, indent=2))
    print()
    print(f"Elapsed seconds: {elapsed:.6f}")


if __name__ == "__main__":
    main()
