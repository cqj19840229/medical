# 查看哪些成功了
python - <<'PY'
from pathlib import Path
import json

out = Path("/data/drug_fragment/build_005")

rows = []
for success in out.glob("molecule_id=*/_SUCCESS"):
    mol = success.parent.name.split("molecule_id=", 1)[-1]
    try:
        data = json.loads(success.read_text(encoding="utf-8"))
    except Exception as e:
        rows.append((mol, -1, f"bad_json: {e}"))
        continue

    n = data.get("emitted_fragment_count", 0)
    rows.append((mol, int(n or 0), ""))

rows.sort(key=lambda x: x[1], reverse=True)

for mol, n, err in rows:
    if err:
        print(f"{mol}\t{n}\t{err}")
    else:
        print(f"{mol}\t{n}")
PY

# 完成时间、耗时、chunk 数：
python - <<'PY'
from pathlib import Path
import json

out = Path("/data/drug_fragment/build_005")

rows = []
for success in out.glob("molecule_id=*/_SUCCESS"):
    mol = success.parent.name.split("molecule_id=", 1)[-1]
    try:
        d = json.loads(success.read_text(encoding="utf-8"))
    except Exception as e:
        rows.append((mol, -1, "", "", "", f"bad_json: {e}"))
        continue

    rows.append((
        mol,
        int(d.get("emitted_fragment_count") or 0),
        d.get("wall_hours", ""),
        d.get("chunk_count", ""),
        d.get("finished_at", ""),
        "",
    ))

rows.sort(key=lambda x: x[1], reverse=True)

print("molecule_id\temitted_fragment_count\twall_hours\tchunk_count\tfinished_at")
for mol, n, hours, chunks, finished, err in rows:
    if err:
        print(f"{mol}\t{n}\t\t\t\t{err}")
    else:
        print(f"{mol}\t{n}\t{hours}\t{chunks}\t{finished}")
PY

# 看还剩多少个shard

python - <<'PY'
from pathlib import Path
import json

mol = "1093"
d = Path(f"/data/drug_fragment/build_005/molecule_id={mol}")

plan = json.loads((d / "_SHARD_PLAN.json").read_text())
shard_count = int(plan["shard_count"])

done = set()
for p in d.glob("shard-*.done"):
    try:
        done.add(int(p.name.split("-")[1].split(".")[0]))
    except Exception:
        pass

missing = [i for i in range(shard_count) if i not in done]

print("molecule_id =", mol)
print("shard_count =", shard_count)
print("done_shards =", len(done))
print("missing_count =", len(missing))
print("first_missing_50 =", missing[:50])
print("last_missing_50 =", missing[-50:])
PY
##  tail-active 查看
python - <<'PY'
from pathlib import Path
import json
import os

out = Path("/data/drug_fragment/build_005")
total_missing = 0
alive_mols = 0

def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False

for lock in sorted(out.glob("molecule_id=*/_RUNNING.lock")):
    d = lock.parent
    payload = json.loads(lock.read_text())
    pid = int(payload.get("pid") or 0)
    if not alive(pid):
        continue

    plan = d / "_SHARD_PLAN.json"
    j = json.loads(plan.read_text())
    done = len(list(d.glob("shard-*.done")))
    missing = int(j["shard_count"]) - done
    total_missing += missing
    alive_mols += 1
    print(f"{d.name}: missing={missing}")

print(f"alive_molecules={alive_mols} total_missing={total_missing}")
print("tail_active_trigger_threshold=", 56 * 2)
PY
