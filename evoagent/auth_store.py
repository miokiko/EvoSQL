"""SQLite authentication and action audit storage for the Text2SQL console."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class AuthStore:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS memberships (
                user_id TEXT NOT NULL REFERENCES users(id), tenant_id TEXT NOT NULL,
                role TEXT NOT NULL, PRIMARY KEY(user_id, tenant_id)
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, resource_id TEXT NOT NULL,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)

    def create_user(self, user_id, username, password_hash, tenant_id, role):
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO users(id,username,password_hash) VALUES(?,?,?)",
                (user_id, username, password_hash),
            )
            actual_id = self.connection.execute(
                "SELECT id FROM users WHERE username=?", (username,)
            ).fetchone()["id"]
            self.connection.execute(
                "INSERT OR IGNORE INTO memberships(user_id,tenant_id,role) VALUES(?,?,?)",
                (actual_id, tenant_id, role),
            )

    def get_user(self, username):
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
            if row is None:
                return None
            return {**dict(row), "memberships": [dict(item) for item in
                self.connection.execute(
                    "SELECT tenant_id,role FROM memberships WHERE user_id=?", (row["id"],)
                ).fetchall()]}

    def audit(self, tenant_id, actor, action, resource_id, payload):
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO audit_events(tenant_id,actor,action,resource_id,payload_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (tenant_id, actor, action, resource_id,
                 json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )

    def close(self):
        with self._lock:
            self.connection.close()
