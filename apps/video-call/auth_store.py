#!/usr/bin/env python3
"""Local multi-user identity and opaque session storage for RiverBank Edge."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
    from argon2.low_level import Type as Argon2Type
except ImportError:  # pragma: no cover - exercised on minimal Python builds
    PasswordHasher = None  # type: ignore[assignment]
    InvalidHashError = VerificationError = VerifyMismatchError = ValueError
    Argon2Type = None  # type: ignore[assignment]


DEFAULT_AUTH_DB = Path("/var/lib/riverbank-tasks/auth.db")
DEFAULT_REGISTRATION_PASSCODE_FILE = Path(
    "/home/geo/.config/riverbank-video-call/registration-passcode.hash"
)
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
PASSWORD_MIN_CHARS = 10
PASSWORD_MAX_CHARS = 128
REGISTRATION_PASSCODE_MAX_CHARS = 128
USERNAME_MIN_CHARS = 3
USERNAME_MAX_CHARS = 48
SCRYPT_LOG_N = 17
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 256 * 1024 * 1024
PBKDF2_ITERATIONS = 600_000
VALID_ROLES = {"admin", "user"}


def _argon2_hasher() -> Any | None:
    if PasswordHasher is None or Argon2Type is None:
        return None
    # OWASP minimum Argon2id profile: 19 MiB, two iterations, one lane.
    return PasswordHasher(
        time_cost=2,
        memory_cost=19 * 1024,
        parallelism=1,
        hash_len=32,
        salt_len=16,
        type=Argon2Type.ID,
    )


def now_epoch() -> float:
    return time.time()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def normalize_username(value: Any) -> tuple[str, str]:
    display = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not USERNAME_MIN_CHARS <= len(display) <= USERNAME_MAX_CHARS:
        raise ValueError(
            f"username must contain {USERNAME_MIN_CHARS}-{USERNAME_MAX_CHARS} characters"
        )
    if not all(character.isalnum() or character in "._-" for character in display):
        raise ValueError("username may contain letters, numbers, dots, dashes and underscores")
    return display, display.casefold()


def validate_password(password: Any, *, username: str = "") -> str:
    value = str(password or "")
    if not PASSWORD_MIN_CHARS <= len(value) <= PASSWORD_MAX_CHARS:
        raise ValueError(
            f"password must contain {PASSWORD_MIN_CHARS}-{PASSWORD_MAX_CHARS} characters"
        )
    if "\x00" in value:
        raise ValueError("password contains an invalid character")
    if username and value.casefold() == username.casefold():
        raise ValueError("password must not equal username")
    return value


def hash_password(password: str) -> str:
    argon2_hasher = _argon2_hasher()
    if argon2_hasher is not None:
        return str(argon2_hasher.hash(password))
    salt = secrets.token_bytes(16)
    if hasattr(hashlib, "scrypt"):
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=1 << SCRYPT_LOG_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            maxmem=SCRYPT_MAXMEM,
            dklen=32,
        )
        return "$".join(
            (
                "scrypt",
                str(SCRYPT_LOG_N),
                str(SCRYPT_R),
                str(SCRYPT_P),
                _b64encode(salt),
                _b64encode(digest),
            )
        )
    # Compatibility fallback for Python builds compiled without OpenSSL scrypt.
    # PBKDF2-HMAC-SHA256 at 600k iterations follows the OWASP fallback profile.
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=32,
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PBKDF2_ITERATIONS),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    if encoded.startswith("$argon2id$"):
        argon2_hasher = _argon2_hasher()
        if argon2_hasher is None:
            return False
        try:
            return bool(argon2_hasher.verify(encoded, password))
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False
    try:
        parts = encoded.split("$")
        algorithm = parts[0]
        if algorithm == "scrypt" and len(parts) == 6 and hasattr(hashlib, "scrypt"):
            _algorithm, log_n, r, p, salt, expected = parts
            digest = hashlib.scrypt(
                password.encode("utf-8"),
                salt=_b64decode(salt),
                n=1 << int(log_n),
                r=int(r),
                p=int(p),
                maxmem=SCRYPT_MAXMEM,
                dklen=len(_b64decode(expected)),
            )
        elif algorithm == "pbkdf2_sha256" and len(parts) == 4:
            _algorithm, iterations, salt, expected = parts
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode("utf-8"),
                _b64decode(salt),
                int(iterations),
                dklen=len(_b64decode(expected)),
            )
        else:
            return False
        return hmac.compare_digest(digest, _b64decode(expected))
    except (ValueError, TypeError, AttributeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RegistrationPasscodeGate:
    """Read-only verifier for a privately stored registration passcode hash."""

    def __init__(self, path: Path = DEFAULT_REGISTRATION_PASSCODE_FILE) -> None:
        self.path = Path(path)

    def _encoded_hash(self) -> str:
        try:
            if not self.path.is_file() or self.path.is_symlink():
                return ""
            if self.path.stat().st_size > 1024:
                return ""
            encoded = self.path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""
        if not encoded.startswith(("$argon2id$", "scrypt$", "pbkdf2_sha256$")):
            return ""
        return encoded

    def enabled(self) -> bool:
        return bool(self._encoded_hash())

    def verify(self, supplied: Any) -> bool:
        encoded = self._encoded_hash()
        value = str(supplied or "")
        if not encoded or not 1 <= len(value) <= REGISTRATION_PASSCODE_MAX_CHARS:
            return False
        return verify_password(value, encoded)


class AuthStore:
    """SQLite identity store; plaintext passwords and session tokens are never stored."""

    def __init__(self, path: Path = DEFAULT_AUTH_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    normalized_username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_login_at REAL
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    device_name TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL,
                    FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS auth_sessions_user
                    ON auth_sessions(user_id, expires_at DESC);
                CREATE INDEX IF NOT EXISTS auth_sessions_expiry
                    ON auth_sessions(expires_at);
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _user(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "disabled": bool(row["disabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_login_at": row["last_login_at"],
        }

    def has_users(self) -> bool:
        with self.connect() as connection:
            row = connection.execute("SELECT 1 FROM auth_users LIMIT 1").fetchone()
        return row is not None

    def create_user(
        self,
        *,
        username: Any,
        password: Any,
        display_name: Any = "",
        role: str = "user",
        only_if_empty: bool = False,
        admin_if_empty: bool = False,
    ) -> dict[str, Any]:
        clean_username, normalized = normalize_username(username)
        clean_password = validate_password(password, username=clean_username)
        clean_display = unicodedata.normalize("NFKC", str(display_name or "")).strip()
        if len(clean_display) > 80:
            raise ValueError("display name is too long")
        clean_role = str(role or "user").strip().lower()
        if clean_role not in VALID_ROLES:
            raise ValueError("invalid user role")
        timestamp = now_epoch()
        user_id = uuid.uuid4().hex
        encoded = hash_password(clean_password)
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                has_users = connection.execute(
                    "SELECT 1 FROM auth_users LIMIT 1"
                ).fetchone() is not None
                if only_if_empty and has_users:
                    raise RuntimeError("RiverBank account setup is already complete")
                if admin_if_empty and not has_users:
                    clean_role = "admin"
                connection.execute(
                    """
                    INSERT INTO auth_users (
                        id, username, normalized_username, display_name,
                        password_hash, role, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        clean_username,
                        normalized,
                        clean_display or clean_username,
                        encoded,
                        clean_role,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM auth_users WHERE id = ?", (user_id,)
                ).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("username is already in use") from exc
        assert row is not None
        return self._user(row)

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM auth_users ORDER BY created_at ASC"
            ).fetchall()
        return [self._user(row) for row in rows]

    def active_session_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, COUNT(*) AS count FROM auth_sessions
                WHERE revoked_at IS NULL AND expires_at > ?
                GROUP BY user_id
                """,
                (now_epoch(),),
            ).fetchall()
        return {str(row["user_id"]): int(row["count"]) for row in rows}

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM auth_users WHERE id = ?", (str(user_id),)
            ).fetchone()
        return self._user(row) if row is not None else None

    def authenticate(
        self,
        *,
        username: Any,
        password: Any,
        device_name: Any = "",
        ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> tuple[dict[str, Any], str, float] | None:
        try:
            _display, normalized = normalize_username(username)
        except ValueError:
            normalized = ""
        supplied_password = str(password or "")[:PASSWORD_MAX_CHARS]
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM auth_users WHERE normalized_username = ?",
                (normalized,),
            ).fetchone()
        if row is None or bool(row["disabled"]):
            # Keep nonexistent-user timing in the same memory-hard class.
            hash_password(supplied_password)
            return None
        if not verify_password(supplied_password, row["password_hash"]):
            return None
        timestamp = now_epoch()
        expires_at = timestamp + max(3600, min(int(ttl_seconds), SESSION_TTL_SECONDS))
        token = "rbs_" + secrets.token_urlsafe(32)
        session_id = uuid.uuid4().hex
        clean_device = unicodedata.normalize("NFKC", str(device_name or "")).strip()[:120]
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    id, user_id, token_hash, device_name,
                    created_at, last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    row["id"],
                    token_digest(token),
                    clean_device,
                    timestamp,
                    timestamp,
                    expires_at,
                ),
            )
            connection.execute(
                "UPDATE auth_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (timestamp, timestamp, row["id"]),
            )
        user = self.get_user(row["id"])
        assert user is not None
        return user, token, expires_at

    def session_for_token(self, token: Any) -> dict[str, Any] | None:
        supplied = str(token or "").strip()
        if not supplied.startswith("rbs_") or len(supplied) < 32:
            return None
        timestamp = now_epoch()
        digest = token_digest(supplied)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT s.id AS session_id, s.device_name, s.created_at AS session_created_at,
                       s.last_seen_at, s.expires_at, u.*
                FROM auth_sessions s
                JOIN auth_users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.revoked_at IS NULL
                      AND s.expires_at > ? AND u.disabled = 0
                """,
                (digest, timestamp),
            ).fetchone()
            if row is None:
                return None
            if timestamp - float(row["last_seen_at"]) >= 300:
                connection.execute(
                    "UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?",
                    (timestamp, row["session_id"]),
                )
        user = self._user(row)
        return {
            "session_id": row["session_id"],
            "device_name": row["device_name"],
            "expires_at": row["expires_at"],
            "user": user,
        }

    def revoke_session(self, session_id: str) -> bool:
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE auth_sessions SET revoked_at = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (now_epoch(), str(session_id)),
            )
        return result.rowcount > 0

    def revoke_user_sessions(self, user_id: str) -> int:
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE auth_sessions SET revoked_at = ?
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (now_epoch(), str(user_id)),
            )
        return int(result.rowcount)

    def set_user_role(self, user_id: str, role: Any) -> dict[str, Any] | None:
        clean_role = str(role or "").strip().lower()
        if clean_role not in VALID_ROLES:
            raise ValueError("invalid user role")
        timestamp = now_epoch()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM auth_users WHERE id = ?", (str(user_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            if row["role"] == "admin" and clean_role != "admin" and not row["disabled"]:
                other_admins = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM auth_users
                        WHERE role = 'admin' AND disabled = 0 AND id != ?
                        """,
                        (str(user_id),),
                    ).fetchone()[0]
                )
                if other_admins == 0:
                    connection.rollback()
                    raise RuntimeError("the last enabled administrator cannot be demoted")
            connection.execute(
                "UPDATE auth_users SET role = ?, updated_at = ? WHERE id = ?",
                (clean_role, timestamp, str(user_id)),
            )
            updated = connection.execute(
                "SELECT * FROM auth_users WHERE id = ?", (str(user_id),)
            ).fetchone()
            connection.commit()
        return self._user(updated) if updated is not None else None

    def change_password(
        self,
        *,
        user_id: str,
        current_password: Any,
        new_password: Any,
        keep_session_id: str = "",
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM auth_users WHERE id = ?", (str(user_id),)
            ).fetchone()
        if row is None or not verify_password(str(current_password or ""), row["password_hash"]):
            raise PermissionError("current password is incorrect")
        clean_password = validate_password(new_password, username=row["username"])
        timestamp = now_epoch()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE auth_users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (hash_password(clean_password), timestamp, row["id"]),
            )
            if keep_session_id:
                connection.execute(
                    """
                    UPDATE auth_sessions SET revoked_at = ?
                    WHERE user_id = ? AND id != ? AND revoked_at IS NULL
                    """,
                    (timestamp, row["id"], keep_session_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE auth_sessions SET revoked_at = ?
                    WHERE user_id = ? AND revoked_at IS NULL
                    """,
                    (timestamp, row["id"]),
                )

    def set_user_disabled(self, user_id: str, disabled: bool) -> dict[str, Any] | None:
        timestamp = now_epoch()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM auth_users WHERE id = ?", (str(user_id),)
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            if disabled and row["role"] == "admin" and not row["disabled"]:
                other_admins = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM auth_users
                        WHERE role = 'admin' AND disabled = 0 AND id != ?
                        """,
                        (str(user_id),),
                    ).fetchone()[0]
                )
                if other_admins == 0:
                    connection.rollback()
                    raise RuntimeError("the last enabled administrator cannot be disabled")
            result = connection.execute(
                "UPDATE auth_users SET disabled = ?, updated_at = ? WHERE id = ?",
                (int(bool(disabled)), timestamp, str(user_id)),
            )
            if result.rowcount and disabled:
                connection.execute(
                    """
                    UPDATE auth_sessions SET revoked_at = ?
                    WHERE user_id = ? AND revoked_at IS NULL
                    """,
                    (timestamp, str(user_id)),
                )
        return self.get_user(user_id) if result.rowcount else None

    def prune_sessions(self) -> int:
        with self.connect() as connection:
            result = connection.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                (now_epoch(),),
            )
        return result.rowcount
