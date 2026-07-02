你是资深 Python 数据工程师、RDKit 工程师和大规模离线数据 pipeline 工程师。

请基于当前仓库中的 RDKit SMILES fragment 拆分脚本，重构成一个高性能、可中断恢复、适合 8 亿级 fragment 规模的工程化 pipeline。

重要约束：

1. 不要使用 MySQL。
2. 不要使用 ClickHouse。
3. 不要生成 clickhouse_schema.sql。
4. 不要生成 clickhouse_import.sql。
5. 不要在 README 中写 ClickHouse 建表、导入、查询说明。
6. 不要实现 Web 后台。
7. 拆分阶段只写 Parquet。
8. Parquet 是最终构建产物。
9. 必须支持断点恢复。
10. 必须支持 checksum 校验。
11. 必须避免单个 molecule 把所有 fragment 一次性放入内存。

一、目标架构

实现如下 pipeline：

SMILES 拆分程序
→ 流式生成 fragment
→ 分 chunk 写 Parquet
→ 每个 molecule 完成后写 _SUCCESS
→ validate_build 校验完整性
→ Parquet 数据集作为最终产物

二、项目结构

请生成或重构为如下项目结构：

drug_fragment_pipeline/
  __init__.py
  config.py
  smiles_fragmenter.py
  hash_utils.py
  parquet_writer.py
  molecule_worker.py
  build_runner.py
  validate_build.py
  README.md

不要创建以下文件：

  clickhouse_schema.sql
  clickhouse_import.sql

三、默认配置

在 config.py 中提供以下默认配置，并支持环境变量覆盖：

DRUG_FRAGMENT_BUILD_ID，默认 build_001
DRUG_FRAGMENT_BASE_DIR，默认 /mnt/datadisk/drug_fragment
DRUG_FRAGMENT_INPUT，默认 molecules.csv
DRUG_FRAGMENT_MAX_WORKERS，默认 16
DRUG_FRAGMENT_CHUNK_ROWS，默认 200000
DRUG_FRAGMENT_MIN_ATOMS，默认 3
DRUG_FRAGMENT_MAX_ATOMS，默认空，表示使用分子 heavy_atom_count
DRUG_FRAGMENT_LIMIT，默认空，不启用
DRUG_FRAGMENT_PARQUET_COMPRESSION，默认 zstd
DRUG_FRAGMENT_PARQUET_COMPRESSION_LEVEL，默认 1
DRUG_FRAGMENT_DEBUG_FIELDS，默认 false

程序入口最前面必须设置：

OMP_NUM_THREADS=1
MKL_NUM_THREADS=1
OPENBLAS_NUM_THREADS=1
NUMEXPR_NUM_THREADS=1
ARROW_NUM_THREADS=1

四、输入文件格式

输入 CSV：molecules.csv

字段：

molecule_id,smiles

示例：

1,CCOc1ccc(...)
2,Nc1ncnc2...

build_runner.py 读取该 CSV，并按 molecule_id 分发任务。

要求大数据场景下不要一次性把所有 molecule 读入内存，优先流式读取 CSV。

五、算法层：smiles_fragmenter.py

当前仓库中已有 RDKit SMILES 拆分逻辑。请搜索并复用以下函数或等价逻辑：

parse_smiles
heavy_atom_indices
heavy_bond_indices
build_aromatic_systems
close_protected_aromatic_systems
build_bond_adjacency
fragment_smiles_from_atom_bond_set
_normalize_state
enumerate_bond_fragments_non_induced

现有算法必须保持不变：

1. 重键 BFS 枚举。
2. 芳香系统保护闭包。
3. 基于 selected bond set 生成非诱导片段。
4. 使用 RDKit MolFragmentToSmiles 生成 canonical fragment_smiles。
5. 使用 fragment_smiles 做去重。
6. 支持 min_atoms / max_atoms / limit 参数。
7. 当前 fragment_type 保持为：
   bond_enumerated_non_induced_aromatic_protected

请把现有 enumerate_bond_fragments_non_induced 改造成流式 generator：

iter_bond_fragments_non_induced(
    mol: Chem.Mol,
    min_atoms: int = 3,
    max_atoms: int | None = None,
    limit: int | None = None,
) -> Iterator[dict[str, object]]

要求：

1. 保持原来的 BFS bond-set 扩展逻辑。
2. 保持 aromatic_systems 保护闭包逻辑。
3. 保持 visited_states / queued_states，避免重复状态。
4. 保持 fragment_smiles 去重。
5. 不要最终 sorted。
6. 不要返回 list。
7. 每发现一个新的 unique fragment_smiles，立即 yield。
8. yield 的字段至少包括：

   fragment_smiles
   atom_count
   bond_count

9. DEBUG_FIELDS=true 时，额外 yield：

   atom_indices
   bond_indices
   protected_units_hit
   non_chon_hetero_atoms

10. 默认不要输出 atom_indices_json、bond_indices_json、protected_units_hit_json，避免 Parquet 体积过大。
11. limit 不为空时，yield 数量达到 limit 后停止。

请保留兼容函数：

enumerate_fragments(...)

它内部调用 iter_bond_fragments_non_induced 并返回 list，方便单元测试和小分子调试。

六、hash_utils.py

实现：

canonicalize_smiles(smiles: str) -> str

要求：

1. 使用 RDKit Chem.MolFromSmiles 解析。
2. 使用以下方式输出 canonical smiles：

   Chem.MolToSmiles(
       mol,
       canonical=True,
       kekuleSmiles=False,
       isomericSmiles=True,
   )

3. 解析失败抛出 ValueError。

实现：

hash_fragment(canonical_smiles: str) -> tuple[str, str]

要求：

1. fragment_key 使用 canonical_smiles 的 128-bit hash hex。
2. fragment_hash256 使用 canonical_smiles 的 256-bit hash hex。
3. 使用 hashlib.blake2b：
   digest_size=16 作为 fragment_key
   digest_size=32 作为 fragment_hash256
4. 输入必须使用 utf-8 编码。
5. 返回两个 hex 字符串。

七、Parquet 输出字段

默认 Parquet 字段：

molecule_id: uint16
fragment_key: string
fragment_hash256: string
canonical_smiles: string
atom_count: uint16
bond_count: uint16

DEBUG_FIELDS=true 时追加：

fragment_smiles: string
atom_indices_json: string
bond_indices_json: string
protected_units_hit_json: string
non_chon_hetero_atoms_json: string

八、parquet_writer.py

实现：

write_chunk_parquet(
    records: list[dict],
    output_path: Path,
    compression: str,
    compression_level: int | None,
) -> None

要求：

1. 使用 pyarrow 写 Parquet。
2. 先写临时文件：

   chunk_000001.parquet.tmp

3. 写入完成并 close 后，atomic rename 成：

   chunk_000001.parquet

4. 生成 checksum：

   chunk_000001.checksum

5. checksum 使用 sha256，内容为 parquet 文件 sha256 hex。
6. 如果写入失败，删除 .tmp 文件。
7. 不允许留下半成品正式 parquet 文件。

实现：

calculate_file_checksum(path: Path) -> str

实现：

write_checksum(path: Path) -> None

实现：

verify_checksum(path: Path) -> bool

九、molecule_worker.py

实现：

process_molecule(
    molecule_id: int,
    smiles: str,
    build_dir: Path,
    min_atoms: int,
    max_atoms: int | None,
    limit: int | None,
    chunk_rows: int,
    debug_fields: bool,
) -> dict

输出目录：

/mnt/datadisk/drug_fragment/build_001/molecule_id={molecule_id}/

处理逻辑：

1. 如果 molecule 目录存在 _SUCCESS，直接跳过，返回 status=skipped。
2. 如果 molecule 目录存在但没有 _SUCCESS，第一版直接删除整个 molecule 目录后重跑。
3. 清理 .tmp 文件。
4. 解析 smiles。
5. 使用 iter_bond_fragments_non_induced 流式产生 fragment。
6. 对每条 fragment：

   canonical_smiles = canonicalize_smiles(fragment["fragment_smiles"])
   fragment_key, fragment_hash256 = hash_fragment(canonical_smiles)

   然后生成 Parquet record。

7. 每累计 chunk_rows 条，写一个 chunk。
8. chunk 文件名：

   chunk_000001.parquet
   chunk_000002.parquet

9. 所有 fragment 完成后，写剩余 buffer。
10. 校验所有 chunk checksum。
11. 写 _SUCCESS 文件。
12. _SUCCESS 内容为 JSON，至少包括：

    molecule_id
    status
    chunk_count
    fragment_rows
    elapsed_seconds
    fragments_per_second
    min_atoms
    max_atoms

13. 如果失败，返回 status=failed 和 error_message，不影响其他 molecule。

十、build_runner.py

实现 CLI：

python -m drug_fragment_pipeline.build_runner --input molecules.csv

要求：

1. 使用 ProcessPoolExecutor。
2. 默认 max_workers=16。
3. 一个 worker 处理一个 molecule。
4. 不允许多个 worker 同时处理同一个 molecule_id。
5. 某个 molecule 失败时记录失败，其他 molecule 继续。
6. 每个 molecule 完成后通过 logging 输出：

   molecule_id
   status
   fragment_rows
   elapsed_seconds
   fragments_per_second

7. 最后输出汇总：

   total_molecules
   success_count
   skipped_count
   failed_count
   total_fragment_rows
   total_elapsed_seconds

8. 失败列表写入：

   {DRUG_FRAGMENT_BASE_DIR}/{DRUG_FRAGMENT_BUILD_ID}/failed_molecules.json

9. 汇总写入：

   {DRUG_FRAGMENT_BASE_DIR}/{DRUG_FRAGMENT_BUILD_ID}/build_summary.json

注意：

不要把路径硬编码为 /data/drug_fragment。
统一使用 config 中的 base_dir 和 build_id。

十一、validate_build.py

实现 CLI：

python -m drug_fragment_pipeline.validate_build \
  --input molecules.csv \
  --build-dir /mnt/datadisk/drug_fragment/build_001

检查：

1. expected molecule_id 是否全部存在目录。
2. 每个 molecule 是否有 _SUCCESS。
3. 是否存在 .tmp 文件。
4. 是否存在 parquet 但没有 checksum。
5. checksum 是否正确。
6. 输出：

   expected_molecule_count
   success_molecule_count
   missing_success_count
   tmp_file_count
   bad_checksum_count
   total_parquet_files
   total_parquet_size_bytes

如果所有检查通过，退出码为 0，否则退出码为 1。

十二、README.md 要求

README.md 需要包含：

1. 项目目标。
2. 总体架构。
3. 为什么不边拆分边写数据库。
4. 为什么先写 Parquet。
5. 为什么按 molecule 并行。
6. 为什么 MAX_WORKERS 默认 16 而不是 32。
7. 为什么设置 OMP_NUM_THREADS=1 等环境变量。
8. 如何启动拆分：

   export DRUG_FRAGMENT_MAX_WORKERS=16
   export DRUG_FRAGMENT_CHUNK_ROWS=200000
   python -m drug_fragment_pipeline.build_runner --input molecules.csv

9. 如何中断恢复：

   有 _SUCCESS 的 molecule 会跳过。
   没有 _SUCCESS 的 molecule 会删除目录后重跑。
   .tmp 文件会被清理。

10. 如何验证：

   python -m drug_fragment_pipeline.validate_build \
     --input molecules.csv \
     --build-dir /mnt/datadisk/drug_fragment/build_001

11. 如何调参：

   如果 CPU 没跑满、iowait 不高、swap 不增长，把 MAX_WORKERS 从 16 调到 20，再试 24。
   如果 swap 增长，把 MAX_WORKERS 降到 12。
   如果小文件太多，把 CHUNK_ROWS 从 200000 调到 300000 或 500000。
   如果内存压力大，把 CHUNK_ROWS 降到 100000。

12. Parquet 输出目录结构。
13. Parquet 字段说明。
14. 下游如何消费 Parquet，例如 DuckDB / Spark / PyArrow / Polars。
15. 后续如果需要版本管理，可以在 Parquet 路径或字段中增加 build_id 或 release_id。

README 中不要出现：

ClickHouse
fragment_dict
mol_to_frag
frag_to_mol
clickhouse_schema.sql
clickhouse_import.sql
CREATE TABLE
INSERT INTO drug_fragment

十三、性能要求

默认参数：

MAX_WORKERS = 16
CHUNK_ROWS = 200000
PARQUET_COMPRESSION = zstd
PARQUET_COMPRESSION_LEVEL = 1

调优建议：

1. 首次运行使用 16 workers。
2. 观察 htop、free -h、iostat -x 1。
3. 如果 CPU idle 高且 iowait 低，尝试 20 workers。
4. 如果仍稳定，尝试 24 workers。
5. 不建议默认 32 workers。
6. 一旦 swap 持续增长，立即降低 workers。
7. 程序每个 molecule 输出 fragments_per_second，用于比较不同参数配置。

十四、代码质量要求

1. Python 代码必须有类型标注。
2. 使用 logging，不要只用 print。
3. 大数据场景不要一次性把所有 molecule 或所有 fragment 读入内存。
4. 单 molecule 内 fragment 流式产生，分 chunk 写入。
5. 文件写入必须使用 tmp + atomic rename。
6. 错误处理要清晰。
7. 每个模块职责清楚。
8. 生成可运行的完整代码。
9. 不要引入 MySQL。
10. 不要引入 ClickHouse。
11. 不要在拆分阶段写任何数据库。
12. 优先保证正确性、可恢复性和速度。

十五、验收标准

完成后请确保：

1. 项目可以通过：

   python -m drug_fragment_pipeline.build_runner --input molecules.csv

   启动。

2. 项目可以通过：

   python -m drug_fragment_pipeline.validate_build \
     --input molecules.csv \
     --build-dir /mnt/datadisk/drug_fragment/build_001

   校验。

3. 对同一个 molecule 重复运行时，如果已有 _SUCCESS，会跳过。
4. 如果 molecule 目录没有 _SUCCESS，会删除后重跑。
5. Parquet chunk 写入使用 .tmp + atomic rename。
6. 每个 .parquet 都有对应 .checksum。
7. validate_build 能发现 checksum 缺失或错误。
8. README 不包含任何 ClickHouse 内容。
9. 仓库中不生成 clickhouse_schema.sql 和 clickhouse_import.sql。
10. fragment 枚举逻辑与原算法保持一致，只把最终 list 返回改成流式 yield。