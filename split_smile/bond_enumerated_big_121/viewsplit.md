# 查看已完成数量：

```bash
find /mnt/datadisk/drug_fragment/build_002 -name _SUCCESS | wc -l
```

 ## 如果想按片段数从大到小排：
    find /mnt/datadisk/drug_fragment/build_002 -name _SUCCESS -print0 \
  | while IFS= read -r -d '' f; do
      mid=$(dirname "$f")
      mid=${mid##*molecule_id=}
      n=$(python -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fp:
        print(json.load(fp).get("emitted_fragment_count", 0))
except Exception:
    print(0)
' "$f")
      printf "%s\t%s\n" "$mid" "$n"
    done \
  | sort -k2,2nr

## 如果想按分子id从小到大排：
  find /mnt/datadisk/drug_fragment/build_002 -name _SUCCESS \
  | sed 's#.*/molecule_id=##; s#/_SUCCESS##' \
  | sort -n

# 汇总单个分子的片段数
```
python -c "import json; p='/mnt/datadisk/drug_fragment/build_002/molecule_id=950/_SUCCESS'; d=json.load(open(p)); print(d['molecule_id'], d['emitted_fragment_count'])"
```
# 汇总所有已完成分子的片段数和总片段数：
python - <<'PY'
import json
from pathlib import Path

base = Path('/mnt/datadisk/drug_fragment/build_002')
total = 0
count = 0

for success in sorted(base.glob('molecule_id=*/_SUCCESS')):
    d = json.load(open(success, encoding='utf-8'))
    mid = d.get('molecule_id', success.parent.name.replace('molecule_id=', ''))
    n = int(d.get('emitted_fragment_count', 0))
    print(f'{mid}\t{n}')
    total += n
    count += 1

print(f'TOTAL_MOLECULES\t{count}')
print(f'TOTAL_FRAGMENTS\t{total}')
PY



## 查看某个分子的片段

python - <<'PY'
from pathlib import Path
import pyarrow.parquet as pq

build_dir = Path("/mnt/datadisk/drug_fragment/build_002")
molecule_id = "1076"
mol_dir = build_dir / f"molecule_id={molecule_id}"

if not mol_dir.exists():
    raise SystemExit(f"目录不存在: {mol_dir}")

### 优先统计最终汇总文件，避免和 shard 文件重复计算
files = sorted(mol_dir.glob("chunk_*.parquet"))
file_type = "chunk"

### 如果没有最终 chunk 文件，才统计 shard 输出
if not files:
    files = sorted(mol_dir.glob("shard-*.chunk-*.parquet"))
    file_type = "shard"

total_rows = 0
bad_files = []

for path in files:
    try:
        total_rows += pq.ParquetFile(path).metadata.num_rows
    except Exception as exc:
        bad_files.append((path, str(exc)))

print(f"molecule_id       : {molecule_id}")
print(f"directory         : {mol_dir}")
print(f"success           : {(mol_dir / '_SUCCESS').exists()}")
print(f"file_type         : {file_type}")
print(f"parquet_file_count: {len(files)}")
print(f"fragment_count    : {total_rows}")
print(f"bad_file_count    : {len(bad_files)}")

for path, error in bad_files:
    print(f"BAD {path}: {error}")
PY

# 单个分子的耗时
```
python -c "import json; p='/mnt/datadisk/drug_fragment/build_002/molecule_id=950/_SUCCESS'; d=json.load(open(p)); print(d['molecule_id'], d.get('wall_elapsed_seconds', d.get('worker_elapsed_seconds_sum')))"

python - <<'PY'
import re
from datetime import datetime

log = "/mnt/datadisk/split_smile/v3/fragment_run.log"
pat = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}).*molecule_id=950\b")

times = []
with open(log, "r", errors="ignore") as f:
    for line in f:
        m = pat.search(line)
        if m:
            times.append(datetime.strptime(m.group(1)+"."+m.group(2), "%Y-%m-%d %H:%M:%S.%f"))

print("matched lines:", len(times))
print("start:", times[0])
print("end:", times[-1])
delta = times[-1] - times[0]
print("wall time:", delta)
print("hours:", delta.total_seconds()/3600)
PY
```

# 所有已完成分子的耗时
python - <<'PY'
import re
from collections import defaultdict
from datetime import datetime

log = "/mnt/datadisk/split_smile/v3/fragment_run.log"

pat = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}).*molecule_id=([^\s]+)'
)

stats = {}

with open(log, "r", errors="ignore") as f:
    for line in f:
        m = pat.search(line)
        if not m:
            continue

        ts = datetime.strptime(
            m.group(1) + "." + m.group(2),
            "%Y-%m-%d %H:%M:%S.%f"
        )

        mol_id = m.group(3)

        if mol_id not in stats:
            stats[mol_id] = {
                "start": ts,
                "end": ts,
                "count": 1,
            }
        else:
            stats[mol_id]["end"] = ts
            stats[mol_id]["count"] += 1

rows = []

for mol_id, s in stats.items():
    hours = (s["end"] - s["start"]).total_seconds() / 3600

    rows.append(
        (
            hours,
            mol_id,
            s["start"],
            s["end"],
            s["count"],
        )
    )

rows.sort(reverse=True)

print("molecule_id,hours,start,end,matched_lines")

for hours, mol_id, start, end, count in rows:
    print(
        f"{mol_id},{hours:.3f},{start},{end},{count}"
    )
PY