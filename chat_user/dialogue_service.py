"""Dialogue-related service functions."""

from typing import List, Optional

from mysql.connector import Error

from db import get_conn


def create_dialogue(
    user_id: int,
    title: str,
    request_content: str,
    response_title: Optional[str],
    response_content: Optional[str],
    response_svgs: Optional[List[str]],
    responses: List[dict],
) -> dict:
    """
    Create one dialogue and its first turn in a single transaction.

    Returns a dict containing the new dialogue_id and first turn_id.
    """
    if not title or not request_content:
        raise ValueError("title and request_content cannot be empty")
    if response_svgs is None:
        response_svgs = []
    if response_title and not response_content:
        raise ValueError("response_content cannot be empty when response_title is provided")
    if response_content and not response_title:
        raise ValueError("response_title cannot be empty when response_content is provided")
    if not responses and not (response_title and response_content):
        raise ValueError("responses or top-level response_title/response_content cannot be empty")

    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        conn.start_transaction()

        cursor.execute(
            """
            INSERT INTO user_dialogues (user_id, title, turn_count)
            VALUES (%s, %s, 1)
            """,
            (user_id, title),
        )
        dialogue_id = cursor.lastrowid

        cursor.execute(
            """
            INSERT INTO dialogue_turns (dialogue_id, request_content, response_title, response_content)
            VALUES (%s, %s, %s, %s)
            """,
            (dialogue_id, request_content, response_title, response_content),
        )
        turn_id = cursor.lastrowid

        svg_count = 0
        for svg in response_svgs:
            cursor.execute(
                """
                INSERT INTO user_dialogue_turns_img_svg (turn_id, svg)
                VALUES (%s, %s)
                """,
                (turn_id, svg),
            )
            svg_count += 1

        for index, response in enumerate(responses, start=1):
            response_title = response["response_title"]
            response_content = response["response_content"]
            child_response_svgs = response.get("response_svgs", [])

            if not response_title or not response_content:
                raise ValueError("response_title and response_content cannot be empty")

            cursor.execute(
                """
                INSERT INTO user_dialogue_turns_response (
                    turn_id, resp_no, response_title, response_content
                )
                VALUES (%s, %s, %s, %s)
                """,
                (turn_id, index, response_title, response_content),
            )
            response_id = cursor.lastrowid

            for svg in child_response_svgs:
                cursor.execute(
                    """
                    INSERT INTO user_dialogue_turns_response_img_svg (turn_id, response_id, svg)
                    VALUES (%s, %s, %s)
                    """,
                    (turn_id, response_id, svg),
                )
                svg_count += 1

        conn.commit()
        return {"dialogue_id": dialogue_id, "turn_id": turn_id, "svg_count": svg_count}
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to create dialogue: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def update_dialogue_title(dialogue_id: int, title: str) -> Optional[dict]:
    """Update a dialogue title and return the latest dialogue summary."""
    if not title:
        raise ValueError("title cannot be empty")

    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            UPDATE user_dialogues
            SET title = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE dialogue_id = %s AND is_deleted = 0
            """,
            (title, dialogue_id),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return None

        conn.commit()
        cursor.execute(
            """
            SELECT dialogue_id, user_id, title, turn_count, created_at, updated_at
            FROM user_dialogues
            WHERE dialogue_id = %s AND is_deleted = 0
            """,
            (dialogue_id,),
        )
        return cursor.fetchone()
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to update dialogue title: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def delete_dialogue(dialogue_id: int) -> bool:
    """Logically delete one dialogue."""
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        conn.start_transaction()

        cursor.execute(
            """
            SELECT dialogue_id
            FROM user_dialogues
            WHERE dialogue_id = %s AND is_deleted = 0
            FOR UPDATE
            """,
            (dialogue_id,),
        )
        dialogue = cursor.fetchone()
        if not dialogue:
            conn.rollback()
            return False

        cursor.execute(
            """
            UPDATE user_dialogues
            SET is_deleted = 1,
                deleted_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE dialogue_id = %s
            """,
            (dialogue_id,),
        )
        conn.commit()
        return True
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to delete dialogue: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def append_dialogue_turn_by_dialogue_id(
    dialogue_id: int,
    request_content: str,
    response_title: Optional[str],
    response_content: Optional[str],
    response_svgs: Optional[List[str]],
    responses: List[dict],
) -> Optional[dict]:
    """
    Append a new turn to the dialogue identified by dialogue_id.

    The parent dialogue turn count and updated_at are also updated.
    """
    if not request_content:
        raise ValueError("request_content cannot be empty")
    if response_svgs is None:
        response_svgs = []
    if response_title and not response_content:
        raise ValueError("response_content cannot be empty when response_title is provided")
    if response_content and not response_title:
        raise ValueError("response_title cannot be empty when response_content is provided")
    if not responses and not (response_title and response_content):
        raise ValueError("responses or top-level response_title/response_content cannot be empty")

    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        conn.start_transaction()

        cursor.execute(
            """
            SELECT dialogue_id
            FROM user_dialogues
            WHERE dialogue_id = %s AND is_deleted = 0
            FOR UPDATE
            """,
            (dialogue_id,),
        )
        dialogue_row = cursor.fetchone()
        if not dialogue_row:
            conn.rollback()
            return None

        cursor.execute(
            """
            UPDATE user_dialogues
            SET turn_count = turn_count + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE dialogue_id = %s
            """,
            (dialogue_id,),
        )

        cursor.execute(
            """
            INSERT INTO dialogue_turns (dialogue_id, request_content, response_title, response_content)
            VALUES (%s, %s, %s, %s)
            """,
            (dialogue_id, request_content, response_title, response_content),
        )
        new_turn_id = cursor.lastrowid

        svg_count = 0
        for svg in response_svgs:
            cursor.execute(
                """
                INSERT INTO user_dialogue_turns_img_svg (turn_id, svg)
                VALUES (%s, %s)
                """,
                (new_turn_id, svg),
            )
            svg_count += 1

        for index, response in enumerate(responses, start=1):
            response_title = response["response_title"]
            response_content = response["response_content"]
            child_response_svgs = response.get("response_svgs", [])

            if not response_title or not response_content:
                raise ValueError("response_title and response_content cannot be empty")

            cursor.execute(
                """
                INSERT INTO user_dialogue_turns_response (
                    turn_id, resp_no, response_title, response_content
                )
                VALUES (%s, %s, %s, %s)
                """,
                (new_turn_id, index, response_title, response_content),
            )

            response_id = cursor.lastrowid

            for svg in child_response_svgs:
                cursor.execute(
                    """
                    INSERT INTO user_dialogue_turns_response_img_svg (turn_id, response_id, svg)
                    VALUES (%s, %s, %s)
                    """,
                    (new_turn_id, response_id, svg),
                )
                svg_count += 1

        conn.commit()
        cursor.execute(
            """
            SELECT turn_id, dialogue_id, request_content, response_title, response_content, created_at, updated_at
            FROM dialogue_turns
            WHERE turn_id = %s
            """,
            (new_turn_id,),
        )
        turn = cursor.fetchone()
        if turn:
            turn["response_svgs"] = _get_turn_svgs(cursor, new_turn_id)
            turn["responses"] = _get_turn_responses(cursor, new_turn_id)
            turn["svg_count"] = svg_count
        return turn
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to append dialogue turn: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def get_user_dialogue_by_id(user_id: int, dialogue_id: int) -> Optional[dict]:
    """Return one dialogue using top-level turn response fields and turn SVGs only."""
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT dialogue_id, user_id, title, turn_count, created_at, updated_at
            FROM user_dialogues
            WHERE user_id = %s AND dialogue_id = %s AND is_deleted = 0
            """,
            (user_id, dialogue_id),
        )
        dialogue = cursor.fetchone()
        if not dialogue:
            return None

        cursor.execute(
            """
            SELECT turn_id, dialogue_id, request_content, response_title, response_content, created_at, updated_at
            FROM dialogue_turns
            WHERE dialogue_id = %s
            ORDER BY turn_id ASC
            """,
            (dialogue_id,),
        )
        turns = cursor.fetchall()
        for turn in turns:
            turn["response_svgs"] = _get_turn_svgs(cursor, turn["turn_id"])
            turn["responses"] = []
            turn["svg_count"] = len(turn["response_svgs"])
        dialogue["turns"] = turns
        return dialogue
    except Error as exc:
        raise RuntimeError(f"Failed to get dialogue by id: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def get_dialogue_turns_with_validate_status(dialogue_id: int) -> Optional[dict]:
    """Return one dialogue using child response tables and validate flags."""
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT dialogue_id, user_id, title, turn_count, created_at, updated_at
            FROM user_dialogues
            WHERE dialogue_id = %s AND is_deleted = 0
            """,
            (dialogue_id,),
        )
        dialogue = cursor.fetchone()
        if not dialogue:
            return None

        cursor.execute(
            """
            SELECT turn_id, dialogue_id, request_content, response_title, response_content, created_at, updated_at
            FROM dialogue_turns
            WHERE dialogue_id = %s
            ORDER BY turn_id ASC
            """,
            (dialogue_id,),
        )
        turns = cursor.fetchall()
        for turn in turns:
            turn["response_title"] = None
            turn["response_content"] = None
            turn["response_svgs"] = []
            responses = _get_turn_responses(cursor, turn["turn_id"])
            for response in responses:
                cursor.execute(
                    """
                    SELECT id
                    FROM zhiling_validate
                    WHERE turn_id = %s AND response_id = %s
                    LIMIT 1
                    """,
                    (turn["turn_id"], response["id"]),
                )
                validate_row = cursor.fetchone()
                response["validate_added"] = validate_row is not None
                response["validate_id"] = validate_row["id"] if validate_row else None
            turn["responses"] = responses
            turn["svg_count"] = sum(len(response.get("response_svgs", [])) for response in responses)
        dialogue["turns"] = turns
        return dialogue
    except Error as exc:
        raise RuntimeError(f"Failed to get dialogue turns with validate status: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def list_dialogues(user_id: int) -> List[dict]:
    """Return all dialogue summaries for a user."""
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT dialogue_id, user_id, title, turn_count, created_at, updated_at
            FROM user_dialogues
            WHERE user_id = %s AND is_deleted = 0
            ORDER BY dialogue_id ASC
            """,
            (user_id,),
        )
        return cursor.fetchall()
    except Error as exc:
        raise RuntimeError(f"Failed to list dialogues: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def count_user_turns(user_id: int) -> int:
    """Return total dialogue turns for a single user."""
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT COUNT(dt.turn_id) AS total_turns
            FROM user_dialogues AS ud
            LEFT JOIN dialogue_turns AS dt
                ON ud.dialogue_id = dt.dialogue_id
            WHERE ud.user_id = %s AND ud.is_deleted = 0
            """,
            (user_id,),
        )
        row = cursor.fetchone()
        return row[0] if row else 0
    except Error as exc:
        raise RuntimeError(f"Failed to count user turns: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def count_all_users_turns() -> List[dict]:
    """Return dialogue turn counts for all users."""
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT u.user_id, u.username, COUNT(dt.turn_id) AS total_turns
            FROM users AS u
            LEFT JOIN user_dialogues AS ud
                ON u.user_id = ud.user_id
               AND ud.is_deleted = 0
            LEFT JOIN dialogue_turns AS dt
                ON ud.dialogue_id = dt.dialogue_id
            GROUP BY u.user_id, u.username
            ORDER BY u.user_id ASC
            """
        )
        return cursor.fetchall()
    except Error as exc:
        raise RuntimeError(f"Failed to count all users' turns: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def _get_turn_responses(cursor, turn_id: int) -> List[dict]:
    """Return all responses for one turn ordered by resp_no."""
    cursor.execute(
        """
        SELECT id, turn_id, resp_no, response_title, response_content, created_at
        FROM user_dialogue_turns_response
        WHERE turn_id = %s
        ORDER BY resp_no ASC, id ASC
        """,
        (turn_id,),
    )
    rows = cursor.fetchall()
    for row in rows:
        row["response_svgs"] = _get_response_svgs(cursor, turn_id, row["id"])
    return rows


def _get_turn_svgs(cursor, turn_id: int) -> List[str]:
    """Return all SVGs for one turn."""
    cursor.execute(
        """
        SELECT svg
        FROM user_dialogue_turns_img_svg
        WHERE turn_id = %s
        ORDER BY id ASC
        """,
        (turn_id,),
    )
    return [row["svg"] for row in cursor.fetchall()]


def _get_response_svgs(cursor, turn_id: int, response_id: int) -> List[str]:
    """Return all SVGs for one response row."""
    cursor.execute(
        """
        SELECT svg
        FROM user_dialogue_turns_response_img_svg
        WHERE turn_id = %s AND response_id = %s
        ORDER BY id ASC
        """,
        (turn_id, response_id),
    )
    return [row["svg"] for row in cursor.fetchall()]
