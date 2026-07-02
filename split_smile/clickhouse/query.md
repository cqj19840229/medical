# 查询有多少个不重复的canonical_smiles
SELECT
    uniqExact(canonical_smiles) AS unique_canonical_smiles
FROM drug_fragment.fragments_small
WHERE canonical_smiles != '';


# 查询不重复的canonical_smiles是什么
SELECT DISTINCT canonical_smiles
FROM drug_fragment.fragments_small
WHERE canonical_smiles != ''
ORDER BY canonical_smiles;

# 查大分子的
SELECT
    f.molecule_id,
    i.ingredient_name,
    f.fragment_hash256,
    f.canonical_smiles,
    f.atom_count,
    f.bond_count,
    f.occurrence_count
FROM drug_fragment.fragments_big_unique AS f
LEFT JOIN drug_fragment.molecule_ingredients AS i
    USING (molecule_id)
WHERE i.ingredient_name = 'dextroamphetamine';
# 查小分子的
SELECT
    f.molecule_id,
    any(i.ingredient_name) AS ingredient_name,
    f.fragment_hash256,
    any(f.canonical_smiles) AS canonical_smiles,
    any(f.atom_count) AS atom_count,
    any(f.bond_count) AS bond_count,
    count() AS occurrence_count
FROM drug_fragment.fragments_small AS f
INNER JOIN drug_fragment.molecule_ingredients AS i
    ON f.molecule_id = i.molecule_id
WHERE i.ingredient_name = 'dextroamphetamine'
GROUP BY
    f.molecule_id,
    f.fragment_hash256
ORDER BY occurrence_count DESC;
# 统一查询的
SELECT
    source,
    molecule_id,
    ingredient_name,
    fragment_hash256,
    canonical_smiles,
    atom_count,
    bond_count,
    occurrence_count
FROM
(
    SELECT
        'small' AS source,
        f.molecule_id,
        any(i.ingredient_name) AS ingredient_name,
        f.fragment_hash256,
        any(f.canonical_smiles) AS canonical_smiles,
        any(f.atom_count) AS atom_count,
        any(f.bond_count) AS bond_count,
        count() AS occurrence_count
    FROM drug_fragment.fragments_small AS f
    INNER JOIN drug_fragment.molecule_ingredients AS i
        ON f.molecule_id = i.molecule_id
    WHERE i.ingredient_name = 'dextroamphetamine'
    GROUP BY
        f.molecule_id,
        f.fragment_hash256
    UNION ALL
    SELECT
        'big' AS source,
        f.molecule_id,
        any(i.ingredient_name) AS ingredient_name,
        f.fragment_hash256,
        anyMerge(f.canonical_smiles_state) AS canonical_smiles,
        anyMerge(f.atom_count_state) AS atom_count,
        anyMerge(f.bond_count_state) AS bond_count,
        countMerge(f.occurrence_count_state) AS occurrence_count
    FROM drug_fragment.fragments_big_unique_state AS f
    INNER JOIN drug_fragment.molecule_ingredients AS i
        ON f.molecule_id = i.molecule_id
    WHERE i.ingredient_name = 'dextroamphetamine'
    GROUP BY
        f.molecule_id,
        f.fragment_hash256
)
ORDER BY
    molecule_id,
    atom_count,
    canonical_smiles;