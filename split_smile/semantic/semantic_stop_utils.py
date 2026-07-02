from __future__ import annotations

from typing import Sequence, Tuple


HIGH_SEMANTIC_FUNCTIONAL_GROUPS = {
    "nitro",
    "nitrile",
    "amide",
    "ester",
    "carboxyl",
    "sulfonyl",
    "sulfone",
    "phosphate",
    "phosphate_like",
    "disulfide",
}


def infer_stop_decompose_unified(
    semantic_type: str,
    semantic_class: list[str],
    priority: int,
    heavy_atoms: int,
    ring_count: int,
    aromatic_ring_count: int,
    rotatable_bonds: int,
    is_complete_residue: bool = False,
    is_side_chain: bool = False,
) -> tuple[bool, str]:
    semantic_set = set(semantic_class)

    if semantic_type == "amino_acid_residue" or is_complete_residue:
        return True, "complete_amino_acid_residue"

    if semantic_type == "amino_acid_side_chain" or is_side_chain:
        return True, "recognized_amino_acid_side_chain"

    if semantic_type == "module":
        if semantic_set & {"fused_ring", "privileged_scaffold_candidate", "high_semantic_core"}:
            return True, "privileged_or_fused_module"
        if "aromatic_heterocycle" in semantic_set and priority >= 85:
            return True, "high_semantic_module"
        if "ring_system" in semantic_set and heavy_atoms >= 5 and ring_count >= 1 and priority >= 85:
            return True, "high_semantic_module"
        if heavy_atoms <= 4 and not (semantic_set & {"fused_ring", "privileged_scaffold_candidate", "high_semantic_core"}):
            return False, "small_common_module_without_distinctive_semantic_density"
        if heavy_atoms <= 5 and ring_count <= 1 and semantic_set <= {
            "amine",
            "basic_candidate",
            "hbond_acceptor",
            "hbond_donor",
            "heterocycle",
            "positive_ionizable_candidate",
            "ring_system",
            "saturated_aza_ring",
            "amide",
            "imine_like",
        }:
            return False, "small_common_module_without_distinctive_semantic_density"
        if priority >= 88 and heavy_atoms >= 5:
            return True, "high_semantic_module"
        return False, "small_common_module_without_distinctive_semantic_density"

    if semantic_type == "linker":
        if semantic_set & {"conjugated_linker", "cleavable_linker_candidate", "disulfide_linker"}:
            return True, "cleavable_or_conjugated_linker"
        if "rigid_linker" in semantic_set and heavy_atoms >= 4:
            return True, "distinctive_linker_unit"
        if "flexible_linker" in semantic_set and heavy_atoms >= 5 and rotatable_bonds >= 2:
            return True, "distinctive_linker_unit"
        if heavy_atoms <= 3 and not (semantic_set & {"cleavable_linker_candidate", "rigid_linker", "conjugated_linker", "disulfide_linker", "flexible_linker"}):
            return False, "short_low_information_linker"
        return False, "short_low_information_linker"

    if semantic_type == "functional_group":
        if semantic_set & HIGH_SEMANTIC_FUNCTIONAL_GROUPS:
            return True, "high_semantic_functional_group"
        if priority >= 45 and heavy_atoms >= 4:
            return True, "medium_complex_functional_group"
        if "small_group" in semantic_set or heavy_atoms <= 2:
            return False, "small_low_information_group"
        return False, "default_functional_group"

    if priority >= 90 and (ring_count >= 1 or aromatic_ring_count >= 1 or heavy_atoms >= 6):
        return True, "fallback_high_priority_semantic_unit"

    return False, "default_fallback_non_terminal_unit"


def semantic_class_frequency_string(values: Sequence[str], top_n: int = 10) -> str:
    counter: dict[str, int] = {}
    for value in values:
        for label in [item for item in str(value).split(";") if item]:
            counter[label] = counter.get(label, 0) + 1
    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:top_n]
    return "; ".join(f"{label}:{count}" for label, count in ranked)
