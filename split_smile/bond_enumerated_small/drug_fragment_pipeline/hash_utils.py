from __future__ import annotations

import hashlib

from rdkit import Chem
from rdkit import RDLogger


RDLogger.DisableLog("rdApp.*")


def canonicalize_smiles(smiles: str) -> str:
    mol = Chem.MolFromSmiles((smiles or "").strip())
    if mol is None:
        raise ValueError(f"SMILES cannot be parsed: {smiles}")
    return Chem.MolToSmiles(
        mol,
        canonical=True,
        kekuleSmiles=False,
        isomericSmiles=True,
    )


def canonicalize_fragment_smiles(smiles: str) -> str:
    stripped = (smiles or "").strip()
    mol = Chem.MolFromSmiles(stripped, sanitize=False)
    if mol is None:
        raise ValueError(f"Fragment SMILES cannot be parsed: {smiles}")
    return Chem.MolToSmiles(
        mol,
        canonical=True,
        kekuleSmiles=False,
        isomericSmiles=True,
    )


def hash_fragment(canonical_smiles: str) -> tuple[str, str]:
    data = canonical_smiles.encode("utf-8")
    fragment_key = hashlib.blake2b(data, digest_size=16).hexdigest()
    fragment_hash256 = hashlib.blake2b(data, digest_size=32).hexdigest()
    return fragment_key, fragment_hash256
