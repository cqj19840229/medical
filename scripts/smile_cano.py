from rdkit import Chem
from rdkit.Chem import rdMolDescriptors, Descriptors

smiles = "COc1ccc2[nH]c([S@@](=O)Cc3ncc(C)c(OC)c3C)nc2c1"

mol = Chem.MolFromSmiles(smiles)

if mol is None:
    print("SMILES 解析失败")
else:
    canonical_smiles = Chem.MolToSmiles(mol, canonical=True)
    formula = rdMolDescriptors.CalcMolFormula(mol)
    mol_weight = Descriptors.MolWt(mol)

    print("Canonical SMILES:", canonical_smiles)
    print("Molecular Formula:", formula)
    print("Molecular Weight:", round(mol_weight, 3))