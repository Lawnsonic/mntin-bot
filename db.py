"""
db.py. Multi-wallet encrypted storage with target tracking.

Each user can have multiple wallets (generated or imported). Private keys
are encrypted at rest using Fernet symmetric encryption with a master key
from .env. SQLite backing store. Includes automatic migration from the
legacy single-wallet schema.
"""

import os
import sqlite3
from typing import Optional, List, Dict

from cryptography.fernet import Fernet
from web3 import Account, Web3
from dotenv import load_dotenv

load_dotenv()

ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise ValueError(
        "ENCRYPTION_KEY missing from .env. Generate one with:\n"
        '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    )

cipher_suite = Fernet(ENCRYPTION_KEY.encode())

# DATA_DIR lets you point the DB at a mounted persistent volume (e.g. on
# Railway). Falls back to the project directory for local dev.
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "users.db")


# ============================================================================
# SCHEMA & MIGRATION
# ============================================================================

def init_db():
    """Create tables and migrate legacy data if present."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                wallet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                address TEXT NOT NULL,
                encrypted_key TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'generated',
                UNIQUE(user_id, address)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                target_address TEXT NOT NULL,
                UNIQUE(user_id, target_address)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sniper_settings (
                user_id INTEGER PRIMARY KEY,
                sniper_active INTEGER NOT NULL DEFAULT 0,
                dry_run INTEGER NOT NULL DEFAULT 1,
                gas_bump_percent INTEGER NOT NULL DEFAULT 30,
                daily_limit_eth REAL NOT NULL DEFAULT 0.05,
                price_mode TEXT NOT NULL DEFAULT 'FREE_ONLY',
                max_mint_price_eth REAL NOT NULL DEFAULT 0.01
            )
        """)
        try:
            conn.execute("ALTER TABLE sniper_settings ADD COLUMN daily_limit_eth REAL NOT NULL DEFAULT 0.05")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE sniper_settings ADD COLUMN price_mode TEXT NOT NULL DEFAULT 'FREE_ONLY'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE sniper_settings ADD COLUMN max_mint_price_eth REAL NOT NULL DEFAULT 0.01")
        except sqlite3.OperationalError:
            pass
        _migrate_legacy(conn)


def _migrate_legacy(conn):
    """Migrate rows from the old single-wallet 'users' table into 'wallets'."""
    try:
        rows = conn.execute(
            "SELECT user_id, address, encrypted_key FROM users"
        ).fetchall()
    except sqlite3.OperationalError:
        return  # no legacy table, nothing to migrate

    if not rows:
        return

    for user_id, address, encrypted_key in rows:
        existing = conn.execute(
            "SELECT 1 FROM wallets WHERE user_id = ? AND address = ?",
            (user_id, address),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO wallets (user_id, label, address, encrypted_key, source) "
                "VALUES (?, 'W1', ?, ?, 'generated')",
                (user_id, address, encrypted_key),
            )

    conn.execute("DROP TABLE users")
    conn.commit()


# ============================================================================
# WALLET MANAGEMENT
# ============================================================================

def _next_label(conn, user_id: int) -> str:
    """Generate the next wallet label (W1, W2, W3...) for a user."""
    count = conn.execute(
        "SELECT COUNT(*) FROM wallets WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    return f"W{count + 1}"


def create_wallet(user_id: int) -> dict:
    """
    Generate a brand new wallet. Returns dict with wallet_id, label,
    address, and private_key (plaintext, for one-time display only).
    """
    with sqlite3.connect(DB_PATH) as conn:
        acct = Account.create()
        # Always store with 0x prefix for consistency with imported keys
        hex_key = "0x" + acct.key.hex()
        enc_key = cipher_suite.encrypt(hex_key.encode()).decode()
        label = _next_label(conn, user_id)

        cur = conn.execute(
            "INSERT INTO wallets (user_id, label, address, encrypted_key, source) "
            "VALUES (?, ?, ?, ?, 'generated')",
            (user_id, label, acct.address, enc_key),
        )
        conn.commit()

        return {
            "wallet_id": cur.lastrowid,
            "label": label,
            "address": acct.address,
            "private_key": hex_key,
        }


def import_wallet(user_id: int, private_key: str) -> dict:
    """
    Import an existing wallet (e.g. one already whitelisted for a mint).
    Validates the key, deduplicates by address, stores encrypted.
    """
    key = private_key.strip()
    if key.startswith("0x"):
        hex_part = key[2:]
    else:
        hex_part = key
        key = "0x" + key

    if len(hex_part) != 64:
        raise ValueError(
            f"Expected 64 hex characters, got {len(hex_part)}. "
            "Check for a stray character from copying."
        )
    if not all(c in "0123456789abcdefABCDEF" for c in hex_part):
        raise ValueError("Key contains a non-hex character.")

    account = Web3().eth.account.from_key(key)

    with sqlite3.connect(DB_PATH) as conn:
        existing = conn.execute(
            "SELECT wallet_id, label FROM wallets WHERE user_id = ? AND address = ?",
            (user_id, account.address),
        ).fetchone()
        if existing:
            return {
                "wallet_id": existing[0],
                "label": existing[1],
                "address": account.address,
                "already_existed": True,
            }

        enc_key = cipher_suite.encrypt(key.encode()).decode()
        label = _next_label(conn, user_id)

        cur = conn.execute(
            "INSERT INTO wallets (user_id, label, address, encrypted_key, source) "
            "VALUES (?, ?, ?, ?, 'imported')",
            (user_id, label, account.address, enc_key),
        )
        conn.commit()

        return {
            "wallet_id": cur.lastrowid,
            "label": label,
            "address": account.address,
            "already_existed": False,
        }


def get_wallets(user_id: int) -> List[dict]:
    """
    Address-only listing, no keys decrypted.
    Use this everywhere the private key is not needed.
    """
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT wallet_id, label, address, source FROM wallets "
            "WHERE user_id = ? ORDER BY wallet_id",
            (user_id,),
        ).fetchall()
        return [
            {"wallet_id": r[0], "label": r[1], "address": r[2], "source": r[3]}
            for r in rows
        ]


def get_wallet_by_id(wallet_id: int, user_id: int) -> Optional[dict]:
    """
    Full wallet with decrypted key. Scoped by wallet_id AND user_id together
    so one user can never fetch another user's wallet by guessing an ID.
    """
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT label, address, encrypted_key FROM wallets "
            "WHERE wallet_id = ? AND user_id = ?",
            (wallet_id, user_id),
        ).fetchone()
        if row:
            return {
                "label": row[0],
                "address": row[1],
                "private_key": cipher_suite.decrypt(row[2].encode()).decode(),
            }
        return None


def get_first_wallet_id(user_id: int) -> Optional[int]:
    """Return the wallet_id of the user's first wallet, or None."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT wallet_id FROM wallets WHERE user_id = ? ORDER BY wallet_id LIMIT 1",
            (user_id,),
        ).fetchone()
        return row[0] if row else None


# ============================================================================
# TARGET TRACKING
# ============================================================================

def add_target(user_id: int, target_address: str):
    """Add a wallet address to track for copy-trading."""
    target = target_address.lower().strip()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO targets (user_id, target_address) VALUES (?, ?)",
            (user_id, target),
        )
        conn.commit()


def remove_target(user_id: int, target_address: str):
    """Remove a tracked target wallet."""
    target = target_address.lower().strip()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM targets WHERE user_id = ? AND target_address = ?",
            (user_id, target),
        )
        conn.commit()


def get_user_targets(user_id: int) -> List[str]:
    """Get all target addresses for a user."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT target_address FROM targets WHERE user_id = ?", (user_id,)
        ).fetchall()
        return [r[0] for r in rows]


def get_all_active_targets() -> Dict[str, List[int]]:
    """
    Build a routing map: {target_address: [user_id_1, user_id_2, ...]}.
    Used by the sniper to fan out detected mints to all subscribed users.
    """
    mapping: Dict[str, List[int]] = {}
    with sqlite3.connect(DB_PATH) as conn:
        for target, user_id in conn.execute(
            "SELECT target_address, user_id FROM targets"
        ).fetchall():
            mapping.setdefault(target, []).append(user_id)
    return mapping


# ============================================================================
# SNIPER SETTINGS PERSISTENCE
# ============================================================================

def get_sniper_settings(user_id: int) -> dict:
    """Load persisted sniper settings. Returns defaults if no row exists."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT sniper_active, dry_run, gas_bump_percent, daily_limit_eth, price_mode, max_mint_price_eth "
            "FROM sniper_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return {
                "sniper_active": False,
                "dry_run": True,
                "gas_bump_percent": 30,
                "daily_limit_eth": 0.05,
                "price_mode": "FREE_ONLY",
                "max_mint_price_eth": 0.01,
            }
        return {
            "sniper_active": bool(row[0]),
            "dry_run": bool(row[1]),
            "gas_bump_percent": row[2],
            "daily_limit_eth": float(row[3]) if row[3] is not None else 0.05,
            "price_mode": str(row[4]) if row[4] is not None else "FREE_ONLY",
            "max_mint_price_eth": float(row[5]) if row[5] is not None else 0.01,
        }


def update_sniper_settings(user_id: int, **fields) -> None:
    """Upsert sniper settings. Only updates the fields you pass."""
    existing = get_sniper_settings(user_id)
    existing.update(fields)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO sniper_settings (user_id, sniper_active, dry_run, gas_bump_percent, daily_limit_eth, price_mode, max_mint_price_eth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "sniper_active=excluded.sniper_active, "
            "dry_run=excluded.dry_run, "
            "gas_bump_percent=excluded.gas_bump_percent, "
            "daily_limit_eth=excluded.daily_limit_eth, "
            "price_mode=excluded.price_mode, "
            "max_mint_price_eth=excluded.max_mint_price_eth",
            (
                user_id,
                int(existing["sniper_active"]),
                int(existing["dry_run"]),
                existing["gas_bump_percent"],
                float(existing["daily_limit_eth"]),
                str(existing["price_mode"]),
                float(existing["max_mint_price_eth"]),
            ),
        )
        conn.commit()


# Auto-initialize on import
init_db()
