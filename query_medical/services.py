from collections.abc import Iterable
from contextlib import closing
import logging
from time import perf_counter
from typing import Any

import mysql.connector
from neo4j import GraphDatabase, Query
from rdkit import Chem
from rdkit.Chem.MolStandardize import rdMolStandardize

from config import DB_CONFIG, NEO4J_CONFIG

logger = logging.getLogger("fragment_api")

PHARMACOKINETIC_FIELDS = (
    "half_life",
    "clearance",
    "bioavailability",
    "cmax",
    "tmax",
    "apparent_volume_distribution",
    "protein_binding",
    "tissue_distribution",
    "excretion_route",
)


class InvalidSmilesError(ValueError):
    pass


def standardize_smiles(smiles: str) -> tuple[str, Chem.Mol]:
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        raise InvalidSmilesError("fragment 不是有效的 SMILES")
    try:
        mol = rdMolStandardize.Cleanup(mol)
        mol = rdMolStandardize.FragmentParent(mol)
    except Exception as exc:
        raise InvalidSmilesError(f"fragment 标准化失败: {exc}") from exc
    return Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True), mol


def search_ingredient_smiles(fragment_mol: Chem.Mol) -> list[dict[str, str | None]]:
    started = perf_counter()
    sql = """
        SELECT DISTINCT active_ingredient, smiles
        FROM pdf_ingredient_smiles
        WHERE smiles IS NOT NULL AND TRIM(smiles) <> ''
    """
    matches: list[dict[str, str | None]] = []
    connection_config = {
        **DB_CONFIG,
        "connection_timeout": 10,
        "read_timeout": 30,
        "write_timeout": 30,
    }
    scanned = 0
    with closing(mysql.connector.connect(**connection_config)) as connection:
        with closing(connection.cursor(dictionary=True)) as cursor:
            cursor.execute(sql)
            for row in cursor:
                scanned += 1
                molecule = Chem.MolFromSmiles(row["smiles"])
                if molecule is not None and molecule.HasSubstructMatch(fragment_mol):
                    matches.append(
                        {
                            "active_ingredient": row["active_ingredient"],
                            "smiles": row["smiles"],
                        }
                    )
    logger.info(
        "mysql_substructure_search scanned=%d matched=%d elapsed_ms=%.2f",
        scanned,
        len(matches),
        (perf_counter() - started) * 1000,
    )
    return matches


def _node_payload(node: Any) -> dict[str, Any]:
    if node is None:
        return {}
    return {"labels": sorted(node.labels), "properties": dict(node)}


def _node_property_values(nodes: Iterable[Any], property_name: str) -> list[Any]:
    """按原查询顺序返回节点属性值，并过滤空值、去重。"""
    values = []
    seen = set()
    for node in nodes:
        value = node.get(property_name) if node is not None else None
        if value is not None and value not in seen:
            seen.add(value)
            values.append(value)
    return values


def _deduplicate(values: Iterable[Any]) -> list[Any]:
    return list(dict.fromkeys(value for value in values if value is not None))


def _query_effect_neo4j(names: list[str]) -> dict[str, dict[str, Any]]:
    """从 Drug 直接查询适应症、临床表征和靶标。"""
    cypher = """
    UNWIND $ingredients AS ingredient
    MATCH (d:Drug)
    WHERE toLower(coalesce(d.drug_name, '')) CONTAINS toLower(ingredient)
    OPTIONAL MATCH (d)-[:TARGETS]-(target:Target)
    WITH ingredient, d,
         [x IN collect(DISTINCT target.target_name) WHERE x IS NOT NULL]
         AS target_names
    OPTIONAL MATCH (d)-[:TREATS]-(indication:Indication)
    WITH ingredient, d, target_names,
         [x IN collect(DISTINCT indication) WHERE x IS NOT NULL] AS indications
    UNWIND CASE WHEN indications = [] THEN [NULL] ELSE indications END AS indication
    OPTIONAL MATCH (indication)-[:HAS_CLINICAL_FEATURE]-(feature:ClinicalFeature)
    WITH ingredient,
         collect(DISTINCT indication.indication_name) AS indication_names,
         collect(DISTINCT feature.feature_name) AS feature_names,
         collect(DISTINCT target_names) AS target_name_groups
    RETURN ingredient,
           [x IN indication_names WHERE x IS NOT NULL] AS indication_names,
           [x IN feature_names WHERE x IS NOT NULL] AS feature_names,
           reduce(result = [], group IN target_name_groups |
               reduce(inner = result, name IN group |
                   CASE WHEN name IN inner THEN inner ELSE inner + [name] END
               )
           ) AS target_names
    """
    driver = GraphDatabase.driver(
        NEO4J_CONFIG["uri"],
        auth=(NEO4J_CONFIG["user"], NEO4J_CONFIG["password"]),
        connection_timeout=10,
        connection_acquisition_timeout=10,
        max_transaction_retry_time=5,
    )
    started = perf_counter()
    try:
        with driver.session(database=NEO4J_CONFIG["database"]) as session:
            logger.info("neo4j_effect_query cypher=%s", " ".join(cypher.split()))
            rows = session.run(Query(cypher, timeout=30), ingredients=names)
            output = {
                row["ingredient"]: {
                    "indication_name": row["indication_names"],
                    "feature_name": row["feature_names"],
                    "target_name": row["target_names"],
                    "drug_found": True,
                }
                for row in rows
            }
    finally:
        driver.close()
        logger.info(
            "neo4j_effect_end ingredient_count=%d elapsed_ms=%.2f",
            len(names),
            (perf_counter() - started) * 1000,
        )

    for name in names:
        output.setdefault(
            name,
            {
                "indication_name": [],
                "feature_name": [],
                "target_name": [],
                "drug_found": False,
            },
        )
    return output


def _query_neo4j(
    ingredients: Iterable[str], query_type: str, fragment: str | None = None
) -> dict[str, dict[str, Any]]:
    names = list(dict.fromkeys(name for name in ingredients if name))
    if not names:
        return {}

    if query_type == "effect":
        return _query_effect_neo4j(names)

    if query_type == "pk":
        cypher = """
        MATCH (d:Drug)
        WHERE d.ingredient IN $ingredients
        OPTIONAL MATCH (d)-[:ENZYME_RELATION]-(enzyme:MetabolicEnzyme)
        RETURN d.ingredient AS ingredient, d,
               [x IN collect(DISTINCT enzyme) WHERE x IS NOT NULL] AS enzymes
        """
    elif query_type == "safety":
        cypher = """
        MATCH (d:Drug)
        WHERE d.ingredient IN $ingredients
        OPTIONAL MATCH (d)-[:HAS_ADVERSE_REACTION]-(reaction)
        RETURN d.ingredient AS ingredient, d,
               [x IN collect(DISTINCT reaction) WHERE x IS NOT NULL]
               AS adverse_reactions
        """
    else:
        raise ValueError(f"不支持的 query_type: {query_type}")

    output: dict[str, dict[str, Any]] = {}
    compact_cypher = " ".join(cypher.split())
    logger.info(
        "neo4j_query_start type=%s ingredient_count=%d timeout_seconds=30 cypher=%s",
        query_type,
        len(names),
        compact_cypher,
    )
    started = perf_counter()
    driver = GraphDatabase.driver(
        NEO4J_CONFIG["uri"],
        auth=(NEO4J_CONFIG["user"], NEO4J_CONFIG["password"]),
        connection_timeout=10,
        connection_acquisition_timeout=10,
        max_transaction_retry_time=5,
    )
    try:
        with driver.session(database=NEO4J_CONFIG["database"]) as session:
            query = Query(cypher, timeout=30)
            for record in session.run(query, ingredients=names, fragment=fragment):
                ingredient = record["ingredient"]
                drug = record["d"]
                if query_type == "pk":
                    properties = dict(drug) if drug is not None else {}
                    output[ingredient] = {
                        **{field: properties.get(field) for field in PHARMACOKINETIC_FIELDS},
                        "metabolic_enzymes": [
                            _node_payload(node) for node in record["enzymes"]
                        ],
                    }
                else:
                    output[ingredient] = {
                        "adverse_reactions": [
                            _node_payload(node) for node in record["adverse_reactions"]
                        ]
                    }
                output[ingredient]["drug_found"] = drug is not None
    finally:
        driver.close()
        logger.info(
            "neo4j_query_end type=%s ingredient_count=%d elapsed_ms=%.2f",
            query_type,
            len(names),
            (perf_counter() - started) * 1000,
        )
    return output


def enrich_matches(
    matches: list[dict[str, str | None]], query_type: str, fragment: str | None = None
) -> list[dict[str, Any]]:
    graph_data = _query_neo4j(
        (item["active_ingredient"] for item in matches), query_type, fragment
    )
    results = []
    for item in matches:
        ingredient = item["active_ingredient"]
        data = graph_data.get(ingredient or "", {"drug_found": False})
        results.append(
            {
                **item,
                "drug_found": data.get("drug_found", False),
                "data": {key: value for key, value in data.items() if key != "drug_found"},
            }
        )
    return results
