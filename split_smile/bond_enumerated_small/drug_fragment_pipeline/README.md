# Drug Fragment Pipeline

## 项目目标

本项目把药物 `SMILES` 按现有 RDKit 片段算法拆成 canonical fragment，流式生成、按 molecule 并行、分 chunk 写 Parquet，并把 Parquet 数据集作为最终构建产物。算法类型保持为：

`bond_enumerated_non_induced_aromatic_protected`

## 总体架构

流程是：

```text
SMILES CSV
-> RDKit fragment generator
-> per-molecule Parquet chunks
-> chunk checksum
-> per-molecule _SUCCESS
-> validate_build
-> Parquet dataset
```

每个 molecule 写入独立目录，避免多个进程争用同一批文件。

## 为什么不边拆分边写数据库

拆分阶段是 CPU、内存和文件 IO 都敏感的离线计算。直接写数据库会把计算、网络、批量提交、失败恢复和幂等处理绑在一起，单个 molecule 失败后很难判断哪些 fragment 已经完整落地。先产出文件可以让失败边界清晰，每个 molecule 只看 `_SUCCESS` 即可判断是否完成。

## 为什么先写 Parquet

Parquet 是列式、压缩友好、可被多种分析引擎直接读取的格式，也适合断点恢复和校验。每个 chunk 有独立 checksum，每个 molecule 有 `_SUCCESS`，构建是否完整可以被 `validate_build` 快速验证。

## 为什么按 molecule 并行

molecule 之间天然独立，一个 worker 只处理一个 molecule，并写入自己的 `molecule_id=...` 目录。这样失败不会影响其他 molecule，恢复时已经完成的 molecule 会被跳过。

## 为什么 MAX_WORKERS 默认 16 而不是 32

RDKit 片段枚举会消耗大量 CPU 和内存。默认 16 更接近物理核心并发，给系统缓存、文件写入和压缩保留余量，避免一开始就触发 swap 或 IO 抖动。稳定后可以再逐步上调。

## 为什么设置线程环境变量

入口会设置：

```bash
OMP_NUM_THREADS=1
MKL_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
NUMEXPR_NUM_THREADS=1
ARROW_NUM_THREADS=1
```

这样每个进程内部不会再开很多计算线程，避免 `ProcessPoolExecutor` worker 和底层库线程互相放大。

## 启动拆分

Linux 默认输出目录：

`/mnt/datadisk/drug_fragment`

Windows 开发默认输出目录：

`C:\medical\github\medical\split_smile\v2\drug_fragment`

Linux 示例：

```bash
cd /path/to/split_smile/v2
export DRUG_FRAGMENT_MAX_WORKERS=16
export DRUG_FRAGMENT_CHUNK_ROWS=200000
export DRUG_FRAGMENT_GC_COLLECT_EVERY_N_CHUNKS=10
python -m drug_fragment_pipeline.build_runner --input molecules.csv
```

Windows 固定环境示例：

```powershell
cd C:\medical\github\medical\split_smile\v2
$env:DRUG_FRAGMENT_MAX_WORKERS="16"
$env:DRUG_FRAGMENT_CHUNK_ROWS="200000"
C:\ProgramData\anaconda3\envs\rdkit_env\python -m drug_fragment_pipeline.build_runner --input molecules.csv
```

## 中断恢复

有 `_SUCCESS` 的 molecule 会跳过。目录存在但没有 `_SUCCESS` 的 molecule 会删除目录后重跑。残留 `.tmp` 文件会在重跑前清理，Parquet chunk 写入使用 `.tmp` 完成后 atomic rename。

## 验证

```bash
python -m drug_fragment_pipeline.validate_build \
  --input molecules.csv \
  --build-dir /mnt/datadisk/drug_fragment/build_001
```

Windows 示例：

```powershell
C:\ProgramData\anaconda3\envs\rdkit_env\python -m drug_fragment_pipeline.validate_build --input molecules.csv --build-dir C:\medical\github\medical\split_smile\v2\drug_fragment\build_001
```

## 调参

如果 CPU 没跑满、iowait 不高、swap 不增长，把 `DRUG_FRAGMENT_MAX_WORKERS` 从 16 调到 20，再试 24。

如果 swap 增长，把 `DRUG_FRAGMENT_MAX_WORKERS` 降到 12。

如果小文件太多，把 `DRUG_FRAGMENT_CHUNK_ROWS` 从 200000 调到 300000 或 500000。

如果内存压力大，把 `DRUG_FRAGMENT_CHUNK_ROWS` 降到 100000。

如果希望减少每条 fragment 的二次 RDKit canonicalize 临时对象，并接受直接信任 `MolFragmentToSmiles(... canonical=True ...)` 的结果，可以设置：

```bash
export DRUG_FRAGMENT_TRUST_FRAGMENT_CANONICAL=true
```

默认不启用该开关，保持严格二次 canonicalize。`DRUG_FRAGMENT_GC_COLLECT_EVERY_N_CHUNKS` 默认 10，设为 0 可关闭主动 GC。

程序每个 molecule 会输出 `fragments_per_second`，用于比较不同参数配置。

## Parquet 输出目录结构

```text
{DRUG_FRAGMENT_BASE_DIR}/
  build_001/
    build_summary.json
    failed_molecules.json
    molecule_id=1/
      chunk_000001.parquet
      chunk_000001.checksum
      _SUCCESS
    molecule_id=2/
      chunk_000001.parquet
      chunk_000002.parquet
      chunk_000001.checksum
      chunk_000002.checksum
      _SUCCESS
```

## Parquet 字段

默认字段：

```text
molecule_id: uint16
fragment_key: string
fragment_hash256: string
canonical_smiles: string
atom_count: uint16
bond_count: uint16
```

`DRUG_FRAGMENT_DEBUG_FIELDS=true` 时追加：

```text
fragment_smiles: string
atom_indices_json: string
bond_indices_json: string
protected_units_hit_json: string
non_chon_hetero_atoms_json: string
```

## 下游消费 Parquet

DuckDB：

```sql
SELECT molecule_id, canonical_smiles
FROM read_parquet('/mnt/datadisk/drug_fragment/build_001/molecule_id=*/*.parquet')
LIMIT 10;
```

Spark 可以按目录读取整个 build：

```python
df = spark.read.parquet("/mnt/datadisk/drug_fragment/build_001/molecule_id=*/*.parquet")
```

PyArrow：

```python
import pyarrow.dataset as ds

dataset = ds.dataset("/mnt/datadisk/drug_fragment/build_001", format="parquet")
table = dataset.to_table()
```

Polars：

```python
import polars as pl

df = pl.scan_parquet("/mnt/datadisk/drug_fragment/build_001/molecule_id=*/*.parquet")
```

## 后续版本管理

如果后续需要版本管理，可以在 Parquet 路径或字段中增加 `build_id` 或 `release_id`。

## 查看已经完成数量
find /mnt/datadisk/drug_fragment/build_001 -name _SUCCESS | wc -l
find /mnt/datadisk/drug_fragment/build_002 -name _SUCCESS | wc -l
## 查看已经完成的 molecule_id

find /mnt/datadisk/drug_fragment/build_001 -name _SUCCESS \
  | sed 's#.*/molecule_id=##; s#/_SUCCESS##' \
  | sort -n

find /mnt/datadisk/drug_fragment/build_002 -name _SUCCESS \
  | sed 's#.*/molecule_id=##; s#/_SUCCESS##' \
  | sort -n

  find /mnt/datadisk/drug_fragment/build_001 -name _SUCCESS \
  | sed 's#.*/molecule_id=##; s#/_SUCCESS##' \
  | sort -n \
  | grep -x 1083

  ###
  find /mnt/datadisk/drug_fragment/build_002 -name _SUCCESS -print0 \
  | while IFS= read -r -d '' f; do
      mid=$(dirname "$f")
      mid=${mid##*molecule_id=}
      n=$(jq -r '.emitted_fragment_count // 0' "$f")
      echo -e "${mid}\t${n}"
    done \
  | sort -n
### 如果想按片段数从大到小排：
{
  printf "molecule_id\tfragment_rows\tchunk_count\tstatus\n"
  find /mnt/datadisk/drug_fragment/build_001 -name _SUCCESS -print0 \
    | while IFS= read -r -d '' f; do
        mid=$(basename "$(dirname "$f")")
        mid=${mid#molecule_id=}

        n=$(jq -r '(.fragment_rows // .emitted_fragment_count // 0) | tonumber' "$f")
        chunks=$(jq -r '(.chunk_count // 0) | tonumber' "$f")
        status=$(jq -r '.status // "unknown"' "$f")

        printf "%s\t%s\t%s\t%s\n" "$mid" "$n" "$chunks" "$status"
      done \
    | sort -t $'\t' -k2,2nr
}
### 如果想按片段数从大到小排(前10)：
find /mnt/datadisk/drug_fragment/build_001 -name _SUCCESS -print0 \
  | while IFS= read -r -d '' f; do
      mid=$(basename "$(dirname "$f")")
      mid=${mid#molecule_id=}
      n=$(jq -r '(.fragment_rows // .emitted_fragment_count // 0) | tonumber' "$f")
      printf "%s\t%s\n" "$mid" "$n"
    done \
  | sort -t $'\t' -k2,2nr \
  | head