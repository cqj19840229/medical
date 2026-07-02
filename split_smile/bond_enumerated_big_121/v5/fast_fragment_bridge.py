from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Iterator

from rdkit import Chem

SCRIPT_DIR = Path(__file__).resolve().parent
PARENT_DIR = SCRIPT_DIR.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

from bond_enumerated_non_induced_fragments import (
    DedupeStore,
    RunStats,
    build_compressed_units,
    heavy_atom_indices,
    non_chon_hetero_atom_symbols,
)

try:
    from fast_fragment_core import FastEnumerator
    HAS_FAST_CORE = True
except Exception:  # pragma: no cover - depends on local C++ build.
    FastEnumerator = None  # type: ignore[assignment]
    HAS_FAST_CORE = False


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_fragment_indices(mol: Chem.Mol, atom_indices: list[int], bond_indices: list[int]) -> tuple[list[int], list[int]]:
    atom_set = {int(index) for index in atom_indices}
    bond_set: set[int] = set()
    for bond_index in bond_indices:
        bond = mol.GetBondWithIdx(int(bond_index))
        atom_set.add(bond.GetBeginAtomIdx())
        atom_set.add(bond.GetEndAtomIdx())
        bond_set.add(bond.GetIdx())
    return sorted(atom_set), sorted(bond_set)


def clone_with_fragment_stereo_fallback(mol: Chem.Mol, atom_indices: list[int], bond_indices: list[int]) -> Chem.Mol:
    atom_set = set(atom_indices)
    bond_set = set(bond_indices)
    rw_mol = Chem.RWMol(mol)
    for bond in rw_mol.GetBonds():
        touches_fragment = bond.GetBeginAtomIdx() in atom_set or bond.GetEndAtomIdx() in atom_set
        if bond.GetIdx() in bond_set or touches_fragment:
            bond.SetStereo(Chem.BondStereo.STEREONONE)
            bond.SetBondDir(Chem.BondDir.NONE)
    return rw_mol.GetMol()


def fragment_smiles_from_indices(mol: Chem.Mol, atom_indices: list[int], bond_indices: list[int]) -> str:
    atom_indices, bond_indices = normalize_fragment_indices(mol, atom_indices, bond_indices)
    try:
        return Chem.MolFragmentToSmiles(
            mol,
            atomsToUse=atom_indices,
            bondsToUse=bond_indices,
            canonical=True,
            isomericSmiles=True,
        )
    except Exception as exc:
        if "neither end atom traversed" not in str(exc):
            raise
        fallback_mol = clone_with_fragment_stereo_fallback(mol, atom_indices, bond_indices)
        try:
            return Chem.MolFragmentToSmiles(
                fallback_mol,
                atomsToUse=atom_indices,
                bondsToUse=bond_indices,
                canonical=True,
                isomericSmiles=True,
            )
        except Exception:
            return Chem.MolFragmentToSmiles(
                fallback_mol,
                atomsToUse=atom_indices,
                bondsToUse=bond_indices,
                canonical=True,
                isomericSmiles=False,
            )


def enumerate_compressed_fragment_records_fast(
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
    cpp_batch_size: int = 8192,
) -> Iterator[dict[str, object]]:
    """C++ state BFS + Python RDKit canonical SMILES.

    This keeps the externally visible records identical to the Python enumerator for the
    default production mode, but removes the Python set/deque/bit iteration hot path.
    For debug output we still support the extra columns, although production should keep
    debug off to avoid JSON overhead.
    """
    if not HAS_FAST_CORE or FastEnumerator is None:
        raise RuntimeError("fast_fragment_core is not built/importable")

    units = build_compressed_units(mol)
    stats.aromatic_system_count = units.aromatic_unit_count
    stats.unit_count = units.unit_count
    stats.ordinary_bond_unit_count = units.ordinary_bond_unit_count
    effective_max_atoms = max_atoms or len(heavy_atom_indices(mol))

    enum = FastEnumerator(
        atom_masks=list(units.atom_masks),
        bond_masks=list(units.bond_masks),
        adjacency_masks=list(units.adjacency_masks),
        closure_masks=list(units.closure_masks),
        min_atoms=int(min_atoms),
        max_atoms=int(effective_max_atoms),
        shard_index=int(shard_index),
        shard_count=int(shard_count),
        root_unit_index=None if root_unit_index is None else int(root_unit_index),
        root_bucket_index=int(root_bucket_index),
        root_bucket_count=int(root_bucket_count),
        limit_states=None if limit_states is None else int(limit_states),
        limit_fragments=None if limit_fragments is None else int(limit_fragments),
    )

    while True:
        batch = enum.next_batch(max_records=max(1, int(cpp_batch_size)))
        stats.visited_state_count = int(batch["visited_state_count"])
        # C++ emitted count is pre-Python-dedupe. In production dedupe=none, so this is exact.
        candidate_emitted = int(batch["emitted_fragment_count"])

        state_keys = batch["state_keys"]
        atom_indices_batch = batch["atom_indices"]
        bond_indices_batch = batch["bond_indices"]
        atom_counts = batch["atom_counts"]
        bond_counts = batch["bond_counts"]

        for state_key, atom_indices, bond_indices, atom_count, bond_count in zip(
            state_keys,
            atom_indices_batch,
            bond_indices_batch,
            atom_counts,
            bond_counts,
        ):
            atom_indices_list, bond_indices_list = normalize_fragment_indices(
                mol,
                list(atom_indices),
                list(bond_indices),
            )
            atom_count = len(atom_indices_list)
            bond_count = len(bond_indices_list)
            if no_smiles:
                canonical_smiles = ""
                dedupe_key = str(state_key)
            else:
                canonical_smiles = fragment_smiles_from_indices(mol, atom_indices_list, bond_indices_list)
                dedupe_key = canonical_smiles

            if key_mode == "state":
                fragment_key = str(state_key)
                hash_source = canonical_smiles or fragment_key
            else:
                fragment_key = sha256_text(canonical_smiles)
                hash_source = canonical_smiles
            fragment_hash = sha256_text(hash_source)

            if not dedupe.add(dedupe_key):
                continue

            stats.emitted_fragment_count += 1
            record: dict[str, object] = {
                "molecule_id": str(molecule_id),
                "fragment_key": fragment_key,
                "fragment_hash256": fragment_hash,
                "canonical_smiles": canonical_smiles,
                "atom_count": int(atom_count),
                "bond_count": int(bond_count),
            }
            if debug_fields:
                # Debug stays available, but should not be used for large production runs.
                protected = [idx for idx in range(units.aromatic_unit_count) if int(state_key, 16) & (1 << idx)]
                record.update(
                    {
                        "fragment_smiles": canonical_smiles,
                        "atom_indices_json": json.dumps(atom_indices_list, ensure_ascii=False),
                        "bond_indices_json": json.dumps(bond_indices_list, ensure_ascii=False),
                        "protected_units_hit_json": json.dumps(protected, ensure_ascii=False),
                        "non_chon_hetero_atoms_json": json.dumps(
                            non_chon_hetero_atom_symbols(mol, atom_indices_list),
                            ensure_ascii=False,
                        ),
                    }
                )
            yield record

        # If dedupe was not none, C++ candidate count can exceed records actually written.
        if dedupe.mode == "none":
            stats.emitted_fragment_count = candidate_emitted

        if bool(batch["done"]):
            break
