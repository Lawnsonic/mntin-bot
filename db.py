"""
db.py — Encrypted wallet storage for multi-user bot.

Each user gets a generated wallet. The private key is encrypted at rest
using Fernet symmetric encryption with a master key from .env.
SQLite is the backing store — simple, zero-config, sufficient for this
scale (hundreds of users, not millions).
"""

import os
import sqlite3
from typing import Optional

from cryptography.fernet import Fernet
from web3 import Account
from dotenv import load_dotenv

load_dotenv()

ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise ValueError(
        "ENCRYPTION_KEY missing from .env — generate one with:\n"
        "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )

cipher_suite = Fernet(ENCRYPTION_KEY.encode())
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")


def init_db():
    """Create the users table if it doesn't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                address TEXT NOT NULL,
                encrypted_key TEXT NOT NULL
            )
        ''')


def get_address(user_id: int) -> Optional[str]:
    """
    Address-only lookup — never decrypts the key.
    Use this wherever the private key isn't needed (balance, deposit display).
    """
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT address FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row else None


def get_or_create_wallet(user_id: int) -> dict:
    """
    Returns {"address", "private_key", "created"}.

    created=True only on the call that actually generated the wallet —
    callers use that to decide whether to show the key at all.

    INSERT OR IGNORE closes the race condition: if two concurrent /wallet
    calls arrive for the same brand-new user, only the first write lands;
    the loser harmlessly re-reads it.
    """
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT address, encrypted_key FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row:
            return {
                "address": row[0],
                "private_key": cipher_suite.decrypt(row[1].encode()).decode(),
                "created": False,
            }

        # Plain key generation — no unaudited HD wallet path needed
        acct = Account.create()
        enc_key = cipher_suite.encrypt(acct.key.hex().encode()).decode()

        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, address, encrypted_key) VALUES (?, ?, ?)",
            (user_id, acct.address, enc_key),
        )
        conn.commit()

        # Re-read to handle the race: if our INSERT was ignored (another call
        # won), we'll get their address/key instead, which is correct.
        row = conn.execute(
            "SELECT address, encrypted_key FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return {
            "address": row[0],
            "private_key": cipher_suite.decrypt(row[1].encode()).decode(),
            "created": True,
        }


def get_user_wallet(user_id: int) -> Optional[dict]:
    """
    Full wallet including decrypted key.
    Use ONLY where the key is actually needed (signing transactions).
    """
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT address, encrypted_key FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row:
            return {
                "address": row[0],
                "private_key": cipher_suite.decrypt(row[1].encode()).decode(),
            }
        return None


# Auto-initialize on import
init_db()
