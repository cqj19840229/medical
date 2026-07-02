#  创建小分子表
clickhouse-client \
  --host 36.151.241.14 \
  --port 9000 \
  --user default \
  --password \
  --multiquery <<'SQL'
CREATE DATABASE IF NOT EXISTS drug_fragment;

CREATE TABLE IF NOT EXISTS drug_fragment.fragments_small
(
    molecule_id LowCardinality(String),
    fragment_key String,
    fragment_hash256 String,
    canonical_smiles String,
    atom_count UInt16,
    bond_count UInt16,
    src_file String DEFAULT '',
    imported_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY
(
    molecule_id,
    atom_count,
    bond_count,
    fragment_hash256,
    fragment_key
)
SETTINGS index_granularity = 8192;
SQL

# 创建大分子
CREATE DATABASE IF NOT EXISTS drug_fragment;

-- 1. Parquet 导入入口。
-- Null 表本身不保存数据，只触发下面的物化视图。
CREATE TABLE IF NOT EXISTS drug_fragment.fragments_big_ingest
(
    molecule_id LowCardinality(String),
    fragment_key String,
    fragment_hash256 String,
    canonical_smiles String,
    atom_count UInt16,
    bond_count UInt16,
    src_file String DEFAULT '',
    imported_at DateTime DEFAULT now()
)
ENGINE = Null;


-- 2. 按“分子 + 化学结构哈希”保存聚合状态。
CREATE TABLE IF NOT EXISTS drug_fragment.fragments_big_unique_state
(
    molecule_id LowCardinality(String),

    -- SHA-256 十六进制固定为 64 个字符
    fragment_hash256 FixedString(64),

    canonical_smiles_state AggregateFunction(any, String),
    atom_count_state AggregateFunction(any, UInt16),
    bond_count_state AggregateFunction(any, UInt16),

    -- 记录该结构在原始拆分结果中出现多少次
    occurrence_count_state AggregateFunction(count)
)
ENGINE = AggregatingMergeTree
PARTITION BY molecule_id
ORDER BY fragment_hash256
SETTINGS index_granularity = 8192;


-- 3. 导入时自动按 molecule_id + fragment_hash256 聚合。
CREATE MATERIALIZED VIEW IF NOT EXISTS
    drug_fragment.mv_fragments_big_unique
TO drug_fragment.fragments_big_unique_state
AS
SELECT
    molecule_id,
    fragment_hash256,
    anyState(canonical_smiles) AS canonical_smiles_state,
    anyState(atom_count) AS atom_count_state,
    anyState(bond_count) AS bond_count_state,
    countState() AS occurrence_count_state
FROM
(
    SELECT
        molecule_id,
        CAST(fragment_hash256, 'FixedString(64)') AS fragment_hash256,
        canonical_smiles,
        atom_count,
        bond_count
    FROM drug_fragment.fragments_big_ingest
)
GROUP BY
    molecule_id,
    fragment_hash256;


-- 4. 普通查询视图。
-- 查询者不需要手动写 anyMerge/countMerge。
CREATE VIEW IF NOT EXISTS drug_fragment.fragments_big_unique
AS
SELECT
    molecule_id,
    fragment_hash256,
    anyMerge(canonical_smiles_state) AS canonical_smiles,
    anyMerge(atom_count_state) AS atom_count,
    anyMerge(bond_count_state) AS bond_count,
    countMerge(occurrence_count_state) AS occurrence_count
FROM drug_fragment.fragments_big_unique_state
GROUP BY
    molecule_id,
    fragment_hash256;

##
DROP VIEW IF EXISTS drug_fragment.fragments_all_unique;

CREATE VIEW drug_fragment.fragments_all_unique
AS
SELECT
    f.source,
    f.molecule_id,
    i.ingredient_name,
    f.fragment_hash256,
    f.canonical_smiles,
    f.atom_count,
    f.bond_count,
    f.occurrence_count
FROM
(
    /* 小分子：查询时按结构哈希去重 */
    SELECT
        'small' AS source,
        molecule_id,
        toString(fragment_hash256) AS fragment_hash256,
        any(canonical_smiles) AS canonical_smiles,
        any(atom_count) AS atom_count,
        any(bond_count) AS bond_count,
        count() AS occurrence_count
    FROM drug_fragment.fragments_small
    GROUP BY
        molecule_id,
        fragment_hash256

    UNION ALL

    /* 大分子：合并 AggregatingMergeTree 中的聚合状态 */
    SELECT
        'big' AS source,
        molecule_id,
        toString(fragment_hash256) AS fragment_hash256,
        anyMerge(canonical_smiles_state) AS canonical_smiles,
        anyMerge(atom_count_state) AS atom_count,
        anyMerge(bond_count_state) AS bond_count,
        countMerge(occurrence_count_state) AS occurrence_count
    FROM drug_fragment.fragments_big_unique_state
    GROUP BY
        molecule_id,
        fragment_hash256
) AS f
LEFT JOIN drug_fragment.molecule_ingredients AS i
    ON f.molecule_id = i.molecule_id;