from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Iterable

from rdkit import Chem
from rdkit import RDLogger


RDLogger.DisableLog("rdApp.*")

FRAGMENT_TYPE = "bond_enumerated_non_induced_aromatic_protected"
CHON_ATOMIC_NUMBERS = {1, 6, 7, 8}


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


def heavy_bond_indices(mol: Chem.Mol) -> list[int]:
    return [
        bond.GetIdx()
        for bond in mol.GetBonds()
        if bond.GetBeginAtom().GetAtomicNum() > 1 and bond.GetEndAtom().GetAtomicNum() > 1
    ]


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
            stack.extend(sorted(graph[current] - visited))

        component_bonds = {
            bond_idx
            for bond_idx in aromatic_bonds
            if mol.GetBondWithIdx(bond_idx).GetBeginAtomIdx() in component_atoms
            and mol.GetBondWithIdx(bond_idx).GetEndAtomIdx() in component_atoms
        }
        systems.append({"atoms": component_atoms, "bonds": component_bonds})

    return systems


def atoms_from_bonds(mol: Chem.Mol, bond_indices: Iterable[int]) -> set[int]:
    atom_indices: set[int] = set()
    for bond_idx in bond_indices:
        bond = mol.GetBondWithIdx(int(bond_idx))
        atom_indices.add(bond.GetBeginAtomIdx())
        atom_indices.add(bond.GetEndAtomIdx())
    return atom_indices


def close_protected_aromatic_systems(
    selected_atoms: Iterable[int],
    selected_bonds: Iterable[int],
    aromatic_systems: list[dict[str, set[int]]],
) -> tuple[set[int], set[int], list[int]]:
    closed_atoms = set(selected_atoms)
    closed_bonds = set(selected_bonds)
    protected_units_hit: set[int] = set()

    changed = True
    while changed:
        changed = False
        for unit_index, system in enumerate(aromatic_systems):
            system_atoms = system["atoms"]
            system_bonds = system["bonds"]
            if not (closed_atoms & system_atoms or closed_bonds & system_bonds):
                continue
            before_atoms = len(closed_atoms)
            before_bonds = len(closed_bonds)
            closed_atoms.update(system_atoms)
            closed_bonds.update(system_bonds)
            protected_units_hit.add(unit_index)
            if len(closed_atoms) != before_atoms or len(closed_bonds) != before_bonds:
                changed = True

    return closed_atoms, closed_bonds, sorted(protected_units_hit)


def build_bond_adjacency(mol: Chem.Mol, bond_indices: Iterable[int]) -> dict[int, set[int]]:
    bonds_by_atom: dict[int, set[int]] = defaultdict(set)
    heavy_bonds = set(bond_indices)
    for bond_idx in heavy_bonds:
        bond = mol.GetBondWithIdx(bond_idx)
        bonds_by_atom[bond.GetBeginAtomIdx()].add(bond_idx)
        bonds_by_atom[bond.GetEndAtomIdx()].add(bond_idx)

    adjacency: dict[int, set[int]] = {bond_idx: set() for bond_idx in heavy_bonds}
    for incident_bonds in bonds_by_atom.values():
        for bond_idx in incident_bonds:
            adjacency[bond_idx].update(incident_bonds - {bond_idx})
    return adjacency


def fragment_smiles_from_atom_bond_set(
    mol: Chem.Mol,
    atom_indices: Iterable[int],
    bond_indices: Iterable[int],
) -> str:
    return Chem.MolFragmentToSmiles(
        mol,
        atomsToUse=sorted(atom_indices),
        bondsToUse=sorted(bond_indices),
        canonical=True,
        isomericSmiles=True,
    )


def _normalize_state(
    mol: Chem.Mol,
    selected_bonds: Iterable[int],
    aromatic_systems: list[dict[str, set[int]]],
) -> tuple[frozenset[int], frozenset[int], tuple[int, ...]]:
    selected_bond_set = set(selected_bonds)
    selected_atom_set = atoms_from_bonds(mol, selected_bond_set)
    closed_atoms, closed_bonds, protected_units_hit = close_protected_aromatic_systems(
        selected_atom_set,
        selected_bond_set,
        aromatic_systems,
    )
    return frozenset(closed_atoms), frozenset(closed_bonds), tuple(protected_units_hit)


def enumerate_bond_fragments_non_induced(
    mol: Chem.Mol,
    min_atoms: int = 3,
    max_atoms: int | None = None,
    limit: int | None = None,
) -> list[dict[str, object]]:
    if max_atoms is None:
        max_atoms = len(heavy_atom_indices(mol))
    if min_atoms < 1:
        raise ValueError("min_atoms must be >= 1.")
    if max_atoms < min_atoms:
        return []

    aromatic_systems = build_aromatic_systems(mol)
    candidate_bonds = heavy_bond_indices(mol)
    bond_adjacency = build_bond_adjacency(mol, candidate_bonds)
    visited_states: set[frozenset[int]] = set()
    queued_states: set[frozenset[int]] = set()
    queue: deque[frozenset[int]] = deque()
    unique: dict[str, dict[str, object]] = {}

    def enqueue(bond_set: Iterable[int]) -> None:
        closed_atoms, closed_bonds, _protected_units_hit = _normalize_state(
            mol,
            bond_set,
            aromatic_systems,
        )
        if len(closed_atoms) > max_atoms:
            return
        state = frozenset(closed_bonds)
        if state not in visited_states and state not in queued_states:
            queued_states.add(state)
            queue.append(state)

    for seed_bond_idx in sorted(candidate_bonds):
        enqueue({seed_bond_idx})

    while queue:
        state = queue.popleft()
        queued_states.discard(state)
        if state in visited_states:
            continue
        visited_states.add(state)

        selected_atoms, selected_bonds, protected_units_hit = _normalize_state(
            mol,
            state,
            aromatic_systems,
        )
        atom_count = len(selected_atoms)
        if min_atoms <= atom_count <= max_atoms:
            fragment_smiles = fragment_smiles_from_atom_bond_set(
                mol,
                selected_atoms,
                selected_bonds,
            )
            record = {
                "fragment_smiles": fragment_smiles,
                "atom_indices": sorted(selected_atoms),
                "bond_indices": sorted(selected_bonds),
                "_bond_indices": sorted(selected_bonds),
                "atom_indices_json": json.dumps(sorted(selected_atoms), ensure_ascii=False),
                "bond_indices_json": json.dumps(sorted(selected_bonds), ensure_ascii=False),
                "fragment_type": FRAGMENT_TYPE,
                "protected_units_hit": list(protected_units_hit),
                "protected_units_hit_json": json.dumps(list(protected_units_hit), ensure_ascii=False),
                "atom_count": atom_count,
                "bond_count": len(selected_bonds),
                "heavy_atoms": atom_count,
                "non_chon_hetero_atoms": non_chon_hetero_atom_symbols(mol, selected_atoms),
            }
            existing = unique.get(fragment_smiles)
            if existing is None or (
                record["atom_indices"],
                record["bond_indices"],
            ) < (
                existing["atom_indices"],
                existing["bond_indices"],
            ):
                unique[fragment_smiles] = record
                if limit is not None and len(unique) >= limit:
                    break

        frontier: set[int] = set()
        for bond_idx in selected_bonds:
            frontier.update(bond_adjacency.get(bond_idx, set()))
        for next_bond_idx in sorted(frontier - set(selected_bonds)):
            enqueue(set(selected_bonds) | {next_bond_idx})

    fragments = sorted(
        unique.values(),
        key=lambda item: (
            int(item["atom_count"]),
            str(item["fragment_smiles"]),
            str(item["atom_indices"]),
            str(item["bond_indices"]),
        ),
    )
    return fragments[:limit] if limit is not None else fragments


def enumerate_fragments(
    smiles: str,
    limit: int | None = None,
    min_atoms: int = 3,
    max_atoms: int | None = None,
) -> dict[str, object]:
    mol = parse_smiles(smiles)
    canonical_smiles = Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True)
    heavy_atoms = heavy_atom_indices(mol)
    effective_max_atoms = len(heavy_atoms) if max_atoms is None else max_atoms
    fragments = enumerate_bond_fragments_non_induced(
        mol,
        min_atoms=min_atoms,
        max_atoms=effective_max_atoms,
        limit=limit,
    )
    return {
        "input_smiles": smiles,
        "canonical_smiles": canonical_smiles,
        "heavy_atom_count": len(heavy_atoms),
        "min_atoms": min_atoms,
        "max_atoms": effective_max_atoms,
        "fragment_type": FRAGMENT_TYPE,
        "non_chon_hetero_atoms": non_chon_hetero_atom_symbols(mol, heavy_atoms),
        "fragments": fragments,
    }


def write_json(result: dict[str, object], output_path: Path) -> None:
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_csv(result: dict[str, object], output_path: Path) -> None:
    fragments = result["fragments"]
    with output_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "index",
                "fragment_smiles",
                "atom_count",
                "bond_count",
                "fragment_type",
                "protected_units_hit_json",
                "atom_indices_json",
                "bond_indices_json",
            ],
        )
        writer.writeheader()
        for index, fragment in enumerate(fragments, start=1):
            writer.writerow(
                {
                    "index": index,
                    "fragment_smiles": fragment["fragment_smiles"],
                    "atom_count": fragment["atom_count"],
                    "bond_count": fragment["bond_count"],
                    "fragment_type": fragment["fragment_type"],
                    "protected_units_hit_json": fragment["protected_units_hit_json"],
                    "atom_indices_json": fragment["atom_indices_json"],
                    "bond_indices_json": fragment["bond_indices_json"],
                }
            )


def parse_optional_positive_int(value: str) -> int | None:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return None if parsed == 0 else parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enumerate non-induced heavy-atom SMILES fragments by growing bond sets."
    )
    parser.add_argument("smiles", nargs="?", help="Input drug SMILES.")
    parser.add_argument(
        "-o",
        "--output",
        default="bond_enumerated_fragments.csv",
        help="Output path. Use .json for JSON, otherwise CSV is written.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of unique fragments to write after sorting.",
    )
    parser.add_argument("--min-atoms", type=int, default=3, help="Minimum heavy atoms per fragment.")
    parser.add_argument(
        "--max-atoms",
        type=parse_optional_positive_int,
        default=None,
        help="Maximum heavy atoms per fragment. Defaults to the full molecule; use 0 for default.",
    )
    args = parser.parse_args()

    if not args.smiles:
        parser.error("smiles is required.")

    result = enumerate_fragments(
        args.smiles,
        limit=args.limit,
        min_atoms=args.min_atoms,
        max_atoms=args.max_atoms,
    )
    output_path = Path(args.output)
    if output_path.suffix.lower() == ".json":
        write_json(result, output_path)
    else:
        write_csv(result, output_path)

    print(f"canonical_smiles: {result['canonical_smiles']}")
    print(f"heavy_atom_count: {result['heavy_atom_count']}")
    print(f"fragment_atom_range: {result['min_atoms']}..{result['max_atoms']}")
    print(f"fragment_type: {FRAGMENT_TYPE}")
    print(f"unique_fragments: {len(result['fragments'])}")
    print(f"output: {output_path.resolve()}")


if __name__ == "__main__":
    main()
