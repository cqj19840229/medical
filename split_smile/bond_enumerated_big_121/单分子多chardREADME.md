# split_smile v3

性能优先版本：先把芳香 connected component 压缩成 protected unit，再用 `int` bitmask 枚举 unit state。普通非芳香 heavy bond 仍然作为枚举 unit，非芳香环仍可拆分；命中任意芳香系统 atom/bond 时会闭合加入整个芳香系统。

## 推荐运行：单分子多进程，保留 SMILES

```powershell
C:\ProgramData\anaconda3\envs\rdkit_env\python C:\medical\github\medical\split_smile\bond_enumerated_big_121\bond_enumerated_non_induced_fragments.py `
  --smiles "CCOc1ccc2nc(S(N)(=O)=O)sc2c1" `
  --molecule-id mol_001 `
  --out-dir /mnt/datadisk/drug_fragment/build_002 `
  --min-atoms 3 `
  --workers 16 `
  --shards 256 `
  --batch-size 200000 `
  --compression zstd `
  --compression-level 1 `
  --dedupe none `
  --key-mode state
```

`--workers` 表示同时运行的进程数，`--shards` 表示单个分子内部切成多少个细粒度小任务。16 Core / 32 Thread + 62 GB 内存机器建议先用 `workers=16, shards=256, batch-size=200000`；稳定后再压测 `workers=20~24`。每个分子一个目录，例如 `build_002/molecule_id=646/`。完成后输出 `chunk_000001.parquet` 和 `_SUCCESS`；只有显式打开 `--checksum` 才会额外输出 `.checksum`。

## 最快模式：不生成 fragment SMILES

如果拆分阶段只需要 state 级输出，后续再做统计、抽样或重新生成 SMILES，可以打开 `--no-smiles` 跳过每个 fragment 的 `MolFragmentToSmiles`。这是当前代码里最明显的加速开关，但 `canonical_smiles` 会为空，`fragment_hash256` 基于 state key，不适合作为跨分子的化学结构去重键。

```powershell
C:\ProgramData\anaconda3\envs\rdkit_env\python C:\medical\github\medical\split_smile\bond_enumerated_big_121\bond_enumerated_non_induced_fragments.py `
  --input C:\medical\github\medical\split_smile\bond_enumerated_big_121\molecule_big.csv `
  --smiles-col smiles `
  --id-col molecule_id `
  --out-dir /mnt/datadisk/drug_fragment/build_002 `
  --workers 16 `
  --shards 256 `
  --batch-size 200000 `
  --dedupe none `
  --key-mode state `
  --no-smiles
```

## CSV 顺序任务表

```powershell
C:\ProgramData\anaconda3\envs\rdkit_env\python C:\medical\github\medical\split_smile\bond_enumerated_big_121\bond_enumerated_non_induced_fragments.py `
  --input C:\medical\github\medical\split_smile\bond_enumerated_big_121\molecule_big.csv `
  --smiles-col smiles `
  --id-col molecule_id `
  --out-dir /mnt/datadisk/drug_fragment/build_002 `
  --workers 16 `
  --shards 256 `
  --batch-size 200000 `
  --dedupe none `
  --key-mode state
```

CSV 只是任务清单。脚本会按行顺序处理：一个 molecule 的所有 shard 完成并写 `_SUCCESS` 后，才会读取并处理下一个 molecule。这样适合大分子批处理，不会同时启动多个 7000w 级分子的拆分任务。当前默认表头是 `molecule_id,smiles`。

## Linux nohup 运行
16 Core / 32 Thread AMD EPYC + 62 GB Memory + 8 GB Swap。

先编辑或用环境变量覆盖 [run_nohup.sh](C:/medical/github/medical/split_smile/bond_enumerated_big_121/run_nohup.sh) 顶部配置，至少设置 Linux 上的 RDKit Python：

```bash
cd /path/to/medical/split_smile/bond_enumerated_big_121
chmod +x run_nohup.sh

PYTHON_BIN=/path/to/rdkit_env/bin/python \
INPUT_FILE=/path/to/molecule_big.csv \
WORKERS=16 \
SHARDS=256 \
BATCH_SIZE=200000 \
NO_SMILES=1 \
LOG_FILE=/mnt/datadisk/drug_fragment/build_002/fragment_run.log \
./run_nohup.sh
```

查看日志：`tail -f /mnt/datadisk/drug_fragment/build_002/fragment_run.log`。

## 恢复运行

当前是 shard 级恢复，不是单个 shard 内部的队列级断点恢复。启动命令不需要变，重新运行 `run_nohup.sh` 即可。

- 如果某个 molecule 目录里已经有 `_SUCCESS`，重新运行会直接跳过。
- 如果 molecule 没有 `_SUCCESS`，但部分 shard 已经有 `shard-000.done`，重新运行会跳过这些完成的 shard，继续跑未完成 shard。
- 如果某个 shard 有 `shard-000.chunk-*.parquet`、tmp、checksum、error 或 sqlite 残留，但没有 `shard-000.done`，说明这个 shard 上次中途失败；重新运行时会自动删除这个 shard 的残留文件，然后重跑该 shard。
- 如果已经进入最终整理阶段，存在 `chunk_*.parquet`，重新运行会保留已有 chunk，并继续整理剩余 shard part，最后写 `_SUCCESS`。
- 每个未完成 molecule 会写 `_SHARD_PLAN.json` 锁定切分计划。未完成 molecule 重启时 `--shards` 不能改变；可以调小 `--workers` 来降低并发压力。

查看已完成数量：

```bash
find /mnt/datadisk/drug_fragment/build_002 -name _SUCCESS | wc -l
```

查看已完成的 molecule_id：

```bash
find /mnt/datadisk/drug_fragment/build_002 -name _SUCCESS \
  | sed 's#.*/molecule_id=##; s#/_SUCCESS##' \
  | sort -n
```

导出单个分子到csv
```
python /mnt/datadisk/split_smile/v3/export_molecule_parquet_to_csv.py \
  /mnt/datadisk/drug_fragment/build_001/molecule_id=1 \
  -o /mnt/datadisk/split_smile/v3/molecule1.csv \
  --batch-size 100000 \
  --overwrite

```

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

## Validation

```powershell
C:\ProgramData\anaconda3\envs\rdkit_env\python C:\medical\github\medical\split_smile\bond_enumerated_big_121\bond_enumerated_non_induced_fragments.py --validate-small
```

该命令比较苯、萘、非芳香环、芳香取代基小分子的旧算法与新算法 canonical fragment set。非芳香环应仍可拆分，芳香环不应产生半环 fragment。

## 单分子 Parquet 导出 CSV

导出某个 `molecule_id=...` 目录下已经整理好的 `chunk_*.parquet`：

```bash
python /path/to/medical/split_smile/bond_enumerated_big_121/export_molecule_parquet_to_csv.py \
  /mnt/datadisk/drug_fragment/build_002/molecule_id=937 \
  -o /mnt/datadisk/drug_fragment/build_002/molecule_id=937.csv \
  --batch-size 100000 \
  --overwrite
```

只导出部分字段可以减少 CSV 体积：

```bash
python /path/to/medical/split_smile/bond_enumerated_big_121/export_molecule_parquet_to_csv.py \
  /mnt/datadisk/drug_fragment/build_002/molecule_id=937 \
  -o /mnt/datadisk/drug_fragment/build_002/molecule_id=937.keys.csv \
  --columns molecule_id,fragment_key,fragment_hash256,canonical_smiles
```

## 速度、内存、去重语义

- `--dedupe none --key-mode state`：最快，按 closed state 输出，不做 canonical SMILES 去重，适合一次性大任务后续全局去重。
- `--no-smiles`：最快的 state-only 模式，跳过 RDKit `MolFragmentToSmiles`；`canonical_smiles` 为空，`fragment_hash256` 不再表示 canonical 化学结构，只适合不需要本阶段输出 SMILES 的流程。
- `--key-mode hash`：需要生成 canonical SMILES 并用 hash 做 key，速度慢于 state。
- `--workers > 1`：同一个 SMILES 内部多进程拆分；要求 `--dedupe none`，跨 shard 的 canonical/global 去重建议后处理。
- `--shards`：单分子内部细分任务数，默认约为 `workers * 8`。设大一些可提升 CPU 利用率并减少最后只剩少数进程运行的长尾；未完成 molecule 恢复时必须保持不变。
- `--dedupe memory`：仅单进程内 canonical 去重，适合小分子或 validation，不适合 6000 万级别常驻内存。
- `--dedupe sqlite`：仅单进程内磁盘去重，内存稳定但明显更慢。
- 输出始终保留 `canonical_smiles` 和 `fragment_hash256`，拆分阶段建议 `--dedupe none --key-mode state`，后续用 DuckDB/Polars/Spark 按 hash/smiles 离线去重。
- `--batch-size`：越大写入吞吐通常越好，但内存更高；多进程时每个 worker 都会攒一批，62 GB 内存建议从 `200000` 开始，保留 SMILES 时不要一开始就开到 `1000000`。
- `--debug-fields`：写 atom/bond/protected unit 等 JSON 字段，会增加 CPU、IO 和文件体积。
- `--checksum`：每个 part 写完后计算 sha256，默认关闭以节省时间。
- `--workers`：同时运行的 worker 进程数。16 Core / 32 Thread 机器可先试 `WORKERS=16 SHARDS=256 BATCH_SIZE=200000`，稳定后再试 `WORKERS=20~24 SHARDS=320~384`。
