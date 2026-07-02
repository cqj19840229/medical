你现在要基于现有两个 Python 文件做一次性能优先的改造：

1. 一个文件是 parquet_writer.py 存储逻辑，当前使用 PyArrow 写 Parquet，并带 checksum。
2. 一个文件是 bond_enumerated_non_induced_fragments.py 拆分算法，当前按 heavy bond 枚举 fragment，对芳香系统做保护：芳香环不拆，其他环仍然允许拆分。

业务背景：

* 现在要拆分的都是大分子。
* 单个分子大约会产生 6000 万多甚至更多级别 fragment。
* 这批分子拆分是一次性离线任务，不是长期在线服务。
* 当前机器配置下目标是尽快完成，而不是追求最优雅架构。
* 当前算法是 bond 枚举；芳香环不拆，其他非芳香环仍然参与拆分。
* 希望对芳香系统做预压缩，减少枚举维度。
* 输出仍然以 Parquet 为主，便于后续处理。

请完成以下改造。

核心目标：

* 把芳香系统预先压缩成 super-unit。
* 枚举时不要把芳香系统内部 aromatic bonds 当作普通枚举维度。
* 一旦 fragment 命中某个芳香系统的任意 atom/bond，就自动闭合加入整个芳香系统。
* 非芳香 bond 仍然作为普通枚举单元。
* 非芳香环仍然允许拆分。
* 保持原始业务语义：芳香系统保护，非诱导 fragment，heavy atom fragment。
* 优先性能、内存稳定性和可恢复性。
* 日志体现当前进度，

请重点优化这些点：

1. 枚举状态表示

当前代码大量使用 set/frozenset 表示 bond 状态，并且频繁 normalize。请改成 int bitmask 或其他更轻量的状态表示。

要求：

* 每个枚举 unit 分配一个连续 unit index。
* 芳香系统作为一个 unit。
* 非芳香 heavy bond 作为一个 unit。
* 状态用 int bitmask 表示。
* visited / queued 也用 int 保存。
* 预计算每个 unit 对应的 atom mask、bond mask、邻接 unit mask、闭包 mask。

2. 芳香系统预压缩

实现 build_compressed_units(mol)：

* 先识别 aromatic systems。
* 每个 aromatic connected component 变成一个 protected aromatic unit。
* aromatic unit 包含该系统所有 aromatic heavy atoms 和 aromatic bonds。
* 非芳香 heavy bond 作为 ordinary bond unit。
* 如果 ordinary bond 连接到芳香系统，则它与对应 aromatic unit 邻接。
* 如果 ordinary bond 连接两个芳香系统，则与两个 aromatic units 邻接。
* aromatic system 内部 bonds 不进入 ordinary bond units。
* 需要保留 protected_units_hit 信息，至少在 debug 模式下可输出。

3. 状态闭合

实现 close_state(state_mask)：

* 如果状态中包含某个 aromatic unit，则已经闭合。
* 如果状态中普通 bond 的 atom 接触某个 aromatic system，也要把对应 aromatic unit 加入 state。
* 闭合过程应基于预计算的 closure mask，不能每次扫描所有 aromatic systems。
* close_state 应尽可能 O(命中 unit 数) 或 bit 操作完成。

4. 枚举逻辑

替换当前 enumerate_bond_fragments_non_induced 的核心实现。

要求：

* 初始 seed 为每个 ordinary bond unit 和 aromatic unit。
* 每次从 queue 取 state。
* 先 close_state。
* 用 state 计算 atom_count / bond_count。
* 超过 max_atoms 直接跳过扩展。
* 满足 min_atoms..max_atoms 时生成 fragment。
* frontier 通过预计算 unit_adjacency_mask 得到。
* 新状态 = state | next_unit_bit，然后 close_state 后入队。
* visited 状态按 closed state 去重。
* 支持 limit 或 limit_states，方便压测。

5. SMILES 生成延后与减少

当前每个候选 fragment 都调用 MolFragmentToSmiles，成本很高。

要求：

* 先用 state / atom mask / bond mask 过滤 min_atoms / max_atoms。
* 只有满足输出条件时再调用 MolFragmentToSmiles。
* key-mode 支持：

  * state：最快，用状态作为 fragment_key，不做 canonical fragment 去重。
  * hash：生成 canonical_smiles 后计算 hash，用于后续去重。
* dedupe 支持：

  * none：最快，不按 SMILES 去重，只按状态去重。
  * memory：进程内 set 去重 canonical_smiles/hash。
  * sqlite：磁盘去重，适合 6000 万级别，牺牲速度换内存稳定。
* 默认建议 dedupe=none，key-mode=state，因为这是一次性任务，后面可以再全局去重。

6. 输出改成流式 Parquet

不要把所有 fragments 放进 list 里最后统一排序写出。

要求：

* 支持 batch_size，例如 100000、500000、1000000。
* 每攒够一批就写一个 Parquet part 文件。
* 文件名包含 molecule_id 和 part 序号。
* 每个 part 先写 .tmp，完成后 rename。
* 支持 resume：如果 part 文件已存在，可以跳过或报错，由参数控制。
* 默认不写 CSV/JSON。
* 默认不写 debug 字段。
* debug 字段通过 --debug-fields 显式打开。
* checksum 默认关闭，通过 --checksum 显式打开。
* compression 默认 zstd，compression_level 默认 1；也支持 snappy。

7. 并行策略

这是大分子一次性任务，优先按分子并行，而不是单个分子内部复杂并行。

要求：

* CLI 支持输入 CSV/TSV，指定 smiles_col 和 id_col。
* 支持 --workers。
* 每个 worker 处理一个分子。
* 每个分子的输出放到 /mnt/datadisk/drug_fragment/build_002 下。
* 每个分子完成后写 done marker。
* 失败时写 error 文件，包含 molecule_id、SMILES、异常堆栈。
* 如果 done marker 存在，默认跳过，支持 --force 重跑。

8. 字段设计

Parquet 默认字段：

* molecule_id
* fragment_key
* fragment_hash256
* canonical_smiles
* atom_count
* bond_count

debug 模式额外字段：

* fragment_smiles
* atom_indices_json
* bond_indices_json
* protected_units_hit_json
* non_chon_hetero_atoms_json

说明：

* canonical_smiles 可以等于 fragment_smiles。
* fragment_hash256 对 canonical_smiles 或 fragment_key 做 sha256。
* 在 key-mode=state 且 dedupe=none 时，可以允许 canonical_smiles 仍然写出，但 fragment_key 使用 state key。
* 如果为了速度增加 --no-smiles 模式，则 canonical_smiles 可以为空，但必须明确在 CLI help 中说明该模式只适合只需要结构状态、不需要 SMILES 的场景。

9. CLI 参数

请提供完整 argparse CLI，至少包括：

* --input
* --smiles-col
* --id-col
* --out-dir
* --min-atoms
* --max-atoms
* --workers
* --batch-size
* --compression
* --compression-level
* --dedupe {none,memory,sqlite}
* --key-mode {state,hash}
* --debug-fields
* --checksum
* --resume / --force
* --limit-states
* --limit-fragments

10. 兼容性验证

请保留一个小规模 validation 函数或测试脚本，用于比较旧算法和新算法在小分子上的输出。

要求：

* 对苯、萘、非芳香环、含芳香取代基的小分子做测试。
* 在 dedupe=memory 且 key-mode=hash 时，新算法输出的 canonical fragment set 应与旧算法一致，或者如果存在差异，要解释差异是否来自 state 去重、芳香闭包或非诱导定义。
* 对非芳香环，要确认仍然可以被拆分。
* 对芳香环，要确认不会产生半个芳香环 fragment。

11. 性能压测输出

运行时请打印或记录：

* molecule_id
* heavy_atom_count
* aromatic_system_count
* unit_count
* ordinary_bond_unit_count
* visited_state_count
* emitted_fragment_count
* written_part_count
* elapsed_seconds
* fragments_per_second
* peak_memory_mb，如果容易实现

12. 代码风格

* Python 3.11+。
* 使用 RDKit。
* 使用 PyArrow 写 Parquet。
* 不要引入复杂框架。
* 代码尽量单文件可运行，便于直接丢到机器上跑。
* 关键函数写清楚注释。
* 对 6000 万 fragment 级别，不允许全量 fragments 常驻内存。
* 默认配置要偏向最快完成一次性任务。

请最终交付：

1. 一个优化后的主脚本。
2. 一个最小 README，说明推荐运行命令。
3. 一个小规模 validation 命令。
4. 说明哪些参数影响速度、内存和去重语义。
