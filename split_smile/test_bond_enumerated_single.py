from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

from rdkit import Chem


DEFAULT_SMILES = (
    "COC(=O)OCOc1c2n(ccc1=O)N([C@@H]1c3ccccc3SCc3c1ccc(F)c3F)"
    "[C@@H]1COCCN1C2=O"
)
DEFAULT_OUTPUT_CSV = "test_bond_enumerated_single_fragments.csv"


def ensure_import_paths() -> None:
    project_root = Path(__file__).resolve().parent
    bond_dir = project_root / "bond_enumerated_big_121"
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    if str(bond_dir) not in sys.path:
        sys.path.insert(0, str(bond_dir))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run bond_enumerated_non_induced_fragments on a single SMILES and write fragments to CSV."
    )
    parser.add_argument(
        "--smiles",
        default=DEFAULT_SMILES,
        help="Single SMILES to enumerate.",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / DEFAULT_OUTPUT_CSV),
        help="Output CSV path.",
    )
    return parser.parse_args()


def is_all_carbon_fragment(fragment_smiles: str) -> bool:
    mol = Chem.MolFromSmiles(fragment_smiles)
    if mol is None:
        return False
    heavy_atoms = [atom for atom in mol.GetAtoms() if atom.GetAtomicNum() > 1]
    if not heavy_atoms:
        return False
    return all(atom.GetAtomicNum() == 6 for atom in heavy_atoms)


def main() -> None:
    ensure_import_paths()
    args = parse_args()

    from bond_enumerated_non_induced_fragments import (
        DedupeStore,
        RunStats,
        enumerate_compressed_fragment_records,
        parse_smiles,
    )

    smiles = str(args.smiles).strip()
    output_path = Path(str(args.out)).expanduser().resolve()

    mol = parse_smiles(smiles)
    stats = RunStats(molecule_id="test_single")
    stats.heavy_atom_count = sum(1 for atom in mol.GetAtoms() if atom.GetAtomicNum() > 1)

    total_start = time.perf_counter()
    records = list(
        enumerate_compressed_fragment_records(
            mol=mol,
            molecule_id="test_single",
            min_atoms=3,
            max_atoms=None,
            key_mode="state",
            dedupe=DedupeStore(mode="none"),
            debug_fields=False,
            no_smiles=False,
            limit_states=None,
            limit_fragments=None,
            stats=stats,
        )
    )
    enumerate_elapsed = time.perf_counter() - total_start

    filtered_records = [
        record
        for record in records
        if not is_all_carbon_fragment(str(record["canonical_smiles"]))
    ]

    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["index", "fragment_smiles", "fragment_key"],
        )
        writer.writeheader()
        for index, record in enumerate(filtered_records, start=1):
            writer.writerow(
                {
                    "index": index,
                    "fragment_smiles": record["canonical_smiles"],
                    "fragment_key": record["fragment_key"],
                }
            )

    total_elapsed = time.perf_counter() - total_start

    print("Input SMILES:")
    print(smiles)
    print()
    print(f"CSV saved to: {output_path}")
    print()
    print("Stats:")
    print(
        json.dumps(
            {
                "heavy_atom_count": stats.heavy_atom_count,
                "aromatic_system_count": stats.aromatic_system_count,
                "unit_count": stats.unit_count,
                "ordinary_bond_unit_count": stats.ordinary_bond_unit_count,
                "visited_state_count": stats.visited_state_count,
                "emitted_fragment_count": stats.emitted_fragment_count,
                "filtered_out_all_carbon_count": len(records) - len(filtered_records),
                "written_fragment_count": len(filtered_records),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print()
    print(f"Enumerate seconds: {enumerate_elapsed:.6f}")
    print(f"Total seconds: {total_elapsed:.6f}")


if __name__ == "__main__":
    main()
