"""Validation-related service functions."""

import json
import logging
from datetime import datetime
from typing import List, Optional

from mysql.connector import Error, IntegrityError

from db import get_conn

logger = logging.getLogger("chat_user_api")


def create_validate(turn_id: int, response_id: int) -> int:
    """Create one validate record with default status and return id."""
    validate_payload = _get_validate_payload(turn_id, response_id)

    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO zhiling_validate (
                turn_id, response_id, request_content, response_title, response_content
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                turn_id,
                response_id,
                validate_payload["request_content"],
                validate_payload["response_title"],
                validate_payload["response_content"],
            ),
        )
        conn.commit()
        return cursor.lastrowid
    except IntegrityError as exc:
        conn.rollback()
        raise ValueError("Validation record already exists for this turn_id and response_id") from exc
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to create validate record: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def create_validates_batch(items: List[dict]) -> List[int]:
    """Batch create validate records and return created ids."""
    if not items:
        return []

    conn = get_conn()
    cursor = conn.cursor()
    try:
        conn.start_transaction()
        created_ids: List[int] = []
        for item in items:
            turn_id = item["turn_id"]
            response_ids = item["response_ids"]
            if not response_ids:
                raise ValueError("response_ids cannot be empty")

            for response_id in response_ids:
                validate_payload = _get_validate_payload(turn_id, response_id)
                cursor.execute(
                    """
                    INSERT INTO zhiling_validate (
                        turn_id, response_id, request_content, response_title, response_content
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        turn_id,
                        response_id,
                        validate_payload["request_content"],
                        validate_payload["response_title"],
                        validate_payload["response_content"],
                    ),
                )
                created_ids.append(cursor.lastrowid)

        conn.commit()
        return created_ids
    except IntegrityError as exc:
        conn.rollback()
        raise ValueError("One or more validation records already exist for the provided turn_id and response_id pairs") from exc
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to batch create validate records: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def get_validate_by_id(validate_id: int) -> Optional[dict]:
    """Get one validate record by id."""
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT
                id,
                turn_id,
                response_id,
                request_content,
                response_title,
                response_content,
                status,
                judge_conclusion,
                judge_content,
                attachment_urls,
                create_at,
                update_at
            FROM zhiling_validate
            WHERE id = %s
            """,
            (validate_id,),
        )
        row = cursor.fetchone()
        return _deserialize_validate_row(row)
    except Error as exc:
        raise RuntimeError(f"Failed to query validate record by id: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def list_validates_by_filters(
    user_id: int,
    status: Optional[str],
    judge_conclusion: Optional[int],
    start_time: Optional[datetime],
    end_time: Optional[datetime],
    keywords: Optional[str],
) -> List[dict]:
    """Batch query validate records with optional filters."""
    if judge_conclusion is not None and judge_conclusion not in (-1, 0, 1):
        raise ValueError("judge_conclusion must be one of -1, 0, 1")
    if start_time and end_time and start_time > end_time:
        raise ValueError("startTime cannot be greater than endTime")

    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        where_parts = ["ud.user_id = %s"]
        params: List[object] = [user_id]

        if status:
            where_parts.append("zv.status = %s")
            params.append(status)
        if judge_conclusion is not None:
            where_parts.append("zv.judge_conclusion = %s")
            params.append(judge_conclusion)
        if start_time is not None:
            where_parts.append("zv.update_at >= %s")
            params.append(start_time)
        if end_time is not None:
            where_parts.append("zv.update_at <= %s")
            params.append(end_time)
        if keywords:
            where_parts.append(
                "(zv.request_content LIKE %s OR zv.response_title LIKE %s OR zv.response_content LIKE %s)"
            )
            keyword_value = f"%{keywords}%"
            params.extend([keyword_value, keyword_value, keyword_value])

        sql = f"""
        SELECT
            zv.id,
            zv.turn_id,
            zv.response_id,
            zv.request_content,
            zv.response_title,
            zv.response_content,
            zv.status,
            zv.judge_conclusion,
            zv.judge_content,
            zv.attachment_urls,
            zv.create_at,
            zv.update_at
        FROM zhiling_validate AS zv
        INNER JOIN dialogue_turns AS dt
            ON zv.turn_id = dt.turn_id
        INNER JOIN user_dialogues AS ud
            ON dt.dialogue_id = ud.dialogue_id
        WHERE {' AND '.join(where_parts)}
        ORDER BY zv.id ASC
        """
        logger.info("list_validates_by_filters sql=%s params=%s", " ".join(sql.split()), params)
        cursor.execute(sql, tuple(params))
        return [_deserialize_validate_row(row) for row in cursor.fetchall()]
    except ValueError:
        raise
    except Error as exc:
        raise RuntimeError(f"Failed to batch query validate records: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def update_validate(
    validate_id: int,
    judge_conclusion: Optional[int],
    judge_content: Optional[str],
    attachment_urls: Optional[List[str]],
) -> Optional[dict]:
    """Update validate record by id and return latest row."""
    if judge_conclusion is not None and judge_conclusion not in (-1, 0, 1):
        raise ValueError("judge_conclusion must be one of -1, 0, 1")

    attachment_urls_json = json.dumps(attachment_urls or [], ensure_ascii=False)

    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            UPDATE zhiling_validate
            SET judge_conclusion = %s,
                judge_content = %s,
                attachment_urls = %s,
                status = CASE
                    WHEN %s IS NULL AND %s IS NULL AND %s = '[]' THEN status
                    ELSE '已验证'
                END,
                update_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (
                judge_conclusion,
                judge_content,
                attachment_urls_json,
                judge_conclusion,
                judge_content,
                attachment_urls_json,
                validate_id,
            ),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return None
        conn.commit()
        cursor.execute(
            """
            SELECT
                id,
                turn_id,
                response_id,
                request_content,
                response_title,
                response_content,
                status,
                judge_conclusion,
                judge_content,
                attachment_urls,
                create_at,
                update_at
            FROM zhiling_validate
            WHERE id = %s
            """,
            (validate_id,),
        )
        return _deserialize_validate_row(cursor.fetchone())
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to update validate record: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def delete_validate(validate_id: int) -> bool:
    """Delete one validate record by id."""
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            DELETE FROM zhiling_validate
            WHERE id = %s
            """,
            (validate_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to delete validate record: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def _deserialize_validate_row(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    value = row.get("attachment_urls")
    if value:
        try:
            row["attachment_urls"] = json.loads(value)
        except json.JSONDecodeError:
            row["attachment_urls"] = []
    else:
        row["attachment_urls"] = []
    return row


def _get_validate_payload(turn_id: int, response_id: int) -> dict:
    """Load validate snapshot fields using the same association logic as batch-query."""
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT
                dt.request_content,
                ur.response_title,
                ur.response_content
            FROM dialogue_turns AS dt
            INNER JOIN user_dialogue_turns_response AS ur
                ON dt.turn_id = ur.turn_id
            WHERE dt.turn_id = %s AND ur.id = %s
            """,
            (turn_id, response_id),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError("turn_id and response_id do not match an existing dialogue response")
        return row
    except Error as exc:
        raise RuntimeError(f"Failed to load validate payload: {exc}") from exc
    finally:
        cursor.close()
        conn.close()
