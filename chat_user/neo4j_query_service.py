"""Neo4j query-related service functions."""

from typing import Optional

from mysql.connector import Error

from db import get_conn


def create_neo4j_query(
    source_type: str,
    source: str,
    aim_type: str,
    aim: str,
    max_jump_num: int,
    max_path_num: int,
    user_id: int,
) -> dict:
    """Create one neo4j query record and return the inserted row."""
    if not source_type or not aim_type:
        raise ValueError("source_type and aim_type cannot be empty")
    if max_jump_num <= 0 or max_path_num <= 0:
        raise ValueError("max_jump_num and max_path_num must be greater than 0")

    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            INSERT INTO neo4j_query (
                source_type, source, aim_type, aim, max_jump_num, max_path_num, user_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (source_type, source, aim_type, aim, max_jump_num, max_path_num, user_id),
        )
        neo4j_query_id = cursor.lastrowid
        conn.commit()

        cursor.execute(
            """
            SELECT
                id,
                source_type,
                source,
                aim_type,
                aim,
                max_jump_num,
                max_path_num,
                user_id,
                created_at
            FROM neo4j_query
            WHERE id = %s
            """,
            (neo4j_query_id,),
        )
        row: Optional[dict] = cursor.fetchone()
        if not row:
            raise RuntimeError("Neo4j query record created but not found")
        return row
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to create neo4j query: {exc}") from exc
    finally:
        cursor.close()
        conn.close()
