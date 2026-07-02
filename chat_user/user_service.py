"""User-related service functions."""

from typing import Optional

import bcrypt
from mysql.connector import Error, IntegrityError

from db import get_conn


def get_user_by_username(username: str) -> Optional[dict]:
    """Return user info by username, or None if not found."""
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        sql = """
        SELECT user_id, username, password_hash, created_at
        FROM users
        WHERE username = %s
        """
        cursor.execute(sql, (username,))
        return cursor.fetchone()
    except Error as exc:
        raise RuntimeError(f"Failed to query user: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def get_user_by_id(user_id: int) -> Optional[dict]:
    """Return user info by user_id, or None if not found."""
    conn = get_conn()
    cursor = conn.cursor(dictionary=True)
    try:
        sql = """
        SELECT user_id, username, password_hash, created_at
        FROM users
        WHERE user_id = %s
        """
        cursor.execute(sql, (user_id,))
        return cursor.fetchone()
    except Error as exc:
        raise RuntimeError(f"Failed to query user by id: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def create_user(username: str, password: str) -> int:
    """Create a user and return the new user_id."""
    if not username or not password:
        raise ValueError("username and password cannot be empty")

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    conn = get_conn()
    cursor = conn.cursor()
    try:
        sql = """
        INSERT INTO users (username, password_hash)
        VALUES (%s, %s)
        """
        cursor.execute(sql, (username, password_hash))
        conn.commit()
        return cursor.lastrowid
    except IntegrityError as exc:
        conn.rollback()
        raise ValueError(f"User '{username}' already exists") from exc
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to create user: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


def verify_user(username: str, password: str) -> Optional[int]:
    """Verify user credentials and return user_id on success."""
    try:
        user = get_user_by_username(username)
    except RuntimeError:
        raise

    if not user:
        return None

    password_hash = user["password_hash"].encode("utf-8")
    if bcrypt.checkpw(password.encode("utf-8"), password_hash):
        return user["user_id"]
    return None


def change_password(user_id: int, old_password: str, new_password: str) -> bool:
    """Change a user's password after verifying the old password."""
    if not old_password or not new_password:
        raise ValueError("old_password and new_password cannot be empty")

    user = get_user_by_id(user_id)
    if not user:
        return False

    password_hash = user["password_hash"].encode("utf-8")
    if not bcrypt.checkpw(old_password.encode("utf-8"), password_hash):
        return False

    new_password_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE users
            SET password_hash = %s
            WHERE user_id = %s
            """,
            (new_password_hash, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to change password: {exc}") from exc
    finally:
        cursor.close()
        conn.close()
