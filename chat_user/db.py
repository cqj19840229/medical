"""Database connection helpers."""

import logging

import mysql.connector
from mysql.connector import Error

from config import DB_CONFIG

logger = logging.getLogger("chat_user_api")


class LoggingCursor:
    """Cursor wrapper that logs executed SQL statements."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, operation, params=None, map_results=False):
        logger.info("sql execute: %s | params=%s", " ".join(str(operation).split()), params)
        if map_results:
            try:
                return self._cursor.execute(operation, params=params, map_results=map_results)
            except TypeError:
                return self._cursor.execute(operation, params)
        return self._cursor.execute(operation, params)

    def executemany(self, operation, seq_params):
        logger.info("sql executemany: %s | params=%s", " ".join(str(operation).split()), seq_params)
        return self._cursor.executemany(operation, seq_params)

    def __getattr__(self, item):
        return getattr(self._cursor, item)


class LoggingConnection:
    """Connection wrapper that returns logging cursors."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *args, **kwargs):
        return LoggingCursor(self._conn.cursor(*args, **kwargs))

    def __getattr__(self, item):
        return getattr(self._conn, item)


def get_conn():
    """Create and return a MySQL connection."""
    try:
        conn = mysql.connector.connect(
            host=DB_CONFIG["host"],
            port=DB_CONFIG["port"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            database=DB_CONFIG["database"],
            charset=DB_CONFIG["charset"],
            use_unicode=True,
            autocommit=False,
        )
        return LoggingConnection(conn)
    except Error as exc:
        raise RuntimeError(f"MySQL connection failed: {exc}") from exc
