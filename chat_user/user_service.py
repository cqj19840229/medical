"""User-related service functions."""

from typing import Optional

import bcrypt
from cryptography.fernet import Fernet
from mysql.connector import Error, IntegrityError

from config import USER_PASSWORD_KEY
from db import get_conn

_cipher = Fernet(USER_PASSWORD_KEY.encode())


def _encrypt_password(password: str) -> str:
    """Encrypt plain password to cipher text for storage."""
    return _cipher.encrypt(password.encode()).decode()


def _decrypt_password(token: str) -> str:
    """Decrypt stored cipher text back to plain password."""
    return _cipher.decrypt(token.encode()).decode()


def _verify_password(password: str, stored_password: str) -> bool:
    """Verify password against either the new reversible cipher or legacy bcrypt."""
    try:
        decrypted = _decrypt_password(stored_password)
        return decrypted == password
    except Exception:
        pass

    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored_password.encode("utf-8"))
    except Exception:
        return False


def _is_legacy_bcrypt(stored_password: str) -> bool:
    """Return True when the stored password looks like a legacy bcrypt hash."""
    return stored_password.startswith("$2a$") or stored_password.startswith("$2b$") or stored_password.startswith("$2y$")


def _upgrade_password_to_new_cipher(user_id: int, password: str) -> None:
    """Upgrade one user's stored password to the new cipher format."""
    encrypted_password = _encrypt_password(password)

    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE users
            SET password_hash = %s
            WHERE user_id = %s
            """,
            (encrypted_password, user_id),
        )
        conn.commit()
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to upgrade user password storage: {exc}") from exc
    finally:
        cursor.close()
        conn.close()


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

    encrypted_password = _encrypt_password(password)

    conn = get_conn()
    cursor = conn.cursor()
    try:
        sql = """
        INSERT INTO users (username, password_hash)
        VALUES (%s, %s)
        """
        cursor.execute(sql, (username, encrypted_password))
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

    if _verify_password(password, user["password_hash"]):
        if _is_legacy_bcrypt(user["password_hash"]):
            _upgrade_password_to_new_cipher(user["user_id"], password)
        return user["user_id"]
    return None


def change_password(user_id: int, old_password: str, new_password: str) -> bool:
    """Change a user's password after verifying the old password."""
    if not old_password or not new_password:
        raise ValueError("old_password and new_password cannot be empty")

    user = get_user_by_id(user_id)
    if not user:
        return False

    if not _verify_password(old_password, user["password_hash"]):
        return False

    new_encrypted_password = _encrypt_password(new_password)

    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            UPDATE users
            SET password_hash = %s
            WHERE user_id = %s
            """,
            (new_encrypted_password, user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Error as exc:
        conn.rollback()
        raise RuntimeError(f"Failed to change password: {exc}") from exc
    finally:
        cursor.close()
        conn.close()
