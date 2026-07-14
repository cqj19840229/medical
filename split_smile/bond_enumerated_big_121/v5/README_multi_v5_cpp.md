# split_smile v5 C++/Python 加速版

v5 保留 v4 的全局调度器、输出目录布局、lock 文件、shard done 标记、中断恢复语义和 Parquet 写入路径。生产路径唯一变化是 fragment 枚举热点：C++ 负责 compressed unit state BFS、`close_state`、`visited`/`queued`、root shard 归属、frontier 扩展，以及 atom/bond index 提取。Python 仍然负责 RDKit 解析、canonical SMILES 生成、哈希和 Parquet 写入。

默认启用 `canonical_smiles`。正常生产运行使用 `--with-smiles --key-mode state`，此时 `fragment_hash256` 基于 `canonical_smiles` 计算。

## 文件

```text
fast_fragment_core.cpp
build_fast_fragment_core.sh
fast_fragment_bridge.py
fragment_multi_v5_cpp.py
run_multi_v5_cpp_nohup.sh
README_multi_v5_cpp.md
```

## 编译

在当前 v5 目录中，使用项目固定的 medical 环境编译 CPython 扩展：

```bash
cd /mnt/c/medical/github/medical/split_smile/bond_enumerated_big_121/v5
PYTHON_BIN=C:/Users/56884/anaconda3/envs/medical/python.exe ./build_fast_fragment_core.sh
```

如果在目标 Linux EPYC 机器上运行，请把 `PYTHON_BIN` 设置为那台机器上的 medical 环境解释器，然后执行同一个脚本：

```bash
PYTHON_BIN=/absolute/path/to/medical/python ./build_fast_fragment_core.sh
```

例如 Miniconda 安装在 `/opt/miniconda3` 时：

```bash
PYTHON_BIN=/opt/miniconda3/envs/medical/bin/python ./build_fast_fragment_core.sh
```

Linux 的 Conda 环境解释器位于 `bin/python`，不要使用 Windows 专用的 `python.exe`。

不需要 `pybind11`。扩展使用 CPython C API。编译产物和操作系统、Python ABI 强绑定，所以必须在实际运行任务的同类环境中编译。

## 验证

基础 Python 枚举器验证：

```powershell
& 'C:\Users\56884\anaconda3\envs\medical\python.exe' 'C:\medical\github\medical\split_smile\bond_enumerated_big_121\v5\fragment_multi_v5_cpp.py' --validate-small
```

编译 C++ 扩展后，对 benzene、naphthalene、cyclohexane、phenethylamine 比较 Python 枚举和 C++ bridge 的 canonical SMILES 集合是否一致：

```powershell
& 'C:\Users\56884\anaconda3\envs\medical\python.exe' 'C:\medical\github\medical\split_smile\bond_enumerated_big_121\v5\fragment_multi_v5_cpp.py' --validate-fast-core --cpp-batch-size 8192
```

用一个很小的 CSV 强制 C++ 模式，确认扩展确实被使用：

```powershell
& 'C:\Users\56884\anaconda3\envs\medical\python.exe' 'C:\medical\github\medical\split_smile\bond_enumerated_big_121\v5\fragment_multi_v5_cpp.py' --input tiny.csv --out-dir out_tiny_cpp --total-workers 4 --active-molecules 2 --shards 32 --fast-core cpp --with-smiles --dedupe none --key-mode state
```

需要做 A/B 耗时对比或回退验证时，使用 `--fast-core python` 强制走 v4 的 Python 枚举路径。

## 生产运行

针对 16 cores / 32 threads / 62 GB RAM / 8 GB swap 的推荐起步参数：

```bash
cd /mnt/datadisk/split_smile/v5
PYTHON_BIN=/opt/miniconda3/envs/medical/bin/python \
TOTAL_WORKERS=4 \
ACTIVE_MOLECULES=1 \
SHARDS=4096 \
BATCH_SIZE=25000 \
MAX_PENDING_TASKS=8 \
CHECKSUM=0 \
COMPRESSION=zstd \
COMPRESSION_LEVEL=1 \
FAST_CORE=auto \
CPP_BATCH_SIZE=1024 \
./run_multi_v5_cpp_nohup.sh
```

命令解读
shards
每个 molecule 被拆成多少个 shard 目标。越大，单个 shard 更小，单 worker 内存压力通常更低，恢复粒度也更细，但 shard 文件/调度开销更多。未完成 molecule 已有 _SHARD_PLAN.json 后不要改，否则恢复会报 shard plan 不一致。要改只能 --force 重建
BATCH_SIZE
每个 worker 累积多少 fragment 记录后写一次 Parquet。越大，写文件次数少、吞吐好，但内存更高。OOM 时优先降低它，比如 100000 -> 50000 -> 25000。

CPP_BATCH_SIZE
C++ 枚举器每次批量返回给 Python 的 fragment 数。越大，C++/Python 调用开销低，但一次性返回的 atom/bond 列表和 Python 处理缓存更占内存。OOM 或 RDKit 异常定位时降低，比如 8192 -> 2048 -> 1024。

FAST_CORE
选择枚举核心：

auto：有 C++ 扩展就用 C++，没有就自动回退 Python。
cpp：强制用 C++，扩展缺失直接报错。生产推荐这个，避免悄悄退回慢速 Python。
python：强制用旧 Python 枚举，适合 A/B 验证或排查 C++ bridge 问题。
MAX_PENDING_TASKS
全局进程池中最多同时排队/运行的 shard future 数。越大，CPU 更容易吃满，但 manager proxy、任务对象和待处理 worker 更多，内存和调度压力更高。OOM 后建议从 64 降到 12 或 8。

COMPRESSION
Parquet 压缩算法：

zstd：压缩率好，通常推荐。
snappy：速度快，压缩率弱一些。
none：不压缩，文件大，I/O 压力高，一般不建议。
COMPRESSION_LEVEL
压缩等级。对 zstd 有意义。1 是轻压缩，速度快、CPU 占用低，适合大规模输出。等级越高文件可能更小，但 CPU 更重。snappy 和 none 基本不用这个值。

如果希望扩展缺失时立即失败，使用 `FAST_CORE=cpp`。如果希望强制使用 v4 Python 枚举路径做验证或计时，使用 `FAST_CORE=python`。

查看内存占用前20
watch -n 60 'date; free -h; ps -C python -o pid,rss,cmd --sort=-rss | head -20'

## 输出和恢复

输出布局继续兼容 v4：

```text
OUT_DIR/molecule_id=<molecule_id>/
  _SHARD_PLAN.json
  _RUNNING.lock
  shard-000.done
  shard-000.chunk-000001.parquet
  chunk_000001.parquet
  _SUCCESS
  shard-000.error.txt
```

恢复规则：

1. molecule 已有 `_SUCCESS` 时跳过整个分子。
2. shard 已有 `shard-xxx.done` 时跳过该 shard。
3. shard 没有 done 标记但有残留 shard 输出时，删除该 shard 残留并重跑。
4. `_SHARD_PLAN.json` 会锁定未完成 molecule 的 shard plan。
5. 未完成 molecule 不要改变 `SHARDS`，除非使用 `--force` 从头重建。

`--force` 会先获取 molecule lock，然后再清理旧输出。

## 注意事项

`--no-smiles` 仍然保留，但它不会生成 canonical SMILES，因此不适合做跨分子的化学结构去重。它只适合用于状态数量统计或调度性能检查。

Canonical SMILES 仍然是主要耗时点，因为每个输出 fragment 仍然会调用 RDKit `MolFragmentToSmiles(canonical=True, isomericSmiles=True)`。C++ 层主要减少 Python BFS、set、bitmask 的开销，让更多 CPU 时间用于 RDKit 和 Parquet 写入。

## OOM 后参数建议

如果日志中出现 `A process in the process pool was terminated abruptly`，通常是 worker 被系统 OOM killer 杀掉。优先使用下面的内存稳态参数恢复：

```bash
PYTHON_BIN=/opt/miniconda3/envs/medical/bin/python \
TOTAL_WORKERS=4 \
ACTIVE_MOLECULES=1 \
SHARDS=4096 \
BATCH_SIZE=25000 \
MAX_PENDING_TASKS=8 \
CHECKSUM=0 \
COMPRESSION=zstd \
COMPRESSION_LEVEL=1 \
FAST_CORE=auto \
CPP_BATCH_SIZE=1024 \
./run_multi_v5_cpp_nohup.sh
```

已经开始但未完成的 molecule 会被 `_SHARD_PLAN.json` 锁定 shard plan；要恢复 molecule_id=1049 这类未完成任务，保持 `SHARDS=2048` 不变即可。已完成 shard 会跳过，未完成 shard 的残留会清理后重跑。

如果仍然 OOM，再降到 `TOTAL_WORKERS=3 MAX_PENDING_TASKS=6 BATCH_SIZE=20000 CPP_BATCH_SIZE=512`。如果希望改成 `SHARDS=4096` 或 `SHARDS=8192` 来降低单 shard 状态集内存，必须使用 `--force` 或清理该 molecule 输出后从头重建，不能对未完成 molecule 直接改 `SHARDS`。

`TOTAL_WORKERS=14 ACTIVE_MOLECULES=2 BATCH_SIZE=100000 MAX_PENDING_TASKS=64 CPP_BATCH_SIZE=8192` 这一类高并发参数在大 molecule 上已经实测触发过 Linux OOM kill，不建议作为默认或常规恢复参数。

## RDKit double-bond stereo fallback

少数带 `/`、`\` 双键立体标记的大分子 fragment 可能触发 RDKit `Canon.cpp` 的 `neither end atom traversed` pre-condition。v5 bridge 会先保持 `isomericSmiles=True` 生成 canonical SMILES；如果遇到该 RDKit 异常，则清理该 fragment 周边的双键 stereo/direction 后重试；如果仍失败，最后降级为 `isomericSmiles=False` 的 canonical SMILES，保证 shard 不会因为单个不完整立体上下文中断。

服务器替换 `fast_fragment_bridge.py` 后，可以用下面命令确认正在加载的是目标文件：

```bash
/root/anaconda3/envs/medical/bin/python - <<'PY'
import fast_fragment_bridge
print(fast_fragment_bridge.__file__)
PY
```

## Watchdog 守护启动

如果希望高内存运行但避免系统 OOM killer 随机杀进程，使用 watchdog。它会直接启动 `fragment_multi_v5_cpp.py`，监控整个进程组 RSS、MemAvailable 和 swap；达到阈值时先 `TERM`，等待释放，超时再 `KILL`，然后自动重启。恢复仍依赖 `_SUCCESS` / `shard-xxx.done`，已完成 shard 会跳过。

推荐启动：

```bash
cd /mnt/datadisk/split_smile/v5
chmod +x run_multi_v5_cpp_watchdog.sh
TOTAL_WORKERS=14 \
ACTIVE_MOLECULES=2 \
SHARDS=2048 \
BATCH_SIZE=50000 \
MAX_PENDING_TASKS=56 \
FAST_CORE=cpp \
CPP_BATCH_SIZE=4096 \
RSS_MAX_GB=56 \
MEM_AVAILABLE_MIN_GB=6 \
SWAP_USED_MAX_GB=3 \
CHECK_INTERVAL_SECONDS=20 \
nohup ./run_multi_v5_cpp_watchdog.sh > /mnt/datadisk/drug_fragment/build_005/watchdog.nohup.log 2>&1 &
```

关键阈值：

- `RSS_MAX_GB=56`：当前任务进程组 RSS 超过该值时主动重启。
- `MEM_AVAILABLE_MIN_GB=6`：系统可用内存低于该值时主动重启。
- `SWAP_USED_MAX_GB=3`：swap 使用超过该值时主动重启。
- `CHECK_INTERVAL_SECONDS=20`：监控间隔。
- `GRACE_SECONDS=300`：TERM 后等待清理的时间。
- `RESTART_DELAY_SECONDS=120`：重启前等待系统释放资源的时间。

查看守护日志：

```bash
tail -f /mnt/datadisk/drug_fragment/build_005/fragment_multi_v5_cpp_watchdog.log
```

停止 watchdog 和任务：

```bash
pkill -TERM -f run_multi_v5_cpp_watchdog.sh
if [[ -f /mnt/datadisk/drug_fragment/build_005/fragment_multi_v5_cpp.pid ]]; then
  kill -TERM "-$(cat /mnt/datadisk/drug_fragment/build_005/fragment_multi_v5_cpp.pid)"
fi
```

find /mnt/datadisk/drug_fragment/build_002 -name _SUCCESS | wc -l

find /data/drug_fragment/build_005 -name _SUCCESS | wc -l

 find /mnt/datadisk/drug_fragment/build_005 -name _SUCCESS -print0 \
  | while IFS= read -r -d '' f; do
      mid=$(dirname "$f")
      mid=${mid##*molecule_id=}
      n=$(jq -r '.emitted_fragment_count // 0' "$f")
      echo -e "${mid}\t${n}"
    done \
  | sort -k2,2nr

  find /data/drug_fragment/build_005 -name _SUCCESS -print0 \
  | while IFS= read -r -d '' f; do
      mid=$(dirname "$f")
      mid=${mid##*molecule_id=}
      n=$(jq -r '.emitted_fragment_count // 0' "$f")
      echo -e "${mid}\t${n}"
    done \
  | sort -k2,2nr

## Tail active molecules

For large machines, tail-active-molecules can open extra active molecules only when the
currently active set is near shard tail. It keeps the same global
`ProcessPoolExecutor(max_workers=TOTAL_WORKERS)` and does not change output schema,
shard plans, shard done markers, `_SUCCESS`, recovery, canonical SMILES, or Parquet
writing. Existing behavior is unchanged unless `TAIL_ACTIVE_MOLECULES` is greater than
`ACTIVE_MOLECULES`.

Recommended 64 CPU / 512 GB example:

```bash
INPUT_FILE=/data/split_smile/molecule_big_now.csv \
OUT_DIR=/data/drug_fragment/build_005 \
TOTAL_WORKERS=56 \
ACTIVE_MOLECULES=4 \
TAIL_ACTIVE_MOLECULES=7 \
TAIL_TRIGGER_SHARDS_FACTOR=2 \
TAIL_MOLECULE_SHARDS=32 \
TAIL_MIN_TAIL_MOLECULES=1 \
TAIL_MIN_NONTAIL_MOLECULES=0 \
TAIL_RSS_SOFT_MAX_GB=320 \
TAIL_MEM_AVAILABLE_MIN_GB=160 \
SHARDS=8192 \
MAX_PENDING_TASKS=768 \
BATCH_SIZE=50000 \
CPP_BATCH_SIZE=2048 \
MEM_AVAILABLE_MIN_GB=96 \
RSS_MAX_GB=380 \
SWAP_USED_MAX_GB=1 \
./start.sh
```

##  查看没有残留进程：
ps -eo pid,ppid,pgid,etimes,%cpu,rss,args \
| egrep 'run_multi_v5_cpp_watchdog|fragment_multi_v5_cpp.py|watchdog.singleton.lock' \
| grep -v grep || true
