from rdkit import Chem

q = Chem.MolFromSmiles("CC(=O)OCCCC(C)C(=CCCCCCC(C)C(c1cccnc1)=CCC)C")
t = Chem.MolFromSmiles("CC(=O)O[C@H]1CC[C@@]2(C)C(=CC[C@@H]3[C@@H]2CC[C@]2(C)C(c4cccnc4)=CC[C@@H]32)C1")

print(q.GetNumAtoms(), q.GetNumBonds())
print(t.GetNumAtoms(), t.GetNumBonds())

print(t.HasSubstructMatch(q, useChirality=False))