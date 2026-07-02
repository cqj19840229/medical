你现在在C:\medical\github\medical\split_smile\bond_enumerated_big_121\v5 目录工作。请基于现有 v4 多分子并发版本，新增一个 v5 C++/Python 加速版，不要破坏原有 v4 文件。

现有关键文件在C:\medical\github\medical\split_smile\bond_enumerated_big_121下：
- fragment_multi_v4.py
- run_multi_v4_nohup.sh
- README_multi_v4.md
- bond_enumerated_non_induced_fragments.py
- parquet_writer.py

目标：
用 C++ 加速 fragment 枚举热路径，但保留 Python/RDKit 生成 canonical_smiles。canonical_smiles 必须默认开启，不能默认关闭。输出 schema、文件命名、恢复逻辑、日志风格尽量保持 v4 不变。

请新增或确认存在这些文件：
- fast_fragment_core.cpp
- build_fast_fragment_core.sh
- fast_fragment_bridge.py
- fragment_multi_v5_cpp.py
- run_multi_v5_cpp_nohup.sh
- README_multi_v5_cpp.md

核心要求：
1. 不要修改 fragment_multi_v4.py 的行为；v5 用新文件 fragment_multi_v5_cpp.py。
2. v5 保留 v4 的全局调度架构：
   - 一个全局 ProcessPoolExecutor(max_workers=TOTAL_WORKERS)
   - 多个 active molecule 的 shard 进入同一个全局 pending queue
   - 不允许 molecule pool -> shard pool 的两层进程池
3. 保留 v4 输出目录和文件命名：
   OUT_DIR/molecule_id=<molecule_id>/
   _SHARD_PLAN.json
   _RUNNING.lock
   shard-000.done
   shard-000.chunk-000001.parquet
   chunk_000001.parquet
   _SUCCESS
   shard-000.error.txt
4. 保留中断恢复语义：
   - molecule 有 _SUCCESS 就跳过
   - shard 有 shard-xxx.done 就跳过
   - shard 没有 done 但有残留 parquet/tmp/checksum/sqlite/error 就删除该 shard 残留并重跑
   - _SHARD_PLAN.json 锁定 unfinished molecule 的 shard plan
   - --force 必须先拿 molecule lock，再清理旧输出
5. Parquet 继续走 parquet_writer.write_chunk_parquet，继续用 columnar buffer，不要改成 list[dict] 大缓存。
6. 默认必须：
   - --with-smiles
   - canonical_smiles_enabled=true
   - fragment_hash256 基于 canonical_smiles
   - CHECKSUM=0
   - dedupe none
   - key-mode state
7. 可以保留 --no-smiles，但 README 必须说明 no-smiles 不适合跨分子化学结构去重。
8. 不要引入复杂框架。C++ 扩展用 CPython C API 或 pybind11 都可以，但优先 CPython C API，避免额外依赖。
9. C++ 只负责：
   - compressed unit state BFS
   - close_state
   - visited / queued
   - root_unit / root_bucket shard ownership
   - frontier expansion
   - atom_indices / bond_indices 批量返回
   Python 仍负责：
   - RDKit parse_smiles
   - Chem.MolFragmentToSmiles canonical=True isomericSmiles=True
   - fragment_hash256
   - Parquet 写入
10. 新增 CLI：
   - --fast-core {auto,cpp,python}，默认 auto
   - --cpp-batch-size，默认 8192
   auto 表示 C++ 扩展可用就用，否则 fallback 到 Python 枚举。
   cpp 表示必须用 C++，扩展不可用就报错。
   python 表示强制使用原 Python 枚举，便于 A/B 测试。
11. run_multi_v5_cpp_nohup.sh 默认：
   PYTHON_BIN="${PYTHON_BIN:-python}"
   INPUT_FILE="${INPUT_FILE:-${SCRIPT_DIR}/molecule_big_after991.csv}"
   OUT_DIR="${OUT_DIR:-/mnt/datadisk/drug_fragment/build_005_cpp}"
   LOG_FILE="${LOG_FILE:-${OUT_DIR}/fragment_multi_v5_cpp.log}"
   TOTAL_WORKERS="${TOTAL_WORKERS:-14}"
   ACTIVE_MOLECULES="${ACTIVE_MOLECULES:-2}"
   SHARDS="${SHARDS:-2048}"
   BATCH_SIZE="${BATCH_SIZE:-100000}"
   MAX_PENDING_TASKS="${MAX_PENDING_TASKS:-64}"
   CHECKSUM="${CHECKSUM:-0}"
   COMPRESSION="${COMPRESSION:-zstd}"
   COMPRESSION_LEVEL="${COMPRESSION_LEVEL:-1}"
   FAST_CORE="${FAST_CORE:-auto}"
   CPP_BATCH_SIZE="${CPP_BATCH_SIZE:-8192}"
   wrapper 默认传 --with-smiles。
12. README_multi_v5_cpp.md 要写清楚：
   - 先运行 ./build_fast_fragment_core.sh
   - 推荐运行命令
   - 如何用 FAST_CORE=cpp 强制检查 C++ 扩展
   - 如何用 FAST_CORE=python 回退验证
   - canonical_smiles 仍是主要成本，因为每个 fragment 仍调用 RDKit MolFragmentToSmiles
   - 中断后重新执行同一命令即可恢复
   - 未完成 molecule 不要改变 SHARDS
13. 验证：
   - python fragment_multi_v5_cpp.py --validate-small 必须通过
   - 写一个小 CSV，至少包含 benzene 和 cyclohexane，用 --fast-core cpp 跑一个临时 out-dir，确认不报错
   - 对 benzene / naphthalene / cyclohexane / phenethylamine 比较 Python 枚举和 C++ bridge 的 canonical_smiles 集合一致
14. 最后输出：
   - 修改/新增的文件列表
   - build 命令
   - validate 命令
   - 推荐 nohup 启动命令
   - 如有无法完成的地方，明确说明，不要假装完成

请现在执行实现、编译检查和基本验证。