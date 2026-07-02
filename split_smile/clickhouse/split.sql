-- drug_fragment.fragments_big_ingest definition

CREATE TABLE drug_fragment.fragments_big_ingest
(

    `molecule_id` LowCardinality(String),

    `fragment_key` String,

    `fragment_hash256` String,

    `canonical_smiles` String,

    `atom_count` UInt16,

    `bond_count` UInt16,

    `src_file` String DEFAULT '',

    `imported_at` DateTime DEFAULT now()
)
ENGINE = `Null`;


-- drug_fragment.fragments_big_unique_state definition

CREATE TABLE drug_fragment.fragments_big_unique_state
(

    `molecule_id` LowCardinality(String),

    `fragment_hash256` FixedString(64),

    `canonical_smiles_state` AggregateFunction(any,
 String),

    `atom_count_state` AggregateFunction(any,
 UInt16),

    `bond_count_state` AggregateFunction(any,
 UInt16),

    `occurrence_count_state` AggregateFunction(count)
)
ENGINE = AggregatingMergeTree
PARTITION BY molecule_id
ORDER BY fragment_hash256
SETTINGS index_granularity = 8192;


-- drug_fragment.fragments_small definition

CREATE TABLE drug_fragment.fragments_small
(

    `molecule_id` LowCardinality(String),

    `fragment_key` String,

    `fragment_hash256` String,

    `canonical_smiles` String,

    `atom_count` UInt16,

    `bond_count` UInt16,

    `src_file` String DEFAULT '',

    `imported_at` DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (molecule_id,
 atom_count,
 bond_count,
 fragment_hash256,
 fragment_key)
SETTINGS index_granularity = 8192;


-- drug_fragment.molecule_ingredients definition

CREATE TABLE drug_fragment.molecule_ingredients
(

    `molecule_id` LowCardinality(String),

    `ingredient_name` String,

    `imported_at` DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY molecule_id
SETTINGS index_granularity = 8192;

