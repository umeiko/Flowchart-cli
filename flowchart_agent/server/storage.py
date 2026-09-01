from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
              password_hash TEXT NOT NULL, created_at TEXT NOT NULL,
              avatar BLOB, avatar_mime TEXT
            );
            CREATE TABLE IF NOT EXISTS auth_tokens (
              token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_sessions (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              title TEXT NOT NULL, engine TEXT NOT NULL, verification_mode TEXT NOT NULL,
              style TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              context_summary TEXT, context_cutoff_id INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_agent_sessions_user_updated
              ON agent_sessions(user_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
              role TEXT NOT NULL, content TEXT NOT NULL, attachments TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id);
            CREATE TABLE IF NOT EXISTS resource_mounts (
              session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
              kind TEXT NOT NULL CHECK(kind IN ('skills', 'styles')),
              name TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(session_id, kind, name)
            );
            """)
            # Existing 1.5.0 databases predate user avatars.  Keep startup
            # migrations additive so an intranet deployment can update in place.
            user_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
            }
            if "avatar" not in user_columns:
                db.execute("ALTER TABLE users ADD COLUMN avatar BLOB")
            if "avatar_mime" not in user_columns:
                db.execute("ALTER TABLE users ADD COLUMN avatar_mime TEXT")
            session_columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(agent_sessions)").fetchall()
            }
            if "context_summary" not in session_columns:
                db.execute("ALTER TABLE agent_sessions ADD COLUMN context_summary TEXT")
            if "context_cutoff_id" not in session_columns:
                db.execute(
                    "ALTER TABLE agent_sessions ADD COLUMN context_cutoff_id INTEGER NOT NULL DEFAULT 0"
                )
            db.execute("PRAGMA optimize")

    @staticmethod
    def hash_password(password: str, salt: bytes | None = None) -> str:
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return f"scrypt${salt.hex()}${digest.hex()}"

    @classmethod
    def verify_password(cls, password: str, encoded: str) -> bool:
        try:
            _, salt_hex, expected = encoded.split("$", 2)
            actual = cls.hash_password(password, bytes.fromhex(salt_hex)).split("$", 2)[2]
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    def create_user(self, username: str, password: str) -> dict:
        username = username.strip()
        if len(username) < 3 or len(password) < 8:
            raise ValueError("用户名至少 3 个字符，密码至少 8 个字符")
        user = {
            "id": f"usr_{uuid.uuid4().hex}", "username": username,
            "created_at": _now(), "avatar_url": None,
        }
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?)", (
                    user["id"], username, self.hash_password(password), user["created_at"]
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名已存在") from exc
        return user

    def authenticate(self, username: str, password: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()
        if row and self.verify_password(password, row["password_hash"]):
            return {
                "id": row["id"], "username": row["username"],
                "avatar_url": "/v1/auth/avatar" if row["avatar"] is not None else None,
            }
        return None

    def issue_token(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO auth_tokens VALUES (?, ?, ?)", (token_hash, user_id, expires))
        return token

    def user_for_token(self, token: str | None) -> dict | None:
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("""
              SELECT users.id, users.username, users.avatar FROM auth_tokens
              JOIN users ON users.id=auth_tokens.user_id
              WHERE token_hash=? AND expires_at>?
            """, (digest, _now())).fetchone()
        if not row:
            return None
        return {
            "id": row["id"], "username": row["username"],
            "avatar_url": "/v1/auth/avatar" if row["avatar"] is not None else None,
        }

    def user_avatar(self, user_id: str) -> tuple[bytes, str] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT avatar,avatar_mime FROM users WHERE id=?", (user_id,)
            ).fetchone()
        if not row or row["avatar"] is None:
            return None
        return bytes(row["avatar"]), row["avatar_mime"] or "application/octet-stream"

    def set_user_avatar(
        self, user_id: str, content: bytes | None, media_type: str | None = None
    ) -> None:
        with self.connect() as db:
            result = db.execute(
                "UPDATE users SET avatar=?,avatar_mime=? WHERE id=?",
                (content, media_type, user_id),
            )
        if not result.rowcount:
            raise KeyError(user_id)

    def revoke_token(self, token: str | None):
        if token:
            with self.connect() as db:
                db.execute("DELETE FROM auth_tokens WHERE token_hash=?", (
                    hashlib.sha256(token.encode()).hexdigest(),
                ))

    def save_session(self, session_id: str, user_id: str, title: str, engine: str, mode: str, style=None):
        now = _now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO agent_sessions"
                "(id,user_id,title,engine,verification_mode,style,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (session_id, user_id, title, engine, mode, style, now, now),
            )

    def sessions(self, user_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM agent_sessions WHERE user_id=? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def session(self, session_id: str, user_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM agent_sessions WHERE id=? AND user_id=?", (session_id, user_id)).fetchone()
        return dict(row) if row else None

    def session_by_id(self, session_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def rename_session(self, session_id: str, user_id: str, title: str):
        with self.connect() as db:
            result = db.execute("UPDATE agent_sessions SET title=?, updated_at=? WHERE id=? AND user_id=?",
                                (title.strip()[:80] or "未命名会话", _now(), session_id, user_id))
        if not result.rowcount:
            raise KeyError(session_id)

    def delete_session(self, session_id: str, user_id: str):
        with self.connect() as db:
            result = db.execute("DELETE FROM agent_sessions WHERE id=? AND user_id=?", (session_id, user_id))
        if not result.rowcount:
            raise KeyError(session_id)

    def add_message(self, session_id: str, role: str, content: str, attachments: str = "[]"):
        with self.connect() as db:
            db.execute("INSERT INTO messages(session_id,role,content,attachments,created_at) VALUES(?,?,?,?,?)",
                       (session_id, role, content, attachments, _now()))
            db.execute("UPDATE agent_sessions SET updated_at=? WHERE id=?", (_now(), session_id))

    def messages(self, session_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,role,content,attachments,created_at FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        return [dict(row) for row in rows]

    def context_messages(self, session_id: str) -> tuple[str | None, list[dict]]:
        with self.connect() as db:
            session = db.execute(
                "SELECT context_summary,context_cutoff_id FROM agent_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise KeyError(session_id)
            rows = db.execute(
                "SELECT id,role,content,attachments,created_at FROM messages "
                "WHERE session_id=? AND id>? ORDER BY id",
                (session_id, session["context_cutoff_id"] or 0),
            ).fetchall()
        return session["context_summary"], [dict(row) for row in rows]

    def save_context_summary(
        self, session_id: str, summary: str, retained_messages: int
    ) -> None:
        with self.connect() as db:
            ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM messages WHERE session_id=? ORDER BY id",
                    (session_id,),
                ).fetchall()
            ]
            cutoff = ids[-retained_messages - 1] if len(ids) > retained_messages else 0
            db.execute(
                "UPDATE agent_sessions SET context_summary=?,context_cutoff_id=?,updated_at=? "
                "WHERE id=?",
                (summary, cutoff, _now(), session_id),
            )

    def resource_mounts(self, session_id: str) -> dict[str, set[str]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT kind,name FROM resource_mounts WHERE session_id=?",
                (session_id,),
            ).fetchall()
        result = {"skills": set(), "styles": set()}
        for row in rows:
            result[row["kind"]].add(row["name"])
        return result

    def set_resource_mount(
        self, session_id: str, kind: str, name: str, mounted: bool
    ) -> None:
        with self.connect() as db:
            if kind == "styles" and mounted:
                db.execute(
                    "DELETE FROM resource_mounts WHERE session_id=? AND kind='styles'",
                    (session_id,),
                )
            if mounted:
                db.execute(
                    "INSERT OR REPLACE INTO resource_mounts VALUES (?, ?, ?, ?)",
                    (session_id, kind, name, _now()),
                )
            else:
                db.execute(
                    "DELETE FROM resource_mounts WHERE session_id=? AND kind=? AND name=?",
                    (session_id, kind, name),
                )
