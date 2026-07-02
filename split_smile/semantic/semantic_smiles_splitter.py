from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import mysql.connector
from mysql.connector.connection import MySQLConnection
from mysql.connector.errors import Error as MySQLError
from rdkit import Chem
from rdkit import RDLogger
from rdkit.Chem import BRICS
from rdkit.Chem.MolStandardize import rdMolStandardize
from rdkit.Chem.Scaffolds import MurckoScaffold
from rdkit.Chem.rdMolDescriptors import CalcNumAromaticRings, CalcNumRings

from semantic_stop_utils import infer_stop_decompose_unified


RDLogger.DisableLog("rdApp.*")


DEFAULT_DB_CONFIG = {
    "host": "10.1.100.244",
    "user": "root",
    "port": 3306,
    "password": "zUky@Iot1949.yDB9t",
    "database": "fda",
    "ssl_disabled": True,
    "use_pure": True,
    "connection_timeout": 30,
    "read_timeout": 600,
    "write_timeout": 600,
}

TARGET_TABLE = "smile_split"
DEFAULT_VERSION = "semantic_v1"
BENZENE_PATTERN = Chem.MolFromSmarts("c1ccccc1")
INSERT_BATCH_SIZE = 200

PRIORITY_BY_SEMANTIC = {
    "amino_acid_residue": 1000,
    "amino_acid_side_chain": 950,
    "drug_feature_rule": 920,
    "murcko_scaffold": 800,
    "ring": 780,
    "functional_group": 650,
    "brics": 550,
    "path": 450,
    "sliding": 400,
    "connected_component": 100,
}

FUNCTIONAL_GROUP_PATTERNS = [
    ("hydroxyl", "[OX2H]"),
    ("carboxyl", "[CX3](=O)[OX2H1,O-]"),
    ("amide", "[CX3](=O)[NX3]"),
    ("amine", "[NX3;H2,H1,H0;!$(NC=O)]"),
    ("ether", "[#6]-O-[#6]"),
    ("thioether", "[#6]-S-[#6]"),
    ("nitrile", "C#N"),
    ("nitro", "[N+](=O)[O-]"),
]

RESIDUE_SIDECHAINS = {
    "glycine": [],
    "alanine": ["[*]C"],
    "valine": ["[*]C(C)C"],
    "leucine": ["[*]CC(C)C"],
    "isoleucine": ["[*]C(C)CC"],
    "methionine": ["[*]CCSC"],
    "serine": ["[*]CO"],
    "threonine": ["[*]C(O)C"],
    "cysteine": ["[*]CS"],
    "aspartic_acid": ["[*]CC(=O)O", "[*]CC([O-])=O"],
    "glutamic_acid": ["[*]CCC(=O)O", "[*]CCC([O-])=O"],
    "asparagine": ["[*]CC(N)=O"],
    "glutamine": ["[*]CCC(N)=O"],
    "lysine": ["[*]CCCCN", "[*]CCCC[NH3+]"],
    "arginine": ["[*]CCCNC(N)=N", "[*]CCCNC(=[NH2+])N"],
    "histidine": ["[*]Cc1cnc[nH]1", "[*]Cc1ncc[nH]1"],
    "phenylalanine": ["[*]Cc1ccccc1"],
    "tyrosine": ["[*]Cc1ccc(O)cc1"],
    "tryptophan": ["[*]Cc1c[nH]c2ccccc12", "[*]Cc1c[nH]c2ccccc21"],
}

RESIDUE_CLASS_MAP = {
    "glycine": "special",
    "alanine": "hydrophobic",
    "valine": "hydrophobic",
    "leucine": "hydrophobic",
    "isoleucine": "hydrophobic",
    "methionine": "hydrophobic",
    "proline": "hydrophobic",
    "phenylalanine": "aromatic",
    "tyrosine": "aromatic_polar",
    "tryptophan": "aromatic",
    "serine": "polar_uncharged",
    "threonine": "polar_uncharged",
    "asparagine": "polar_uncharged",
    "glutamine": "polar_uncharged",
    "cysteine": "polar_sulfur",
    "aspartic_acid": "acidic",
    "glutamic_acid": "acidic",
    "lysine": "basic",
    "arginine": "basic",
    "histidine": "basic",
}


@dataclass
class SplitConfig:
    min_path_atoms: int = 3
    max_path_atoms: int = 10
    min_sliding_atoms: int = 3
    max_sliding_atoms: int = 10
    enable_sliding: bool = False
    enable_murcko: bool = True
    enable_ring: bool = True
    enable_functional_group: bool = True
    enable_brics: bool = True
    enable_path: bool = True
    path_min_heavy_atoms: int = 3
    path_require_carbon: bool = True


@dataclass
class FragmentRecord:
    fragment_smiles: str
    fragment_type: str
    semantic_type: str
    atom_indices: list[int]
    source: str
    protected: bool
    stop_decompose: bool
    stop_reason: str
    amino_acid_name: str = ""
    is_complete_residue: bool = False
    is_side_chain: bool = False
    side_chain_smiles: str = ""
    side_chain_class: str = ""
    heavy_atoms: int = 0
    rings: int = 0
    aromatic_rings: int = 0
    has_benzene: bool = False
    priority: int = 0


@dataclass
class AminoAcidMatch:
    residue_name: str
    residue_class: str
    alpha_carbon_idx: int
    residue_atoms: set[int]
    sidechain_atoms: set[int]
    sidechain_smiles: str
    residue_smiles: str


@dataclass(frozen=True, slots=True)
class DBFragmentRow:
    drug_number: str
    indications: str
    input_smiles: str
    canonical_smiles: str
    fragment_smiles: str
    fragment_type: str
    semantic_type: str
    atom_indices_json: str
    source: str
    protected: int
    stop_decompose: int
    stop_reason: str
    amino_acid_name: str
    is_complete_residue: int
    is_side_chain: int
    side_chain_smiles: str
    side_chain_class: str
    heavy_atoms: int
    rings: int
    aromatic_rings: int
    has_benzene: int
    priority: int
    fragment_key: str
    version: str


def normalize_smiles_text(smiles: str) -> str:
    text = (smiles or "").strip()
    return text.replace("[[*]]", "[*]").replace("锛?", "*")


def standardize_smiles(smiles: str) -> tuple[str, Chem.Mol]:
    normalized = normalize_smiles_text(smiles)
    mol = Chem.MolFromSmiles(normalized)
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smiles}")

    cleanup = rdMolStandardize.Cleanup(mol)
    parent = rdMolStandardize.FragmentParent(cleanup)
    uncharger = rdMolStandardize.Uncharger()
    neutral = uncharger.uncharge(parent)
    Chem.SanitizeMol(neutral)
    canonical_smiles = Chem.MolToSmiles(neutral, canonical=True, isomericSmiles=True)
    return canonical_smiles, neutral


def canonicalize_template(smiles: str) -> str:
    mol = Chem.MolFromSmiles(normalize_smiles_text(smiles))
    if mol is None:
        raise ValueError(f"Bad template SMILES: {smiles}")
    return Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True)


CANONICAL_RESIDUE_SIDECHAINS = {
    residue: {canonicalize_template(smiles) for smiles in smiles_list}
    for residue, smiles_list in RESIDUE_SIDECHAINS.items()
}


def _fragment_smiles_from_atoms(mol: Chem.Mol, atom_indices: Iterable[int]) -> str:
    return Chem.MolFragmentToSmiles(
        mol,
        atomsToUse=sorted(set(atom_indices)),
        canonical=True,
        isomericSmiles=True,
    )


def _compute_metrics(fragment_smiles: str) -> tuple[int, int, int, bool]:
    mol = Chem.MolFromSmiles(fragment_smiles)
    if mol is None:
        return 0, 0, 0, False
    return (
        mol.GetNumHeavyAtoms(),
        CalcNumRings(mol),
        CalcNumAromaticRings(mol),
        mol.HasSubstructMatch(BENZENE_PATTERN),
    )


def _compute_subset_metrics(mol: Chem.Mol, atom_indices: Iterable[int]) -> tuple[int, int, int, bool]:
    atom_set = set(atom_indices)
    heavy_atoms = sum(1 for atom_idx in atom_set if mol.GetAtomWithIdx(atom_idx).GetAtomicNum() > 1)
    rings = 0
    aromatic_rings = 0
    has_benzene = False
    for ring_atoms in mol.GetRingInfo().AtomRings():
        ring_set = set(ring_atoms)
        if not ring_set.issubset(atom_set):
            continue
        rings += 1
        if all(mol.GetAtomWithIdx(atom_idx).GetIsAromatic() for atom_idx in ring_set):
            aromatic_rings += 1
        if len(ring_set) == 6 and all(mol.GetAtomWithIdx(atom_idx).GetIsAromatic() for atom_idx in ring_set):
            has_benzene = True
    return heavy_atoms, rings, aromatic_rings, has_benzene


def is_carbonyl_carbon(atom: Chem.Atom) -> bool:
    if atom.GetAtomicNum() != 6:
        return False
    for bond in atom.GetBonds():
        if bond.GetBondType() == Chem.BondType.DOUBLE and bond.GetOtherAtom(atom).GetAtomicNum() == 8:
            return True
    return False


def carbonyl_oxygen_indices(atom: Chem.Atom) -> set[int]:
    return {
        bond.GetOtherAtom(atom).GetIdx()
        for bond in atom.GetBonds()
        if bond.GetOtherAtom(atom).GetAtomicNum() == 8
    }


def bfs_sidechain_atoms(mol: Chem.Mol, roots: Sequence[int], blocked: set[int]) -> set[int]:
    visited: set[int] = set()
    queue = deque(roots)
    while queue:
        atom_idx = queue.popleft()
        if atom_idx in visited or atom_idx in blocked:
            continue
        visited.add(atom_idx)
        atom = mol.GetAtomWithIdx(atom_idx)
        for neighbor in atom.GetNeighbors():
            neighbor_idx = neighbor.GetIdx()
            if neighbor_idx not in visited and neighbor_idx not in blocked:
                queue.append(neighbor_idx)
    return visited


def classify_sidechain(sidechain_smiles: str, closes_to_backbone_n: bool) -> tuple[str, str]:
    if closes_to_backbone_n:
        return "proline", RESIDUE_CLASS_MAP["proline"]
    if sidechain_smiles == "[*]":
        return "glycine", RESIDUE_CLASS_MAP["glycine"]
    for residue_name, variants in CANONICAL_RESIDUE_SIDECHAINS.items():
        if sidechain_smiles in variants:
            return residue_name, RESIDUE_CLASS_MAP[residue_name]
    return "amino_acid_like", "unclassified"


def detect_amino_acid_residues(mol: Chem.Mol) -> list[AminoAcidMatch]:
    matches: list[AminoAcidMatch] = []
    seen_alpha: set[int] = set()

    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() != 6 or atom.GetIsAromatic():
            continue
        neighbors = list(atom.GetNeighbors())
        n_neighbors = [neighbor for neighbor in neighbors if neighbor.GetAtomicNum() == 7]
        carbonyl_neighbors = [neighbor for neighbor in neighbors if is_carbonyl_carbon(neighbor)]
        if not n_neighbors or not carbonyl_neighbors:
            continue

        alpha_idx = atom.GetIdx()
        if alpha_idx in seen_alpha:
            continue

        backbone_n = n_neighbors[0]
        carbonyl_c = carbonyl_neighbors[0]
        blocked = {alpha_idx, backbone_n.GetIdx(), carbonyl_c.GetIdx()} | carbonyl_oxygen_indices(carbonyl_c)
        sidechain_roots = [
            neighbor.GetIdx()
            for neighbor in neighbors
            if neighbor.GetIdx() not in blocked and neighbor.GetAtomicNum() > 1
        ]
        sidechain_atoms = bfs_sidechain_atoms(mol, sidechain_roots, blocked)

        closes_to_backbone_n = False
        for sidechain_idx in sidechain_atoms:
            sidechain_atom = mol.GetAtomWithIdx(sidechain_idx)
            if any(neighbor.GetIdx() == backbone_n.GetIdx() for neighbor in sidechain_atom.GetNeighbors()):
                closes_to_backbone_n = True
                break

        residue_atoms = set(blocked) | sidechain_atoms
        sidechain_smiles = "[*]" if not sidechain_atoms else _fragment_smiles_from_atoms(mol, sidechain_atoms)
        residue_name, residue_class = classify_sidechain(sidechain_smiles, closes_to_backbone_n)
        residue_smiles = _fragment_smiles_from_atoms(mol, residue_atoms)

        matches.append(
            AminoAcidMatch(
                residue_name=residue_name,
                residue_class=residue_class,
                alpha_carbon_idx=alpha_idx,
                residue_atoms=residue_atoms,
                sidechain_atoms=sidechain_atoms,
                sidechain_smiles=sidechain_smiles,
                residue_smiles=residue_smiles,
            )
        )
        seen_alpha.add(alpha_idx)

    return matches


def protected_atom_sets(matches: Sequence[AminoAcidMatch]) -> list[set[int]]:
    return [set(match.residue_atoms) for match in matches]


def uncovered_atoms(mol: Chem.Mol, protected_sets: Sequence[set[int]]) -> set[int]:
    protected: set[int] = set()
    for atom_set in protected_sets:
        protected.update(atom_set)
    return {atom.GetIdx() for atom in mol.GetAtoms()} - protected


def connected_components(mol: Chem.Mol, atom_pool: set[int]) -> list[set[int]]:
    remaining = set(atom_pool)
    components: list[set[int]] = []
    while remaining:
        start = remaining.pop()
        queue = deque([start])
        component = {start}
        while queue:
            atom_idx = queue.popleft()
            atom = mol.GetAtomWithIdx(atom_idx)
            for neighbor in atom.GetNeighbors():
                neighbor_idx = neighbor.GetIdx()
                if neighbor_idx in remaining:
                    remaining.remove(neighbor_idx)
                    component.add(neighbor_idx)
                    queue.append(neighbor_idx)
        components.append(component)
    return components


def build_fragment_record(
    mol: Chem.Mol,
    atom_indices: Iterable[int],
    fragment_type: str,
    semantic_type: str,
    source: str,
    protected: bool,
    stop_decompose: bool,
    stop_reason: str,
    amino_acid_name: str = "",
    is_complete_residue: bool = False,
    is_side_chain: bool = False,
    side_chain_smiles: str = "",
    side_chain_class: str = "",
    fragment_smiles_override: str = "",
) -> FragmentRecord:
    atom_list = sorted(set(atom_indices))
    fragment_smiles = fragment_smiles_override or _fragment_smiles_from_atoms(mol, atom_list)
    heavy_atoms, rings, aromatic_rings, has_benzene = _compute_metrics(fragment_smiles)
    if heavy_atoms == 0 and atom_list:
        heavy_atoms, rings, aromatic_rings, has_benzene = _compute_subset_metrics(mol, atom_list)
    priority = PRIORITY_BY_SEMANTIC.get(semantic_type, 0)
    return FragmentRecord(
        fragment_smiles=fragment_smiles,
        fragment_type=fragment_type,
        semantic_type=semantic_type,
        atom_indices=atom_list,
        source=source,
        protected=protected,
        stop_decompose=stop_decompose,
        stop_reason=stop_reason,
        amino_acid_name=amino_acid_name,
        is_complete_residue=is_complete_residue,
        is_side_chain=is_side_chain,
        side_chain_smiles=side_chain_smiles,
        side_chain_class=side_chain_class,
        heavy_atoms=heavy_atoms,
        rings=rings,
        aromatic_rings=aromatic_rings,
        has_benzene=has_benzene,
        priority=priority,
    )


def emit_amino_acid_fragments(mol: Chem.Mol, matches: Sequence[AminoAcidMatch]) -> list[FragmentRecord]:
    fragments: list[FragmentRecord] = []
    for match in matches:
        residue_stop, residue_reason = infer_stop_decompose_unified(
            semantic_type="amino_acid_residue",
            semantic_class=[f"amino_acid:{match.residue_name}", f"amino_acid_class:{match.residue_class}"],
            priority=PRIORITY_BY_SEMANTIC["amino_acid_residue"],
            heavy_atoms=len(match.residue_atoms),
            ring_count=0,
            aromatic_ring_count=0,
            rotatable_bonds=0,
            is_complete_residue=True,
        )
        fragments.append(
            build_fragment_record(
                mol,
                match.residue_atoms,
                fragment_type=f"amino_acid_residue:{match.residue_name}",
                semantic_type="amino_acid_residue",
                source="semantic_amino_acid",
                protected=True,
                stop_decompose=residue_stop,
                stop_reason=residue_reason,
                amino_acid_name=match.residue_name,
                is_complete_residue=True,
                side_chain_smiles="" if match.sidechain_smiles == "[*]" else match.sidechain_smiles,
                side_chain_class=match.residue_class,
            )
        )

        if not match.sidechain_atoms:
            continue

        sidechain_mol = Chem.MolFromSmiles(match.sidechain_smiles)
        sidechain_rings = sidechain_mol.GetRingInfo().NumRings() if sidechain_mol is not None else 0
        sidechain_stop, sidechain_reason = infer_stop_decompose_unified(
            semantic_type="amino_acid_side_chain",
            semantic_class=[f"amino_acid:{match.residue_name}", f"amino_acid_class:{match.residue_class}"],
            priority=PRIORITY_BY_SEMANTIC["amino_acid_side_chain"],
            heavy_atoms=sidechain_mol.GetNumHeavyAtoms() if sidechain_mol is not None else len(match.sidechain_atoms),
            ring_count=sidechain_rings,
            aromatic_ring_count=0,
            rotatable_bonds=0,
            is_side_chain=True,
        )
        fragments.append(
            build_fragment_record(
                mol,
                match.sidechain_atoms,
                fragment_type=f"amino_acid_sidechain:{match.residue_name}:{match.residue_class}",
                semantic_type="amino_acid_side_chain",
                source="semantic_amino_acid_sidechain",
                protected=True,
                stop_decompose=sidechain_stop,
                stop_reason=sidechain_reason,
                amino_acid_name=match.residue_name,
                is_side_chain=True,
                side_chain_smiles=match.sidechain_smiles,
                side_chain_class=match.residue_class,
            )
        )
    return fragments


def enumerate_functional_groups(mol: Chem.Mol, allowed_atoms: set[int]) -> list[tuple[set[int], str]]:
    found: list[tuple[set[int], str]] = []
    for label, smarts in FUNCTIONAL_GROUP_PATTERNS:
        pattern = Chem.MolFromSmarts(smarts)
        if pattern is None:
            continue
        for match in mol.GetSubstructMatches(pattern, uniquify=True):
            atom_set = set(match)
            if atom_set and atom_set.issubset(allowed_atoms):
                found.append((atom_set, label))
    return found


def enumerate_ring_fragments(mol: Chem.Mol, allowed_atoms: set[int]) -> list[set[int]]:
    return [set(ring) for ring in mol.GetRingInfo().AtomRings() if set(ring).issubset(allowed_atoms)]


def enumerate_path_fragments(mol: Chem.Mol, allowed_atoms: set[int], min_atoms: int, max_atoms: int) -> list[set[int]]:
    paths: set[tuple[int, ...]] = set()
    for atom_idx in allowed_atoms:
        queue = deque([(atom_idx, [atom_idx])])
        while queue:
            current, path = queue.popleft()
            if min_atoms <= len(path) <= max_atoms:
                paths.add(tuple(sorted(path)))
            if len(path) == max_atoms:
                continue
            atom = mol.GetAtomWithIdx(current)
            for neighbor in atom.GetNeighbors():
                neighbor_idx = neighbor.GetIdx()
                if neighbor_idx in allowed_atoms and neighbor_idx not in path:
                    queue.append((neighbor_idx, path + [neighbor_idx]))
    return [set(path) for path in sorted(paths)]


def is_informative_path_fragment(mol: Chem.Mol, atom_indices: Iterable[int], config: SplitConfig) -> bool:
    atom_list = sorted(set(atom_indices))
    if len(atom_list) < config.min_path_atoms:
        return False

    heavy_atoms, rings, aromatic_rings, has_benzene = _compute_subset_metrics(mol, atom_list)
    if heavy_atoms < config.path_min_heavy_atoms:
        return False

    atomic_numbers = [mol.GetAtomWithIdx(atom_idx).GetAtomicNum() for atom_idx in atom_list]
    if config.path_require_carbon and 6 not in atomic_numbers:
        return False

    if all(atomic_number == 6 for atomic_number in atomic_numbers):
        return False

    aromatic_atom_count = sum(1 for atom_idx in atom_list if mol.GetAtomWithIdx(atom_idx).GetIsAromatic())
    if aromatic_atom_count > 0 and rings == 0:
        return False

    if all(mol.GetAtomWithIdx(atom_idx).GetIsAromatic() for atom_idx in atom_list) and rings == 0:
        return False

    canonical_smiles = _fragment_smiles_from_atoms(mol, atom_list)
    fragment_mol = Chem.MolFromSmiles(canonical_smiles)
    if fragment_mol is None:
        return False

    if fragment_mol.GetNumHeavyAtoms() <= 2:
        return False

    if any(atom.GetIsAromatic() for atom in fragment_mol.GetAtoms()) and fragment_mol.GetRingInfo().NumRings() == 0:
        return False

    if all(atom.GetIsAromatic() for atom in fragment_mol.GetAtoms()) and fragment_mol.GetRingInfo().NumRings() == 0:
        return False

    return True


def enumerate_sliding_fragments(mol: Chem.Mol, allowed_atoms: set[int], min_atoms: int, max_atoms: int) -> list[set[int]]:
    adjacency = {
        atom_idx: sorted(
            neighbor.GetIdx()
            for neighbor in mol.GetAtomWithIdx(atom_idx).GetNeighbors()
            if neighbor.GetIdx() in allowed_atoms
        )
        for atom_idx in allowed_atoms
    }
    results: set[frozenset[int]] = set()

    def grow(current: frozenset[int]) -> None:
        if len(current) > max_atoms:
            return
        if min_atoms <= len(current) <= max_atoms:
            results.add(current)
        frontier: set[int] = set()
        for atom_idx in current:
            frontier.update(adjacency[atom_idx])
        for next_idx in frontier - set(current):
            grow(frozenset(set(current) | {next_idx}))

    for atom_idx in sorted(allowed_atoms):
        grow(frozenset({atom_idx}))
    return [set(item) for item in results]


def enumerate_brics_fragments(mol: Chem.Mol, allowed_atoms: set[int]) -> list[set[int]]:
    bonds_to_break: list[int] = []
    for bond_pair, _ in BRICS.FindBRICSBonds(mol):
        bond = mol.GetBondBetweenAtoms(*bond_pair)
        if bond is None:
            continue
        begin_idx, end_idx = bond_pair
        if begin_idx in allowed_atoms and end_idx in allowed_atoms:
            bonds_to_break.append(bond.GetIdx())
    if not bonds_to_break:
        return []
    fragment_mol = Chem.FragmentOnBonds(mol, bonds_to_break, addDummies=False)
    return [set(fragment) for fragment in Chem.GetMolFrags(fragment_mol) if set(fragment).issubset(allowed_atoms)]


def enumerate_murcko_fragments(mol: Chem.Mol, allowed_atoms: set[int]) -> list[set[int]]:
    scaffold = MurckoScaffold.GetScaffoldForMol(mol)
    if scaffold is None or scaffold.GetNumAtoms() == 0:
        return []
    results: list[set[int]] = []
    for match in mol.GetSubstructMatches(scaffold, uniquify=True):
        atom_set = set(match)
        if atom_set.issubset(allowed_atoms):
            results.append(atom_set)
    return results


def enumerate_generic_fragments(mol: Chem.Mol, protected_sets: Sequence[set[int]], config: SplitConfig) -> list[FragmentRecord]:
    fragments: list[FragmentRecord] = []
    allowed = uncovered_atoms(mol, protected_sets)
    components = connected_components(mol, allowed)

    if config.enable_functional_group:
        for atom_set, label in enumerate_functional_groups(mol, allowed):
            stop_decompose, stop_reason = infer_stop_decompose_unified(
                semantic_type="functional_group",
                semantic_class=[label],
                priority=50 if label in {"nitro", "nitrile", "amide", "carboxyl"} else 25,
                heavy_atoms=len(atom_set),
                ring_count=0,
                aromatic_ring_count=0,
                rotatable_bonds=0,
            )
            fragments.append(
                build_fragment_record(
                    mol,
                    atom_set,
                    fragment_type=f"functional_group:{label}",
                    semantic_type="functional_group",
                    source="functional_group",
                    protected=False,
                    stop_decompose=stop_decompose,
                    stop_reason=stop_reason,
                )
            )

    if config.enable_murcko:
        for atom_set in enumerate_murcko_fragments(mol, allowed):
            fragments.append(
                build_fragment_record(
                    mol,
                    atom_set,
                    fragment_type="murcko_scaffold",
                    semantic_type="murcko_scaffold",
                    source="murcko_scaffold",
                    protected=False,
                    stop_decompose=False,
                    stop_reason="default_fallback_non_terminal_unit",
                )
            )

    if config.enable_ring:
        for atom_set in enumerate_ring_fragments(mol, allowed):
            fragments.append(
                build_fragment_record(
                    mol,
                    atom_set,
                    fragment_type="ring",
                    semantic_type="ring",
                    source="ring",
                    protected=False,
                    stop_decompose=False,
                    stop_reason="default_fallback_non_terminal_unit",
                )
            )

    if config.enable_brics:
        for atom_set in enumerate_brics_fragments(mol, allowed):
            fragments.append(
                build_fragment_record(
                    mol,
                    atom_set,
                    fragment_type="brics",
                    semantic_type="brics",
                    source="BRICS",
                    protected=False,
                    stop_decompose=False,
                    stop_reason="default_fallback_non_terminal_unit",
                )
            )

    if config.enable_path:
        for atom_set in enumerate_path_fragments(mol, allowed, config.min_path_atoms, config.max_path_atoms):
            if not is_informative_path_fragment(mol, atom_set, config):
                continue
            fragments.append(
                build_fragment_record(
                    mol,
                    atom_set,
                    fragment_type="path",
                    semantic_type="path",
                    source="path",
                    protected=False,
                    stop_decompose=False,
                    stop_reason="default_fallback_non_terminal_unit",
                )
            )

    if config.enable_sliding:
        for atom_set in enumerate_sliding_fragments(mol, allowed, config.min_sliding_atoms, config.max_sliding_atoms):
            fragments.append(
                build_fragment_record(
                    mol,
                    atom_set,
                    fragment_type="sliding",
                    semantic_type="sliding",
                    source="connected_subgraph",
                    protected=False,
                    stop_decompose=False,
                    stop_reason="default_fallback_non_terminal_unit",
                )
            )

    if not fragments:
        for component in components:
            fragments.append(
                build_fragment_record(
                    mol,
                    component,
                    fragment_type="connected_component",
                    semantic_type="connected_component",
                    source="connected_component",
                    protected=False,
                    stop_decompose=False,
                    stop_reason="default_fallback_non_terminal_unit",
                )
            )

    return fragments


def load_drug_feature_fragments_by_indication(connection: MySQLConnection) -> dict[str, list[str]]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT `indication`, `fragment`
            FROM `drug_feature`
            WHERE `indication` IS NOT NULL
              AND TRIM(`indication`) <> ''
              AND `fragment` IS NOT NULL
              AND TRIM(`fragment`) <> ''
            """
        )
        fragments_by_indication: dict[str, list[str]] = {}
        seen: dict[str, set[str]] = {}
        for indication, fragment in cursor.fetchall():
            indication_text = str(indication).strip()
            fragment_text = str(fragment).strip()
            if not indication_text or not fragment_text:
                continue
            try:
                canonical = canonicalize_template(fragment_text)
            except Exception:
                continue
            fragments_by_indication.setdefault(indication_text, [])
            seen.setdefault(indication_text, set())
            if canonical in seen[indication_text]:
                continue
            seen[indication_text].add(canonical)
            fragments_by_indication[indication_text].append(canonical)
        return fragments_by_indication
    finally:
        cursor.close()


def emit_drug_feature_rule_fragments(
    mol: Chem.Mol,
    indications: Sequence[str],
    drug_feature_fragments_by_indication: dict[str, list[str]],
) -> list[FragmentRecord]:
    fragments: list[FragmentRecord] = []
    for indication in indications:
        for canonical_fragment in drug_feature_fragments_by_indication.get(indication, []):
            feature_mol = Chem.MolFromSmiles(canonical_fragment)
            if feature_mol is None:
                continue
            for match in mol.GetSubstructMatches(feature_mol, uniquify=True):
                fragments.append(
                    build_fragment_record(
                        mol,
                        match,
                        fragment_type="drug_feature_rule",
                        semantic_type="drug_feature_rule",
                        source=f"drug_feature:{indication}",
                        protected=False,
                        stop_decompose=False,
                        stop_reason="matched_indication_drug_feature_fragment",
                        fragment_smiles_override=canonical_fragment,
                    )
                )
    return fragments


def is_low_information_fragment(fragment: FragmentRecord) -> bool:
    if fragment.heavy_atoms <= 2:
        return True

    fragment_mol = Chem.MolFromSmiles(fragment.fragment_smiles)
    if fragment_mol is None:
        return False

    heavy_atoms = fragment_mol.GetNumHeavyAtoms()
    if heavy_atoms <= 2:
        return True

    if (
        heavy_atoms <= 4
        and fragment_mol.GetRingInfo().NumRings() == 0
        and all(atom.GetAtomicNum() == 6 for atom in fragment_mol.GetAtoms())
    ):
        return True

    return False


def resolve_fragment_priority(fragments: Sequence[FragmentRecord]) -> list[FragmentRecord]:
    residue_atom_sets = [
        set(fragment.atom_indices)
        for fragment in fragments
        if fragment.is_complete_residue
    ]
    filtered: list[FragmentRecord] = []
    for fragment in fragments:
        if is_low_information_fragment(fragment):
            continue
        current_atoms = set(fragment.atom_indices)
        if (
            fragment.semantic_type != "drug_feature_rule"
            and not fragment.is_complete_residue
            and any(current_atoms < residue_atoms for residue_atoms in residue_atom_sets)
        ):
            continue
        filtered.append(fragment)

    ordered = sorted(
        filtered,
        key=lambda item: (
            -item.priority,
            -int(item.protected),
            -item.heavy_atoms,
            item.fragment_type,
            tuple(item.atom_indices),
        ),
    )
    best_by_atom_set: OrderedDict[tuple[int, ...], FragmentRecord] = OrderedDict()
    for fragment in ordered:
        atom_key = tuple(fragment.atom_indices)
        best_by_atom_set.setdefault(atom_key, fragment)

    deduped_by_smiles: OrderedDict[str, FragmentRecord] = OrderedDict()
    for fragment in best_by_atom_set.values():
        deduped_by_smiles.setdefault(fragment.fragment_smiles, fragment)
    return list(deduped_by_smiles.values())


def split_smiles_semantically(
    smiles: str,
    *,
    indications: Sequence[str] | None = None,
    drug_feature_fragments_by_indication: dict[str, list[str]] | None = None,
    config: SplitConfig | None = None,
) -> dict[str, object]:
    config = config or SplitConfig()
    canonical_smiles, mol = standardize_smiles(smiles)
    amino_matches = detect_amino_acid_residues(mol)
    semantic_fragments = emit_amino_acid_fragments(mol, amino_matches)
    drug_feature_rule_fragments = emit_drug_feature_rule_fragments(
        mol,
        indications or [],
        drug_feature_fragments_by_indication or {},
    )
    generic_fragments = enumerate_generic_fragments(mol, protected_atom_sets(amino_matches), config)

    fragments = resolve_fragment_priority(
        semantic_fragments + drug_feature_rule_fragments + generic_fragments
    )
    return {
        "input_smiles": smiles,
        "canonical_smiles": canonical_smiles,
        "indications": list(indications or []),
        "fragments": [asdict(fragment) for fragment in fragments],
        "counts": {
            "total_fragments": len(fragments),
            "amino_acid_residue": sum(1 for item in fragments if item.semantic_type == "amino_acid_residue"),
            "amino_acid_side_chain": sum(1 for item in fragments if item.semantic_type == "amino_acid_side_chain"),
            "drug_feature_rule": sum(1 for item in fragments if item.semantic_type == "drug_feature_rule"),
            "murcko_scaffold": sum(1 for item in fragments if item.semantic_type == "murcko_scaffold"),
            "ring": sum(1 for item in fragments if item.semantic_type == "ring"),
            "functional_group": sum(1 for item in fragments if item.semantic_type == "functional_group"),
            "brics": sum(1 for item in fragments if item.semantic_type == "brics"),
            "path": sum(1 for item in fragments if item.semantic_type == "path"),
            "sliding": sum(1 for item in fragments if item.semantic_type == "sliding"),
        },
    }


def build_fragment_key(
    canonical_smiles: str,
    fragment_smiles: str,
) -> str:
    raw = "|".join(
        [
            canonical_smiles,
            fragment_smiles,
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def detect_column_name(connection: MySQLConnection, table_name: str, candidates: list[str]) -> str:
    cursor = connection.cursor()
    try:
        cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
        column_names = {row[0] for row in cursor.fetchall()}
    finally:
        cursor.close()
    for candidate in candidates:
        if candidate in column_names:
            return candidate
    raise ValueError(f"None of the candidate columns {candidates!r} exist in table `{table_name}`.")


def connect(db_config: dict[str, object]) -> MySQLConnection:
    supported_config = dict(db_config)
    for key in ("read_timeout", "write_timeout"):
        supported_config.pop(key, None)
    return mysql.connector.connect(**supported_config)


def create_table_if_needed(connection: MySQLConnection) -> None:
    ddl = f"""
    CREATE TABLE IF NOT EXISTS `{TARGET_TABLE}` (
      `id` bigint NOT NULL AUTO_INCREMENT,
      `drug_number` varchar(255) DEFAULT NULL,
      `indications` text,
      `input_smiles` text,
      `canonical_smiles` text,
      `fragment_smiles` text,
      `fragment_type` varchar(255) DEFAULT NULL,
      `semantic_type` varchar(255) DEFAULT NULL,
      `atom_indices_json` json DEFAULT NULL,
      `source` varchar(255) DEFAULT NULL,
      `protected` tinyint(1) DEFAULT NULL,
      `stop_decompose` tinyint(1) DEFAULT NULL,
      `stop_reason` varchar(255) DEFAULT NULL,
      `amino_acid_name` varchar(255) DEFAULT NULL,
      `is_complete_residue` tinyint(1) DEFAULT NULL,
      `is_side_chain` tinyint(1) DEFAULT NULL,
      `side_chain_smiles` text,
      `side_chain_class` varchar(255) DEFAULT NULL,
      `heavy_atoms` int DEFAULT NULL,
      `rings` int DEFAULT NULL,
      `aromatic_rings` int DEFAULT NULL,
      `has_benzene` tinyint(1) DEFAULT NULL,
      `priority` int DEFAULT NULL,
      `fragment_key` varchar(40) DEFAULT NULL,
      `version` varchar(32) DEFAULT NULL,
      `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (`id`),
      KEY `idx_smile_split_drug_number` (`drug_number`),
      KEY `idx_smile_split_type` (`fragment_type`),
      KEY `idx_smile_split_version` (`version`),
      UNIQUE KEY `uq_smile_split_drug_version_fragment` (`drug_number`,`version`,`fragment_key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
    """
    cursor = connection.cursor()
    try:
        cursor.execute(ddl)
        connection.commit()
    finally:
        cursor.close()


def ensure_table_schema(connection: MySQLConnection) -> None:
    required_columns = {
        "indications": "ADD COLUMN `indications` TEXT NULL AFTER `drug_number`",
        "input_smiles": "ADD COLUMN `input_smiles` TEXT NULL AFTER `indications`",
        "canonical_smiles": "ADD COLUMN `canonical_smiles` TEXT NULL AFTER `input_smiles`",
        "fragment_smiles": "ADD COLUMN `fragment_smiles` TEXT NULL AFTER `canonical_smiles`",
        "fragment_type": "ADD COLUMN `fragment_type` varchar(255) DEFAULT NULL AFTER `fragment_smiles`",
        "semantic_type": "ADD COLUMN `semantic_type` varchar(255) DEFAULT NULL AFTER `fragment_type`",
        "atom_indices_json": "ADD COLUMN `atom_indices_json` JSON NULL AFTER `semantic_type`",
        "source": "ADD COLUMN `source` varchar(255) DEFAULT NULL AFTER `atom_indices_json`",
        "protected": "ADD COLUMN `protected` tinyint(1) DEFAULT NULL AFTER `source`",
        "stop_decompose": "ADD COLUMN `stop_decompose` tinyint(1) DEFAULT NULL AFTER `protected`",
        "stop_reason": "ADD COLUMN `stop_reason` varchar(255) DEFAULT NULL AFTER `stop_decompose`",
        "amino_acid_name": "ADD COLUMN `amino_acid_name` varchar(255) DEFAULT NULL AFTER `stop_reason`",
        "is_complete_residue": "ADD COLUMN `is_complete_residue` tinyint(1) DEFAULT NULL AFTER `amino_acid_name`",
        "is_side_chain": "ADD COLUMN `is_side_chain` tinyint(1) DEFAULT NULL AFTER `is_complete_residue`",
        "side_chain_smiles": "ADD COLUMN `side_chain_smiles` TEXT NULL AFTER `is_side_chain`",
        "side_chain_class": "ADD COLUMN `side_chain_class` varchar(255) DEFAULT NULL AFTER `side_chain_smiles`",
        "heavy_atoms": "ADD COLUMN `heavy_atoms` int DEFAULT NULL AFTER `side_chain_class`",
        "rings": "ADD COLUMN `rings` int DEFAULT NULL AFTER `heavy_atoms`",
        "aromatic_rings": "ADD COLUMN `aromatic_rings` int DEFAULT NULL AFTER `rings`",
        "has_benzene": "ADD COLUMN `has_benzene` tinyint(1) DEFAULT NULL AFTER `aromatic_rings`",
        "priority": "ADD COLUMN `priority` int DEFAULT NULL AFTER `has_benzene`",
        "fragment_key": "ADD COLUMN `fragment_key` varchar(40) DEFAULT NULL AFTER `priority`",
        "created_at": "ADD COLUMN `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP AFTER `version`",
    }

    cursor = connection.cursor()
    try:
        cursor.execute(f"SHOW COLUMNS FROM `{TARGET_TABLE}`")
        existing_columns = {row[0] for row in cursor.fetchall()}
        for column_name, ddl in required_columns.items():
            if column_name in existing_columns:
                continue
            cursor.execute(f"ALTER TABLE `{TARGET_TABLE}` {ddl}")

        cursor.execute(f"SHOW INDEX FROM `{TARGET_TABLE}`")
        existing_indexes = {row[2] for row in cursor.fetchall()}
        if "idx_smile_split_drug_number" not in existing_indexes:
            cursor.execute(f"ALTER TABLE `{TARGET_TABLE}` ADD KEY `idx_smile_split_drug_number` (`drug_number`)")
        if "idx_smile_split_type" not in existing_indexes:
            cursor.execute(f"ALTER TABLE `{TARGET_TABLE}` ADD KEY `idx_smile_split_type` (`fragment_type`)")
        if "idx_smile_split_version" not in existing_indexes:
            cursor.execute(f"ALTER TABLE `{TARGET_TABLE}` ADD KEY `idx_smile_split_version` (`version`)")
        if "uq_smile_split_drug_version_fragment" not in existing_indexes:
            cursor.execute(
                f"ALTER TABLE `{TARGET_TABLE}` "
                "ADD UNIQUE KEY `uq_smile_split_drug_version_fragment` (`drug_number`,`version`,`fragment_key`)"
            )
        connection.commit()
    finally:
        cursor.close()


def fetch_source_rows(
    connection: MySQLConnection,
    drug_number: str | None,
    offset: int,
    limit: int | None,
) -> list[tuple[str, str]]:
    table_name = "drug_ingredient_smiles"
    drug_number_column = detect_column_name(connection, table_name, ["drug-number", "Drug-number", "drug_number", "DrugNumber"])
    smiles_column = detect_column_name(connection, table_name, ["smile", "smiles", "Smile", "Smiles", "SMILES"])

    query = (
        f"SELECT `{drug_number_column}`, `{smiles_column}` "
        f"FROM `{table_name}` "
        f"WHERE `{smiles_column}` IS NOT NULL "
        f"AND TRIM(`{smiles_column}`) <> '' "
    )
    params: list[object] = []
    if drug_number:
        query += f"AND `{drug_number_column}` = %s "
        params.append(drug_number)
    query += f"ORDER BY `{drug_number_column}` "
    if offset:
        if limit is None:
            query += "LIMIT %s, 18446744073709551615"
            params.append(offset)
        else:
            query += "LIMIT %s, %s"
            params.extend([offset, limit])
    elif limit is not None:
        query += "LIMIT %s"
        params.append(limit)

    cursor = connection.cursor()
    try:
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
    finally:
        cursor.close()
    return [(str(row[0]).strip(), str(row[1]).strip()) for row in rows if str(row[0]).strip() and str(row[1]).strip()]


def fetch_indications_for_drug(connection: MySQLConnection, drug_number: str) -> list[str]:
    cursor = connection.cursor()
    try:
        cursor.execute(
            """
            SELECT DISTINCT `In_dication`
            FROM `indication`
            WHERE `Drug_Number` = %s
              AND `In_dication` IS NOT NULL
              AND TRIM(`In_dication`) <> ''
            ORDER BY `In_dication`
            """,
            (drug_number,),
        )
        return [str(row[0]).strip() for row in cursor.fetchall() if str(row[0]).strip()]
    finally:
        cursor.close()


def delete_existing_rows(connection: MySQLConnection, drug_number: str | None, version: str | None = None) -> int:
    query = f"DELETE FROM `{TARGET_TABLE}` WHERE 1=1"
    params: list[object] = []
    if drug_number:
        query += " AND `drug_number` = %s"
        params.append(drug_number)
    if version:
        query += " AND `version` = %s"
        params.append(version)
    cursor = connection.cursor()
    try:
        cursor.execute(query, tuple(params))
        deleted = cursor.rowcount
        connection.commit()
        return deleted
    finally:
        cursor.close()


def build_db_rows(
    *,
    drug_number: str,
    smiles: str,
    indications: Sequence[str],
    drug_feature_fragments_by_indication: dict[str, list[str]],
    config: SplitConfig,
    version: str,
) -> list[DBFragmentRow]:
    result = split_smiles_semantically(
        smiles,
        indications=indications,
        drug_feature_fragments_by_indication=drug_feature_fragments_by_indication,
        config=config,
    )
    indication_text = ",".join(sorted(set(indications)))
    rows: list[DBFragmentRow] = []
    for fragment in result["fragments"]:
        rows.append(
            DBFragmentRow(
                drug_number=drug_number,
                indications=indication_text,
                input_smiles=result["input_smiles"],
                canonical_smiles=result["canonical_smiles"],
                fragment_smiles=str(fragment["fragment_smiles"]),
                fragment_type=str(fragment["fragment_type"]),
                semantic_type=str(fragment["semantic_type"]),
                atom_indices_json=json.dumps(fragment["atom_indices"], ensure_ascii=False),
                source=str(fragment["source"]),
                protected=int(bool(fragment["protected"])),
                stop_decompose=int(bool(fragment["stop_decompose"])),
                stop_reason=str(fragment["stop_reason"]),
                amino_acid_name=str(fragment["amino_acid_name"]),
                is_complete_residue=int(bool(fragment["is_complete_residue"])),
                is_side_chain=int(bool(fragment["is_side_chain"])),
                side_chain_smiles=str(fragment["side_chain_smiles"]),
                side_chain_class=str(fragment["side_chain_class"]),
                heavy_atoms=int(fragment["heavy_atoms"]),
                rings=int(fragment["rings"]),
                aromatic_rings=int(fragment["aromatic_rings"]),
                has_benzene=int(bool(fragment["has_benzene"])),
                priority=int(fragment["priority"]),
                fragment_key=build_fragment_key(
                    result["canonical_smiles"],
                    str(fragment["fragment_smiles"]),
                ),
                version=version,
            )
        )
    deduped_rows: OrderedDict[str, DBFragmentRow] = OrderedDict()
    for row in rows:
        deduped_rows.setdefault(row.fragment_key, row)
    return list(deduped_rows.values())


def insert_rows(connection: MySQLConnection, rows: Sequence[DBFragmentRow], insert_batch_size: int) -> int:
    if not rows:
        return 0
    query = f"""
    INSERT INTO `{TARGET_TABLE}` (
      `drug_number`, `indications`, `input_smiles`, `canonical_smiles`, `fragment_smiles`,
      `fragment_type`, `semantic_type`, `atom_indices_json`, `source`, `protected`,
      `stop_decompose`, `stop_reason`, `amino_acid_name`, `is_complete_residue`, `is_side_chain`,
      `side_chain_smiles`, `side_chain_class`, `heavy_atoms`, `rings`, `aromatic_rings`,
      `has_benzene`, `priority`, `fragment_key`, `version`
    ) VALUES (
      %s, %s, %s, %s, %s,
      %s, %s, %s, %s, %s,
      %s, %s, %s, %s, %s,
      %s, %s, %s, %s, %s,
      %s, %s, %s, %s
    )
    ON DUPLICATE KEY UPDATE
      `indications` = VALUES(`indications`),
      `input_smiles` = VALUES(`input_smiles`),
      `canonical_smiles` = VALUES(`canonical_smiles`),
      `fragment_smiles` = VALUES(`fragment_smiles`),
      `fragment_type` = VALUES(`fragment_type`),
      `semantic_type` = VALUES(`semantic_type`),
      `atom_indices_json` = VALUES(`atom_indices_json`),
      `source` = VALUES(`source`),
      `protected` = VALUES(`protected`),
      `stop_decompose` = VALUES(`stop_decompose`),
      `stop_reason` = VALUES(`stop_reason`),
      `amino_acid_name` = VALUES(`amino_acid_name`),
      `is_complete_residue` = VALUES(`is_complete_residue`),
      `is_side_chain` = VALUES(`is_side_chain`),
      `side_chain_smiles` = VALUES(`side_chain_smiles`),
      `side_chain_class` = VALUES(`side_chain_class`),
      `heavy_atoms` = VALUES(`heavy_atoms`),
      `rings` = VALUES(`rings`),
      `aromatic_rings` = VALUES(`aromatic_rings`),
      `has_benzene` = VALUES(`has_benzene`),
      `priority` = VALUES(`priority`)
    """
    inserted = 0
    for start in range(0, len(rows), insert_batch_size):
        batch = rows[start : start + insert_batch_size]
        cursor = connection.cursor()
        try:
            cursor.executemany(
                query,
                [
                    (
                        row.drug_number,
                        row.indications,
                        row.input_smiles,
                        row.canonical_smiles,
                        row.fragment_smiles,
                        row.fragment_type,
                        row.semantic_type,
                        row.atom_indices_json,
                        row.source,
                        row.protected,
                        row.stop_decompose,
                        row.stop_reason,
                        row.amino_acid_name,
                        row.is_complete_residue,
                        row.is_side_chain,
                        row.side_chain_smiles,
                        row.side_chain_class,
                        row.heavy_atoms,
                        row.rings,
                        row.aromatic_rings,
                        row.has_benzene,
                        row.priority,
                        row.fragment_key,
                        row.version,
                    )
                    for row in batch
                ],
            )
            inserted += cursor.rowcount
            connection.commit()
        finally:
            cursor.close()
    return inserted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Semantic-first SMILES splitter with fixed default rules and MySQL output. "
            "Recommended default: enable path(3-10) and keep sliding(3-10) disabled unless explicitly needed."
        )
    )
    parser.add_argument("--smiles", help="Split a single SMILES.")
    parser.add_argument("--drug-number", help="Selected drug_number for DB mode or for single-row insert metadata.")
    parser.add_argument("--indication", action="append", default=[], help="Optional indication for single-SMILES mode. Repeatable.")
    parser.add_argument("--output", help="Write single-SMILES JSON result to file.")
    parser.add_argument("--indent", type=int, default=2, help="JSON indent.")
    parser.add_argument("--min-path-atoms", type=int, default=3, help="Minimum atoms for path fragments.")
    parser.add_argument("--max-path-atoms", type=int, default=10, help="Maximum atoms for path fragments.")
    parser.add_argument("--enable-sliding", action="store_true", help="Enable sliding connected-subgraph fragments. Default remains disabled.")
    parser.add_argument("--min-sliding-atoms", type=int, default=3, help="Minimum atoms for sliding fragments.")
    parser.add_argument("--max-sliding-atoms", type=int, default=10, help="Maximum atoms for sliding fragments.")
    parser.add_argument("--path-min-heavy-atoms", type=int, default=3, help="Filter out path fragments with fewer heavy atoms.")
    parser.add_argument("--path-require-carbon", action="store_true", default=True, help="Require at least one carbon atom in path fragments.")
    parser.add_argument("--write-db", action="store_true", help=f"Insert results into `{TARGET_TABLE}`.")
    parser.add_argument("--replace-existing", action="store_true", help="Delete existing rows before insert.")
    parser.add_argument("--limit", type=int, help="Only process the first N source rows in DB mode.")
    parser.add_argument("--offset", type=int, default=0, help="Skip the first N source rows in DB mode.")
    parser.add_argument("--insert-batch-size", type=int, default=INSERT_BATCH_SIZE, help="Rows per SQL batch.")
    parser.add_argument("--version", default=DEFAULT_VERSION, help="Version tag written to DB.")
    parser.add_argument("--host", default=DEFAULT_DB_CONFIG["host"], help="MySQL host.")
    parser.add_argument("--port", type=int, default=DEFAULT_DB_CONFIG["port"], help="MySQL port.")
    parser.add_argument("--user", default=DEFAULT_DB_CONFIG["user"], help="MySQL user.")
    parser.add_argument("--password", default=DEFAULT_DB_CONFIG["password"], help="MySQL password.")
    parser.add_argument("--database", default=DEFAULT_DB_CONFIG["database"], help="MySQL database.")
    return parser.parse_args()


def build_db_config(args: argparse.Namespace) -> dict[str, object]:
    return {
        **DEFAULT_DB_CONFIG,
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "password": args.password,
        "database": args.database,
    }


def run_single(
    args: argparse.Namespace,
    drug_feature_fragments_by_indication: dict[str, list[str]],
) -> dict[str, object]:
    config = SplitConfig(
        min_path_atoms=args.min_path_atoms,
        max_path_atoms=args.max_path_atoms,
        min_sliding_atoms=args.min_sliding_atoms,
        max_sliding_atoms=args.max_sliding_atoms,
        enable_sliding=args.enable_sliding,
        path_min_heavy_atoms=args.path_min_heavy_atoms,
        path_require_carbon=args.path_require_carbon,
    )
    result = split_smiles_semantically(
        args.smiles,
        indications=args.indication,
        drug_feature_fragments_by_indication=drug_feature_fragments_by_indication,
        config=config,
    )

    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=args.indent), encoding="utf-8")

    if args.write_db:
        db_config = build_db_config(args)
        rows = build_db_rows(
            drug_number=args.drug_number or "single_input",
            smiles=args.smiles,
            indications=args.indication,
            drug_feature_fragments_by_indication=drug_feature_fragments_by_indication,
            config=config,
            version=args.version,
        )
        with connect(db_config) as connection:
            create_table_if_needed(connection)
            ensure_table_schema(connection)
            if args.replace_existing:
                delete_existing_rows(connection, args.drug_number or "single_input", args.version)
            insert_rows(connection, rows, args.insert_batch_size)
        result["db_inserted_rows"] = len(rows)
    return result


def run_batch(args: argparse.Namespace) -> dict[str, object]:
    db_config = build_db_config(args)
    config = SplitConfig(
        min_path_atoms=args.min_path_atoms,
        max_path_atoms=args.max_path_atoms,
        min_sliding_atoms=args.min_sliding_atoms,
        max_sliding_atoms=args.max_sliding_atoms,
        enable_sliding=args.enable_sliding,
        path_min_heavy_atoms=args.path_min_heavy_atoms,
        path_require_carbon=args.path_require_carbon,
    )
    with connect(db_config) as connection:
        create_table_if_needed(connection)
        ensure_table_schema(connection)
        drug_feature_fragments_by_indication = load_drug_feature_fragments_by_indication(connection)
        source_rows = fetch_source_rows(connection, args.drug_number, args.offset, args.limit)
        if args.replace_existing:
            delete_existing_rows(connection, args.drug_number, args.version)

    if not source_rows:
        raise ValueError("No source SMILES rows found in `drug_ingredient_smiles`.")

    processed_drugs = 0
    inserted_rows = 0
    prepared_rows = 0
    indication_summary: dict[str, dict[str, int]] = {}
    semantic_type_summary: dict[str, int] = {}

    for index, (drug_number, smiles) in enumerate(source_rows, start=1):
        with connect(db_config) as connection:
            indications = fetch_indications_for_drug(connection, drug_number)
            rows = build_db_rows(
                drug_number=drug_number,
                smiles=smiles,
                indications=indications,
                drug_feature_fragments_by_indication=drug_feature_fragments_by_indication,
                config=config,
                version=args.version,
            )
            current_inserted = insert_rows(connection, rows, args.insert_batch_size)
        processed_drugs += 1
        prepared_rows += len(rows)
        inserted_rows += current_inserted

        semantic_counts: dict[str, int] = {}
        for row in rows:
            semantic_counts[row.semantic_type] = semantic_counts.get(row.semantic_type, 0) + 1
            semantic_type_summary[row.semantic_type] = semantic_type_summary.get(row.semantic_type, 0) + 1

        indication_keys = indications or ["(no_indication)"]
        for indication in indication_keys:
            indication_entry = indication_summary.setdefault(
                indication,
                {
                    "drug_count": 0,
                    "prepared_rows": 0,
                    "inserted_rows": 0,
                    "amino_acid_residue": 0,
                    "amino_acid_side_chain": 0,
                    "drug_feature_rule": 0,
                    "murcko_scaffold": 0,
                    "ring": 0,
                    "functional_group": 0,
                    "brics": 0,
                    "path": 0,
                    "sliding": 0,
                    "connected_component": 0,
                },
            )
            indication_entry["drug_count"] += 1
            indication_entry["prepared_rows"] += len(rows)
            indication_entry["inserted_rows"] += current_inserted
            for semantic_type, count in semantic_counts.items():
                indication_entry[semantic_type] = indication_entry.get(semantic_type, 0) + count

        print(
            f"[{index}/{len(source_rows)}] drug_number={drug_number} "
            f"indications={len(indications)} prepared_rows={len(rows)} inserted_rows={current_inserted}"
        )

    return {
        "target_table": TARGET_TABLE,
        "selected_drug_number": args.drug_number or "ALL",
        "source_row_count": len(source_rows),
        "processed_drugs": processed_drugs,
        "prepared_rows": prepared_rows,
        "inserted_rows": inserted_rows,
        "version": args.version,
        "semantic_type_summary": semantic_type_summary,
        "indication_summary": indication_summary,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    drug_feature_fragments_by_indication: dict[str, list[str]] = {}
    if args.smiles:
        db_config = build_db_config(args)
        with connect(db_config) as connection:
            drug_feature_fragments_by_indication = load_drug_feature_fragments_by_indication(connection)

    if args.smiles:
        result = run_single(
            args,
            drug_feature_fragments_by_indication,
        )
        print(json.dumps(result, ensure_ascii=False, indent=args.indent))
        return

    if not args.write_db:
        raise ValueError("Batch mode requires `--write-db` or provide `--smiles` for single mode.")

    summary = run_batch(args)
    print(json.dumps(summary, ensure_ascii=False, indent=args.indent))


if __name__ == "__main__":
    main()
