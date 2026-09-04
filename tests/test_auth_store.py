from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "video-call"))

from auth_store import AuthStore  # noqa: E402


class AuthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "auth.db"
        self.store = AuthStore(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_password_and_session_secrets_are_not_stored_in_plaintext(self) -> None:
        password = "correct horse battery staple"
        user = self.store.create_user(
            username="geo",
            password=password,
            display_name="Geo",
            role="admin",
            only_if_empty=True,
        )
        authenticated = self.store.authenticate(
            username="geo",
            password=password,
            device_name="MacBook",
        )
        self.assertIsNotNone(authenticated)
        assert authenticated is not None
        signed_in_user, token, _expires_at = authenticated
        self.assertEqual(signed_in_user["id"], user["id"])
        self.assertTrue(token.startswith("rbs_"))
        self.assertIsNone(
            self.store.authenticate(username="GEO", password="wrong password value")
        )
        self.assertIsNotNone(
            self.store.authenticate(username="GEO", password=password)
        )

        with sqlite3.connect(self.path) as connection:
            password_hash = connection.execute(
                "SELECT password_hash FROM auth_users WHERE id = ?", (user["id"],)
            ).fetchone()[0]
            token_hash = connection.execute(
                "SELECT token_hash FROM auth_sessions"
            ).fetchone()[0]
        self.assertNotIn(password, password_hash)
        self.assertNotEqual(token, token_hash)
        session = self.store.session_for_token(token)
        self.assertEqual(session["user"]["username"], "geo")
        self.assertTrue(self.store.revoke_session(session["session_id"]))
        self.assertIsNone(self.store.session_for_token(token))

    def test_bootstrap_is_single_use_and_disabled_user_loses_sessions(self) -> None:
        user = self.store.create_user(
            username="owner",
            password="very secure passphrase",
            role="admin",
            only_if_empty=True,
        )
        with self.assertRaises(RuntimeError):
            self.store.create_user(
                username="second",
                password="another secure passphrase",
                only_if_empty=True,
            )
        authenticated = self.store.authenticate(
            username="owner",
            password="very secure passphrase",
        )
        assert authenticated is not None
        _record, token, _expires = authenticated
        self.store.create_user(
            username="backup-admin",
            password="another secure passphrase",
            role="admin",
        )
        self.store.set_user_disabled(user["id"], True)
        self.assertIsNone(self.store.session_for_token(token))

    def test_first_registered_user_can_become_admin_and_last_admin_is_protected(self) -> None:
        first = self.store.create_user(
            username="Geo",
            password="first secure passphrase",
            role="user",
            admin_if_empty=True,
        )
        self.assertEqual(first["role"], "admin")
        with self.assertRaises(RuntimeError):
            self.store.set_user_role(first["id"], "user")
        with self.assertRaises(RuntimeError):
            self.store.set_user_disabled(first["id"], True)

        second = self.store.create_user(
            username="second-admin",
            password="second secure passphrase",
            role="admin",
        )
        demoted = self.store.set_user_role(first["id"], "user")
        self.assertEqual(demoted["role"], "user")
        authenticated = self.store.authenticate(
            username="second-admin",
            password="second secure passphrase",
        )
        assert authenticated is not None
        _user, _token, _expires = authenticated
        self.assertEqual(self.store.active_session_counts()[second["id"]], 1)
        self.assertEqual(self.store.revoke_user_sessions(second["id"]), 1)


if __name__ == "__main__":
    unittest.main()
