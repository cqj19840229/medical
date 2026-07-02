from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from typing import Any, Iterable, Iterator

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
    atoms_to_use = sorted(atom_indices)
    bonds_to_use = sorted(bond_indices)
    try:
        return Chem.MolFragmentToSmiles(
            mol,
            atomsToUse=atoms_to_use,
            bondsToUse=bonds_to_use,
            canonical=True,
            isomericSmiles=True,
        )
    except RuntimeError:
        stereo_safe_mol = Chem.Mol(mol)
        for bond in stereo_safe_mol.GetBonds():
            bond.SetStereo(Chem.BondStereo.STEREONONE)
        return Chem.MolFragmentToSmiles(
            stereo_safe_mol,
            atomsToUse=atoms_to_use,
            bondsToUse=bonds_to_use,
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


def _mask_to_bonds(mask: int, bit_to_bond: list[int]) -> list[int]:
    bonds: list[int] = []
    bit_index = 0
    current = mask
    while current:
        if current & 1:
            bonds.append(bit_to_bond[bit_index])
        current >>= 1
        bit_index += 1
    return bonds


def _atom_mask_to_indices(mask: int) -> list[int]:
    atom_indices: list[int] = []
    bit_index = 0
    current = mask
    while current:
        if current & 1:
            atom_indices.append(bit_index)
        current >>= 1
        bit_index += 1
    return atom_indices


def _bonds_to_mask(bond_indices: Iterable[int], bond_to_bit: dict[int, int]) -> int:
    mask = 0
    for bond_idx in bond_indices:
        mask |= 1 << bond_to_bit[bond_idx]
    return mask


def _build_bond_atom_masks(mol: Chem.Mol, bit_to_bond: list[int]) -> list[int]:
    bond_atom_masks: list[int] = []
    for bond_idx in bit_to_bond:
        bond = mol.GetBondWithIdx(bond_idx)
        bond_atom_masks.append(
            (1 << bond.GetBeginAtomIdx()) | (1 << bond.GetEndAtomIdx())
        )
    return bond_atom_masks


def _atom_mask_from_bond_mask(selected_bond_mask: int, bond_atom_masks: list[int]) -> int:
    atom_mask = 0
    current = selected_bond_mask
    while current:
        low_bit = current & -current
        bit_index = low_bit.bit_length() - 1
        atom_mask |= bond_atom_masks[bit_index]
        current ^= low_bit
    return atom_mask


def _build_aromatic_system_masks(
    aromatic_systems: list[dict[str, set[int]]],
    bond_to_bit: dict[int, int],
) -> list[tuple[int, int]]:
    system_masks: list[tuple[int, int]] = []
    for system in aromatic_systems:
        atom_mask = 0
        for atom_idx in system["atoms"]:
            atom_mask |= 1 << atom_idx

        bond_mask = 0
        for bond_idx in system["bonds"]:
            if bond_idx in bond_to_bit:
                bond_mask |= 1 << bond_to_bit[bond_idx]
        system_masks.append((atom_mask, bond_mask))
    return system_masks


def _close_protected_aromatic_system_masks(
    selected_atom_mask: int,
    selected_bond_mask: int,
    aromatic_system_masks: list[tuple[int, int]],
) -> tuple[int, int, tuple[int, ...]]:
    closed_atom_mask = selected_atom_mask
    closed_bond_mask = selected_bond_mask
    protected_units_hit: list[int] = []
    protected_units_seen: set[int] = set()

    changed = True
    while changed:
        changed = False
        for unit_index, (system_atom_mask, system_bond_mask) in enumerate(aromatic_system_masks):
            if not (
                closed_atom_mask & system_atom_mask
                or closed_bond_mask & system_bond_mask
            ):
                continue
            new_atom_mask = closed_atom_mask | system_atom_mask
            new_bond_mask = closed_bond_mask | system_bond_mask
            if unit_index not in protected_units_seen:
                protected_units_seen.add(unit_index)
                protected_units_hit.append(unit_index)
            if new_atom_mask != closed_atom_mask or new_bond_mask != closed_bond_mask:
                closed_atom_mask = new_atom_mask
                closed_bond_mask = new_bond_mask
                changed = True

    return closed_atom_mask, closed_bond_mask, tuple(sorted(protected_units_hit))


def _build_bond_adjacency_masks(
    mol: Chem.Mol,
    bit_to_bond: list[int],
    bond_to_bit: dict[int, int],
) -> list[int]:
    bonds_by_atom: dict[int, int] = defaultdict(int)
    for bit_index, bond_idx in enumerate(bit_to_bond):
        bond = mol.GetBondWithIdx(bond_idx)
        bond_bit = 1 << bit_index
        bonds_by_atom[bond.GetBeginAtomIdx()] |= bond_bit
        bonds_by_atom[bond.GetEndAtomIdx()] |= bond_bit

    adjacency_masks = [0] * len(bit_to_bond)
    for incident_bond_mask in bonds_by_atom.values():
        current = incident_bond_mask
        while current:
            low_bit = current & -current
            bit_index = low_bit.bit_length() - 1
            adjacency_masks[bit_index] |= incident_bond_mask & ~low_bit
            current ^= low_bit
    return adjacency_masks


def _normalize_state_mask(
    selected_bond_mask: int,
    bond_atom_masks: list[int],
    aromatic_system_masks: list[tuple[int, int]],
) -> tuple[int, int, tuple[int, ...]]:
    selected_atom_mask = _atom_mask_from_bond_mask(selected_bond_mask, bond_atom_masks)
    return _close_protected_aromatic_system_masks(
        selected_atom_mask,
        selected_bond_mask,
        aromatic_system_masks,
    )


def _fragment_smiles_key(fragment_smiles: str) -> bytes:
    return hashlib.blake2b(fragment_smiles.encode("utf-8"), digest_size=16).digest()


class BondFragmentRowIterator:
    def __init__(
        self,
        mol: Chem.Mol,
        min_atoms: int = 3,
        max_atoms: int | None = None,
        limit: int | None = None,
        debug_fields: bool = False,
        state: dict[str, Any] | None = None,
    ) -> None:
        if max_atoms is None:
            max_atoms = len(heavy_atom_indices(mol))
        if min_atoms < 1:
            raise ValueError("min_atoms must be >= 1.")

        self.mol = mol
        self.min_atoms = min_atoms
        self.max_atoms = max_atoms
        self.limit = limit
        self.debug_fields = debug_fields
        self.exhausted = max_atoms < min_atoms

        aromatic_systems = build_aromatic_systems(mol)
        candidate_bonds = heavy_bond_indices(mol)
        self.bit_to_bond = sorted(candidate_bonds)
        self.bond_to_bit = {
            bond_idx: bit_index for bit_index, bond_idx in enumerate(self.bit_to_bond)
        }
        self.bond_atom_masks = _build_bond_atom_masks(mol, self.bit_to_bond)
        self.aromatic_system_masks = _build_aromatic_system_masks(
            aromatic_systems,
            self.bond_to_bit,
        )
        self.bond_adjacency_masks = _build_bond_adjacency_masks(
            mol,
            self.bit_to_bond,
            self.bond_to_bit,
        )

        if state is None:
            self.visited_states: set[int] = set()
            self.queued_states: set[int] = set()
            self.queue: deque[int] = deque()
            self.unique_smiles_keys: set[bytes] = set()
            self.yielded = 0
            if not self.exhausted:
                for seed_bond_idx in self.bit_to_bond:
                    self._enqueue(1 << self.bond_to_bit[seed_bond_idx])
        else:
            self.visited_states = set(state["visited_states"])
            self.queued_states = set(state["queued_states"])
            self.queue = deque(state["queue"])
            self.unique_smiles_keys = set(state["unique_smiles_keys"])
            self.yielded = int(state["yielded"])
            self.exhausted = bool(state.get("exhausted", self.exhausted))

    def __iter__(self) -> "BondFragmentRowIterator":
        return self

    def __next__(
        self,
    ) -> tuple[
        str,
        int,
        int,
        list[int] | None,
        list[int] | None,
        tuple[int, ...] | None,
        list[str] | None,
    ]:
        if self.exhausted or (self.limit is not None and self.yielded >= self.limit):
            self.exhausted = True
            raise StopIteration

        while self.queue:
            state = self.queue.popleft()
            self.queued_states.discard(state)
            if state in self.visited_states:
                continue
            self.visited_states.add(state)

            selected_atom_mask, selected_bond_mask, protected_units_hit = (
                _normalize_state_mask(
                    state,
                    self.bond_atom_masks,
                    self.aromatic_system_masks,
                )
            )
            atom_count = selected_atom_mask.bit_count()
            row = None
            if self.min_atoms <= atom_count <= self.max_atoms:
                selected_atoms = _atom_mask_to_indices(selected_atom_mask)
                selected_bonds = _mask_to_bonds(selected_bond_mask, self.bit_to_bond)
                fragment_smiles = fragment_smiles_from_atom_bond_set(
                    self.mol,
                    selected_atoms,
                    selected_bonds,
                )
                fragment_key = _fragment_smiles_key(fragment_smiles)
                if fragment_key not in self.unique_smiles_keys:
                    self.unique_smiles_keys.add(fragment_key)
                    atom_indices = None
                    bond_indices = None
                    protected_units = None
                    non_chon_atoms = None
                    if self.debug_fields:
                        atom_indices = selected_atoms
                        bond_indices = selected_bonds
                        protected_units = protected_units_hit
                        non_chon_atoms = non_chon_hetero_atom_symbols(
                            self.mol,
                            selected_atoms,
                        )
                    row = (
                        fragment_smiles,
                        atom_count,
                        selected_bond_mask.bit_count(),
                        atom_indices,
                        bond_indices,
                        protected_units,
                        non_chon_atoms,
                    )
                    self.yielded += 1

            if self.limit is None or self.yielded < self.limit:
                self._expand(selected_bond_mask)
            if row is not None:
                return row

        self.exhausted = True
        raise StopIteration

    def snapshot(self) -> dict[str, Any]:
        return {
            "queue": list(self.queue),
            "queued_states": self.queued_states,
            "visited_states": self.visited_states,
            "unique_smiles_keys": self.unique_smiles_keys,
            "yielded": self.yielded,
            "exhausted": self.exhausted,
        }

    def _enqueue(self, bond_mask: int) -> None:
        closed_atom_mask, closed_bond_mask, _protected_units_hit = _normalize_state_mask(
            bond_mask,
            self.bond_atom_masks,
            self.aromatic_system_masks,
        )
        if closed_atom_mask.bit_count() > self.max_atoms:
            return
        if (
            closed_bond_mask not in self.visited_states
            and closed_bond_mask not in self.queued_states
        ):
            self.queued_states.add(closed_bond_mask)
            self.queue.append(closed_bond_mask)

    def _expand(self, selected_bond_mask: int) -> None:
        frontier_mask = 0
        current = selected_bond_mask
        while current:
            low_bit = current & -current
            bit_index = low_bit.bit_length() - 1
            frontier_mask |= self.bond_adjacency_masks[bit_index]
            current ^= low_bit

        next_bond_mask = frontier_mask & ~selected_bond_mask
        while next_bond_mask:
            low_bit = next_bond_mask & -next_bond_mask
            self._enqueue(selected_bond_mask | low_bit)
            next_bond_mask ^= low_bit


def iter_bond_fragment_rows_non_induced(
    mol: Chem.Mol,
    min_atoms: int = 3,
    max_atoms: int | None = None,
    limit: int | None = None,
    debug_fields: bool = False,
) -> Iterator[
    tuple[
        str,
        int,
        int,
        list[int] | None,
        list[int] | None,
        tuple[int, ...] | None,
        list[str] | None,
    ]
]:
    yield from BondFragmentRowIterator(
        mol,
        min_atoms=min_atoms,
        max_atoms=max_atoms,
        limit=limit,
        debug_fields=debug_fields,
    )


def iter_bond_fragments_non_induced(
    mol: Chem.Mol,
    min_atoms: int = 3,
    max_atoms: int | None = None,
    limit: int | None = None,
    debug_fields: bool = False,
) -> Iterator[dict[str, object]]:
    for (
        fragment_smiles,
        atom_count,
        bond_count,
        atom_indices,
        bond_indices,
        protected_units_hit,
        non_chon_hetero_atoms,
    ) in iter_bond_fragment_rows_non_induced(
        mol,
        min_atoms=min_atoms,
        max_atoms=max_atoms,
        limit=limit,
        debug_fields=debug_fields,
    ):
        record: dict[str, object] = {
            "fragment_smiles": fragment_smiles,
            "atom_count": atom_count,
            "bond_count": bond_count,
        }
        if debug_fields:
            record.update(
                {
                    "atom_indices": atom_indices or [],
                    "bond_indices": bond_indices or [],
                    "protected_units_hit": list(protected_units_hit or ()),
                    "non_chon_hetero_atoms": non_chon_hetero_atoms or [],
                }
            )
        yield record


def enumerate_bond_fragments_non_induced(
    mol: Chem.Mol,
    min_atoms: int = 3,
    max_atoms: int | None = None,
    limit: int | None = None,
) -> list[dict[str, object]]:
    return list(
        iter_bond_fragments_non_induced(
            mol,
            min_atoms=min_atoms,
            max_atoms=max_atoms,
            limit=limit,
            debug_fields=True,
        )
    )


def enumerate_fragments(
    smiles: str,
    limit: int | None = None,
    min_atoms: int = 3,
    max_atoms: int | None = None,
) -> list[dict[str, object]]:
    mol = parse_smiles(smiles)
    return enumerate_bond_fragments_non_induced(
        mol,
        min_atoms=min_atoms,
        max_atoms=max_atoms,
        limit=limit,
    )
