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


def _query_neo4j(ingredients: Iterable[str], query_type: str) -> dict[str, dict[str, Any]]:
    names = list(dict.fromkeys(name for name in ingredients if name))
    if not names:
        return {}

    if query_type == "pk":
        cypher = """
        MATCH (d:Drug)
        WHERE d.ingredient IN $ingredients
        OPTIONAL MATCH (d)-[:ENZYME_RELATION]-(enzyme:MetabolicEnzyme)
        RETURN d.ingredient AS ingredient, d,
               [x IN collect(DISTINCT enzyme) WHERE x IS NOT NULL] AS enzymes
        """
    else:
        cypher = """
        MATCH (d:Drug)
        WHERE d.ingredient IN $ingredients
        OPTIONAL MATCH (d)-[:DRUG_INTERACTION]-(interaction)
        WITH d,
             [x IN collect(DISTINCT interaction) WHERE x IS NOT NULL] AS interactions
        OPTIONAL MATCH (d)-[:HAS_CLINICAL_FEATURE]-(feature)
        WITH d, interactions,
             [x IN collect(DISTINCT feature) WHERE x IS NOT NULL] AS clinical_features
        OPTIONAL MATCH (d)-[:TARGETS]-(target)
        RETURN d.ingredient AS ingredient, d, interactions, clinical_features,
               [x IN collect(DISTINCT target) WHERE x IS NOT NULL] AS targets
        """

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
            for record in session.run(query, ingredients=names):
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
                        "drug_interactions": [
                            _node_payload(node) for node in record["interactions"]
                        ],
                        "clinical_features": [
                            _node_payload(node) for node in record["clinical_features"]
                        ],
                        "targets": [_node_payload(node) for node in record["targets"]],
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
    matches: list[dict[str, str | None]], query_type: str
) -> list[dict[str, Any]]:
    graph_data = _query_neo4j(
        (item["active_ingredient"] for item in matches), query_type
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
