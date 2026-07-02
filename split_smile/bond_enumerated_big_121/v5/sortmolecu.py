python - <<'PY'
from pathlib import Path
import csv, json, math

from rdkit import Chem
from rdkit.Chem import Descriptors, rdMolDescriptors

CSV_FILE = Path("/data/split_smile/molecule_big_now.csv")
OUT_DIR = Path("/data/drug_fragment/build_005")

def structural_score(mol):
    heavy = mol.GetNumHeavyAtoms()
    rot = Descriptors.NumRotatableBonds(mol)
    ring = rdMolDescriptors.CalcNumRings(mol)
    arom = rdMolDescriptors.CalcNumAromaticRings(mol)
    hetero = sum(1 for a in mol.GetAtoms() if a.GetAtomicNum() not in (1, 6))

    nonring_heavy_bonds = sum(
        1 for b in mol.GetBonds()
        if not b.IsInRing()
        and b.GetBeginAtom().GetAtomicNum() > 1
        and b.GetEndAtom().GetAtomicNum() > 1
    )

    branch = 0
    for a in mol.GetAtoms():
        if a.GetAtomicNum() <= 1:
            continue
        heavy_degree = sum(1 for nb in a.GetNeighbors() if nb.GetAtomicNum() > 1)
        branch += max(0, heavy_degree - 2)

    # 粗略耗时评分：非环重原子键、可旋转键、分支度通常更容易造成组合爆炸
    score = (
        0.23 * nonring_heavy_bonds
        + 0.13 * rot
        + 0.06 * branch
        + 0.04 * heavy
        + 0.02 * hetero
    )

    return {
        "score": score,
        "heavy_atoms": heavy,
        "nonring_heavy_bonds": nonring_heavy_bonds,
        "rotatable_bonds": rot,
        "branch_score": branch,
        "hetero_atoms": hetero,
        "rings": ring,
        "aromatic_rings": arom,
    }

def read_success(mol_id):
    p = OUT_DIR / f"molecule_id={mol_id}" / "_SUCCESS"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return {
        "wall_hours": float(d.get("wall_hours") or 0),
        "emitted_fragment_count": int(d.get("emitted_fragment_count") or 0),
        "fragments_per_wall_second": float(d.get("fragments_per_wall_second") or 0),
        "chunk_count": int(d.get("chunk_count") or 0),
    }

def shard_status(mol_id):
    d = OUT_DIR / f"molecule_id={mol_id}"
    plan = d / "_SHARD_PLAN.json"
    if not plan.exists():
        return ("not_started", "", "", "")
    try:
        j = json.loads(plan.read_text(encoding="utf-8"))
        shard_count = int(j.get("shard_count") or 0)
    except Exception:
        shard_count = 0
    done = len(list(d.glob("shard-*.done")))
    success = (d / "_SUCCESS").exists()
    running = (d / "_RUNNING.lock").exists()
    if success:
        status = "success"
    elif running:
        status = "running"
    else:
        status = "partial"
    return (status, done, shard_count, shard_count - done if shard_count else "")

rows = []
with CSV_FILE.open("r", encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        mol_id = str(r["molecule_id"])
        smiles = r["smiles"]
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            continue

        feat = structural_score(mol)
        success = read_success(mol_id)
        status, done, total, missing = shard_status(mol_id)

        rows.append({
            "molecule_id": mol_id,
            "status": status,
            "done_shards": done,
            "total_shards": total,
            "missing_shards": missing,
            **feat,
            **(success or {}),
        })

print("\n=== 已完成分子：按真实 wall_hours 从高到低排序 ===")
done_rows = [r for r in rows if r["status"] == "success"]
done_rows.sort(key=lambda x: x.get("wall_hours", 0), reverse=True)

print("rank\tmolecule_id\twall_hours\temitted_fragments\tfps\tchunk_count")
for i, r in enumerate(done_rows, 1):
    print(
        f"{i}\t{r['molecule_id']}\t"
        f"{r.get('wall_hours', 0):.3f}\t"
        f"{r.get('emitted_fragment_count', 0)}\t"
        f"{r.get('fragments_per_wall_second', 0):.0f}\t"
        f"{r.get('chunk_count', 0)}"
    )

print("\n=== 未完成/未开始分子：按结构复杂度估算耗时从高到低排序 ===")
todo_rows = [r for r in rows if r["status"] != "success"]
todo_rows.sort(key=lambda x: x["score"], reverse=True)

print("rank\tmolecule_id\tstatus\tdone/total\tmissing\tscore\theavy\tnonring_bonds\trot\tbranch\thetero\trings\tarom")
for i, r in enumerate(todo_rows, 1):
    done_total = f"{r['done_shards']}/{r['total_shards']}" if r["total_shards"] != "" else ""
    print(
        f"{i}\t{r['molecule_id']}\t{r['status']}\t{done_total}\t{r['missing_shards']}\t"
        f"{r['score']:.2f}\t{r['heavy_atoms']}\t{r['nonring_heavy_bonds']}\t"
        f"{r['rotatable_bonds']}\t{r['branch_score']}\t{r['hetero_atoms']}\t"
        f"{r['rings']}\t{r['aromatic_rings']}"
    )
PY