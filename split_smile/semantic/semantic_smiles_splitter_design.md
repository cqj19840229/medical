# 语义优先 SMILES 拆分设计文档

## Python 依赖

执行 [semantic_smiles_splitter.py](/C:/medical/github/medical/split_smile/semantic_smiles_splitter.py) 至少需要以下 Python 包：

- `rdkit`
- `mysql-connector-python`

推荐安装命令：

```bash
pip install rdkit mysql-connector-python
```

如果使用 Conda，推荐：

```bash
conda install -c conda-forge rdkit
pip install mysql-connector-python
```

脚本额外使用的都是 Python 标准库模块，包括：

- `argparse`
- `hashlib`
- `json`
- `sys`
- `collections`
- `dataclasses`
- `pathlib`
- `typing`

## 1. 目标

本方案用于对药物 `SMILES` 做面向生产的语义优先拆分，并将结果写入 MySQL 表 `fda.smile_split`。

设计目标：

- 优先识别高语义结构，而不是直接做低语义暴力枚举
- 对氨基酸残基等高语义单元进行保护，避免被继续细拆
- 在高语义结构之外，再做通用拆分补召回
- 支持全量批跑、重复重跑、版本化入库
- 支持稳定去重和覆盖更新

当前主入口文件：

- [semantic_smiles_splitter.py](/C:/medical/github/medical/split_smile/semantic_smiles_splitter.py)

辅助依赖：

- [semantic_stop_utils.py](/C:/medical/github/medical/split_smile/semantic_stop_utils.py)

## 2. 总体设计

拆分流程采用“语义主干 + 规则增强 + 通用补召回 + 全局裁决”的方式。

总顺序：

1. SMILES 标准化
2. 高语义结构识别
3. `drug_feature_rule` 适应症原始片段直连命中
4. 固定默认配置下的通用拆分
5. 全局优先级裁决
6. 去重、覆盖更新、入库

## 3. 标准化层

标准化由 RDKit 完成，处理步骤如下：

1. `MolFromSmiles`
2. `Cleanup`
3. `FragmentParent`
4. `Uncharger`
5. `SanitizeMol`
6. 生成 `canonical_smiles`

标准化目的：

- 统一输入结构表达
- 去除盐型和碎片干扰
- 为模板命中和去重键提供稳定输入

## 4. 语义层

### 4.1 氨基酸残基识别

当前优先识别氨基酸样骨架：

- 识别 `alpha carbon + backbone N + carbonyl carbon`
- 提取残基原子集合
- BFS 提取侧链原子集合
- 尝试将侧链归类为天然氨基酸侧链模板

输出字段包括：

- `amino_acid_name`
- `is_complete_residue`
- `is_side_chain`
- `side_chain_smiles`
- `side_chain_class`

当命中完整残基时：

- `protected = true`
- `stop_decompose = true`

### 4.2 `side_chain_class` 的含义

如果 `side_chain_class = unclassified`，表示：

- 已识别出氨基酸样残基或侧链边界
- 但当前侧链模板库无法把它归入已有天然氨基酸类别

常见原因：

- 非天然氨基酸
- 被修饰的天然侧链
- 边界截取与模板不完全一致

## 5. `drug_feature_rule` 设计

当前适应症规则命中统一使用 `drug_feature_rule`：

- 直接来源于 `drug_feature` 表中的 `fragment`
- 只要该片段属于药物对应适应症，且能命中该药物子结构，就作为结果写入
- `fragment_smiles` 使用 `drug_feature.fragment` 的 canonical 形式做 exact emit
- 来源字段格式为 `drug_feature:<indication>`

这层的目标不是学习模板，而是把适应症原始片段中确实命中当前药物子结构的部分直接补入结果集。

## 6. 通用拆分默认配置

当前主流程不再依赖外部规则配置文件，所有药物统一使用固定默认拆分配置。

默认开启：

- `murcko_scaffold`
- `ring`
- `functional_group`
- `brics`
- `path`

默认关闭：

- `sliding`

默认窗口：

- `path` 原子数 `3-10`
- `sliding` 原子数 `3-10`

如果需要调整范围或开启 `sliding`，直接通过命令行参数覆盖默认值。

## 7. 通用拆分层

### 7.1 当前保留的通用类型

- `murcko_scaffold`
- `ring`
- `functional_group`
- `brics`
- `path`

### 7.2 `path` / `sliding` 策略

推荐策略：

- 默认只启用 `path`
- 默认关闭 `sliding`
- 原子窗口统一按 `3-10`

原因：

- `path(3-10)` 更稳，更容易解释，更适合入库
- `sliding(3-10)` 召回更强，但噪声和重复更高

当前实现：

- `path` 默认开启，窗口 `3-10`
- `sliding` 默认关闭，仅通过 `--enable-sliding` 显式开启

### 7.3 `path` 过滤策略

为了降低噪声，当前对 `path` 做了多层过滤：

1. 低重原子数过滤  
   `heavy_atoms < 2` 的片段直接丢弃
2. 无碳 `path` 过滤
3. 纯碳短链过滤  
   例如 `CCC`、`CCCC` 不入库
4. 芳香但不成环的 `path` 过滤
5. 局部芳香但无法形成稳定子结构的 `path` 过滤

效果：

- 明显降低 `path` 片段数量
- 降低 RDKit 的芳香和 kekulize 异常片段

## 8. 全局裁决策略

### 8.1 优先级

当前语义优先级从高到低如下：

1. `amino_acid_residue`
2. `amino_acid_side_chain`
3. `drug_feature_rule`
4. `murcko_scaffold`
5. `ring`
6. `functional_group`
7. `brics`
8. `path`
9. `sliding`
10. `connected_component`

### 8.2 当前裁决规则

当前已实现：

- 被完整氨基酸残基完整覆盖的低级碎片会被丢弃
- 同一组 `atom_indices` 只保留最高优先级解释
- 相同片段做稳定去重
- `connected_component` 仅作为兜底输出
  只有在没有其他片段时才输出

## 9. 输出结构

当前入库字段与拆分结果核心结构如下：

- `fragment_smiles`
- `fragment_type`
- `semantic_type`
- `atom_indices_json`
- `source`
- `protected`
- `stop_decompose`
- `stop_reason`
- `amino_acid_name`
- `is_complete_residue`
- `is_side_chain`
- `side_chain_smiles`
- `side_chain_class`
- `heavy_atoms`
- `rings`
- `aromatic_rings`
- `has_benzene`
- `priority`

## 10. 数据库设计

目标表：

- `fda.smile_split`

关键策略：

- 版本化字段：`version`
- 唯一键：
  `uq_smile_split_drug_version_fragment (drug_number, version, fragment_key)`
- 覆盖更新：
  使用 `ON DUPLICATE KEY UPDATE`

其中 `fragment_key` 由以下字段组合计算 `SHA1`：

- `canonical_smiles`
- `fragment_smiles`
- `fragment_type`
- `semantic_type`
- `atom_indices`
- `source`

作用：

- 支持稳定去重
- 支持同版本重跑覆盖
- 避免重复堆积数据

## 11. 运行命令

### 11.1 单条 SMILES 试跑

```bash
python split_smile\semantic_smiles_splitter.py --smiles "N[C@@H](CC1=CC=CC=C1)C(=O)O"
```

### 11.2 单条 SMILES 试跑并写库

```bash
python split_smile\semantic_smiles_splitter.py ^
  --smiles "N[C@@H](CC1=CC=CC=C1)C(=O)O" ^
  --drug-number TEST001 ^
  --indication oncology ^
  --write-db ^
  --version semantic_current
```

### 11.3 小批量批跑

```bash
python split_smile\semantic_smiles_splitter.py --write-db --limit 20 --version semantic_current
```

### 11.4 全量重跑

```bash
python split_smile\semantic_smiles_splitter.py --write-db --version semantic_current
```

如果数据库连接不稳定，可使用断点续跑：

```bash
python split_smile\semantic_smiles_splitter.py --write-db --offset 1000 --version semantic_current
```

## 12. 当前实现口径

当前版本已经移除：

- `template_rule`
- 外部 `GenericRules` 配置文件
- 与旧规则学习链路相关的 Excel 和派生脚本

当前保留的主逻辑是：

- 语义拆分
- `drug_feature_rule`
- 固定默认通用拆分
- 全局优先级裁决
- MySQL 入库和覆盖更新
## 杂

amino_acid_residue:amino_acid_like(类氨基酸残基)
amino_acid_residue:glycine(甘氨酸残基)
amino_acid_residue:proline(脯氨酸残基)
connected_component(整个分子或独立子结构)
functional_group:amide酰胺基（-CONH-）
functional_group:amine胺基（-NH₂ / -NR₂）
functional_group:carboxyl羧基（-COOH）
functional_group:ether醚键（-O-）
functional_group:hydroxyl羟基（-OH）
functional_group:nitrile腈基（-C≡N）
functional_group:nitro硝基（-NO₂）
functional_group:thioether硫醚（-S-）