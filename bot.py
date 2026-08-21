"""
bot.py. Bloom-style interactive multi-user Telegram bot.

Button-driven UI with step-by-step wizards. Multi-wallet per user,
multi-chain support, variable mint fee detection, copy-mint settings panel
with DB persistence, Robinhood FCFS trigger. Blue menu bar.

Default network: Robinhood Chain (chain ID 4663).
"""

import os
import re
import json
import asyncio
import time
import logging
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional, Set

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

import db
from mint_engine import (
    get_balance,
    execute_withdraw,
    execute_mint,
    execute_copy_mint,
    detect_mint_price,
    parse_mint_quantity,
    MINT_SIGNATURES,
)
from robinhood_sniper import prepare_mint_tx, wait_for_mint_open, broadcast_via_all

load_dotenv()

logger = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in environment variables.")

# Comma-separated Telegram user IDs allowed to use /restore_db.
# Set this in Railway variables, e.g. ADMIN_USER_IDS=123456789,987654321
ADMIN_USER_IDS = {
    int(uid.strip())
    for uid in os.getenv("ADMIN_USER_IDS", "").split(",")
    if uid.strip().isdigit()
}

NETWORKS = {
    "robinhood": {
        "name": "Robinhood",
        "rpc": os.getenv("ROBINHOOD_RPC", "https://rpc.mainnet.chain.robinhood.com"),
        "ws": os.getenv("ROBINHOOD_WS_RPC", ""),
        "icon": "\U0001f7e2",
    },
    "arb": {
        "name": "Arbitrum",
        "rpc": os.getenv("ARB_RPC", "https://arb1.arbitrum.io/rpc"),
        "ws": os.getenv("ARB_WS_RPC", ""),
        "icon": "\U0001f537",
    },
    "base": {
        "name": "Base",
        "rpc": os.getenv("BASE_RPC", "https://mainnet.base.org"),
        "ws": os.getenv("BASE_WS_RPC", ""),
        "icon": "\U0001f7e6",
    },
    "eth": {
        "name": "Ethereum",
        "rpc": os.getenv("ETH_RPC", "https://eth.llamarpc.com"),
        "ws": os.getenv("ETH_WS_RPC", ""),
        "icon": "\U0001f48e",
    },
}

# Robinhood multi-RPC blast targets (comma-separated in .env)
ROBINHOOD_RPCS = [
    r.strip()
    for r in os.getenv(
        "ROBINHOOD_RPCS",
        os.getenv("ROBINHOOD_RPC", "https://rpc.mainnet.chain.robinhood.com"),
    ).split(",")
    if r.strip()
]

DEFAULT_MAX_BASE_FEE_GWEI = int(os.getenv("MAX_BASE_FEE_GWEI", "100"))
DEFAULT_DAILY_LIMIT_ETH = float(os.getenv("DAILY_LIMIT_ETH", "0.05"))

# ============================================================================
# PER-USER STATE
# ============================================================================

user_locks = defaultdict(asyncio.Lock)


def _default_user_state() -> dict:
    return {
        "network": "robinhood",
        "mode": "MANUAL",
        "active_wallet_id": None,
        "menu_message_id": None,
        "cached_balances": {},
        "balances_updated_at": 0,
        # Wizard step tracking
        "step": None,
        "withdraw_amount": None,
        # Sniper settings (loaded from DB on menu open, written back on toggle)
        "sniper_active": False,
        "dry_run": True,
        "daily_limit_eth": DEFAULT_DAILY_LIMIT_ETH,
        "max_base_fee_gwei": DEFAULT_MAX_BASE_FEE_GWEI,
        "gas_bump_percent": 30,
        "max_priority_gwei": 50,
        # Sniper tracking
        "seen_txs": set(),
        "daily_spent_eth": 0.0,
        "daily_reset_time": time.time(),
        "successful_copies": 0,
        "failed_copies": 0,
        "sniper_task": None,
    }


user_states = defaultdict(_default_user_state)


# ============================================================================
# UTILITY FUNCTIONS & CACHED BALANCES
# ============================================================================

def _get_rpc(user_id: int) -> str:
    return NETWORKS[user_states[user_id]["network"]]["rpc"]


async def _run_blocking(fn, *args):
    """Run a blocking function off the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


def _get_active_wallet_id(user_id: int) -> Optional[int]:
    """Resolve the active wallet_id. Auto-selects first wallet if not set."""
    state = user_states[user_id]
    if state["active_wallet_id"] is not None:
        return state["active_wallet_id"]
    first = db.get_first_wallet_id(user_id)
    if first:
        state["active_wallet_id"] = first
    return first


def _get_active_address(user_id: int) -> Optional[str]:
    """Get the address of the active wallet without decrypting the key."""
    wallet_id = _get_active_wallet_id(user_id)
    if wallet_id is None:
        return None
    for w in db.get_wallets(user_id):
        if w["wallet_id"] == wallet_id:
            return w["address"]
    return None


async def _fetch_balances_cached(user_id: int, address: str, force_refresh: bool = False) -> dict:
    """Returns cached balances if younger than 60 seconds, otherwise updates concurrently."""
    state = user_states[user_id]
    now = time.time()

    if not force_refresh and (now - state["balances_updated_at"] < 60) and state["cached_balances"]:
        return state["cached_balances"]

    if not address or address == "No wallet created yet":
        return {k: 0.0 for k in NETWORKS}

    net_keys = list(NETWORKS.keys())
    balance_tasks = [
        _run_blocking(get_balance, NETWORKS[k]["rpc"], address)
        for k in net_keys
    ]
    results = await asyncio.gather(*balance_tasks, return_exceptions=True)

    balances = {}
    for key, res in zip(net_keys, results):
        balances[key] = res if isinstance(res, float) else 0.0

    state["cached_balances"] = balances
    state["balances_updated_at"] = now
    return balances


def _check_daily_limit(user_id: int, amount_eth: float) -> bool:
    state = user_states[user_id]
    if time.time() - state["daily_reset_time"] > 86400:
        state["daily_spent_eth"] = 0.0
        state["daily_reset_time"] = time.time()
    return (state["daily_spent_eth"] + amount_eth) <= state["daily_limit_eth"]


def _is_seen(user_id: int, tx_hash: str) -> bool:
    state = user_states[user_id]
    seen: Set[str] = state["seen_txs"]
    if tx_hash in seen:
        return True
    seen.add(tx_hash)
    if len(seen) > 10000:
        state["seen_txs"] = set(list(seen)[-5000:])
    return False


async def _delete_after(chat_id: int, message_id: int, delay: int, context):
    """Background task to delete a message after delay seconds."""
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def _back_button(target: str = "nav_home") -> list:
    return [InlineKeyboardButton("\u00ab Back", callback_data=target)]


async def _delete_old_menu(user_id: int, chat_id: int, bot):
    """Delete the previous dashboard message if tracked."""
    state = user_states[user_id]
    old_id = state.get("menu_message_id")
    if old_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old_id)
        except Exception:
            pass
        state["menu_message_id"] = None


async def _send_home(user_id: int, chat_id: int, context, via_message=False, update=None, force_refresh=False):
    """Send a fresh dashboard, deleting the old one first."""
    state = user_states[user_id]
    state["step"] = None

    await _delete_old_menu(user_id, chat_id, context.bot)

    text, markup = await _build_home(user_id, force_refresh=force_refresh)

    if via_message and update and update.message:
        msg = await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        msg = await context.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=markup, parse_mode="Markdown"
        )

    state["menu_message_id"] = msg.message_id


async def _update_menu_message(user_id, chat_id, context, text, markup):
    """Edit the tracked menu message in-place, or send a new one if it's gone."""
    state = user_states[user_id]
    msg_id = state.get("menu_message_id")
    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id,
                text=text, reply_markup=markup, parse_mode="Markdown",
            )
            return
        except Exception:
            pass
    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=markup, parse_mode="Markdown"
    )
    state["menu_message_id"] = msg.message_id


# ============================================================================
# HOME DASHBOARD
# ============================================================================

async def _build_home(user_id: int, force_refresh: bool = False) -> tuple:
    """Build the home dashboard text and keyboard. Uses 15s cache for instant responses."""
    state = user_states[user_id]
    wallets = db.get_wallets(user_id)
    active_id = _get_active_wallet_id(user_id)

    active_label = "None"
    active_addr = "No wallet created yet"
    for w in wallets:
        if w["wallet_id"] == active_id:
            active_label = w["label"]
            active_addr = w["address"]
            break

    balances = await _fetch_balances_cached(user_id, active_addr, force_refresh=force_refresh)

    bal_lines = []
    net_keys = list(NETWORKS.keys())
    for i, key in enumerate(net_keys):
        net = NETWORKS[key]
        connector = "\u2514" if i == len(net_keys) - 1 else "\u251c"
        bal_lines.append(f"{connector} {net['icon']} {key.upper()}: `{balances.get(key, 0.0):.4f} ETH`")

    bal_block = "\n".join(bal_lines)
    net_name = NETWORKS[state["network"]]["name"]
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")

    text = (
        f"*Mntin Bot Dashboard*\n\n"
        f"\U0001f535 *Active Wallet:* {active_label}\n"
        f"\u2514 `{active_addr}`\n\n"
        f"\U0001f4b0 *Balances:*\n"
        f"{bal_block}\n\n"
        f"\u2699\ufe0f *Settings:*\n"
        f"\u251c Active Chain: *{net_name}*\n"
        f"\u2514 Mint Mode: *{state['mode']}*\n\n"
        f"Paste any contract address to mint.\n\n"
        f"\U0001f552 Updated: `{now} UTC`"
    )

    keyboard = [
        [
            InlineKeyboardButton("\U0001f4e5 Deposit", callback_data="menu_deposit"),
            InlineKeyboardButton("\U0001f4b8 Withdraw", callback_data="menu_withdraw"),
        ],
        [
            InlineKeyboardButton(
                f"\U0001f310 Chain: {net_name}", callback_data="menu_network"
            ),
            InlineKeyboardButton(
                f"\u2699\ufe0f Mint: {state['mode'].capitalize()}", callback_data="toggle_mode"
            ),
        ],
        [
            InlineKeyboardButton("\U0001f3af Copy Mint", callback_data="menu_sniper"),
            InlineKeyboardButton("\U0001f510 Wallets", callback_data="menu_wallets"),
        ],
        [InlineKeyboardButton("\U0001f504 Refresh Balances", callback_data="refresh_home")],
    ]

    return text, InlineKeyboardMarkup(keyboard)


# ============================================================================
# ENTRY POINT: /start and /home
# ============================================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    state = user_states[user_id]
    state["step"] = None

    # Load persisted sniper settings into memory
    settings = db.get_sniper_settings(user_id)
    state.update(settings)

    # Auto-provision W1 on first ever interaction
    if not db.get_wallets(user_id):
        async with user_locks[user_id]:
            created = db.create_wallet(user_id)
        state["active_wallet_id"] = created["wallet_id"]

        msg = await update.message.reply_text(
            f"Welcome to Mntin Bot. {created['label']} has been created.\n\n"
            f"`{created['address']}`\n\n"
            f"Private key:\n`{created['private_key']}`\n\n"
            f"Save this now. This message deletes in 5 minutes "
            f"and the key will not be shown again here.\n"
            f"Use the Wallets menu to import or create more.",
            parse_mode="Markdown",
        )
        asyncio.create_task(
            _delete_after(chat_id, msg.message_id, 300, context)
        )

    await _send_home(user_id, chat_id, context, via_message=True, update=update, force_refresh=True)


# ============================================================================
# QUICK COMMAND HANDLERS (blue menu bar)
# ============================================================================

async def wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await _delete_old_menu(user_id, chat_id, context.bot)
    text = _build_wallet_text(user_id)
    keyboard = [
        [
            InlineKeyboardButton("Create New", callback_data="wallet_create"),
            InlineKeyboardButton("Import", callback_data="wallet_import"),
        ],
        [InlineKeyboardButton("Switch Active", callback_data="wallet_switch")],
        [InlineKeyboardButton("Reveal Key", callback_data="wallet_reveal")],
        _back_button(),
    ]
    msg = await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )
    user_states[user_id]["menu_message_id"] = msg.message_id


async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await _delete_old_menu(user_id, chat_id, context.bot)
    addr = _get_active_address(user_id)
    if not addr:
        await update.message.reply_text("No wallet found. Use /start to create one.")
        return
    net = NETWORKS[user_states[user_id]["network"]]
    text = (
        f"\U0001f4e5 *Deposit Funds*\n\n"
        f"Send ETH to your execution wallet:\n"
        f"`{addr}`\n\n"
        f"Chain: *{net['name']}*\n"
        f"Funds arrive instantly and are ready to mint."
    )
    msg = await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup([_back_button()]), parse_mode="Markdown"
    )
    user_states[user_id]["menu_message_id"] = msg.message_id


async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    state = user_states[user_id]
    await _delete_old_menu(user_id, chat_id, context.bot)
    addr = _get_active_address(user_id)
    if not addr:
        await update.message.reply_text("No wallet found. Use /start to create one.")
        return
    net_key = state["network"]
    bal = state.get("cached_balances", {}).get(net_key)
    if bal is None:
        bal = await _run_blocking(get_balance, NETWORKS[net_key]["rpc"], addr)
    state["step"] = "WITHDRAW_AMOUNT"
    text = (
        f"\U0001f4b8 *Withdraw Funds*\n\n"
        f"Chain: *{NETWORKS[net_key]['name']}*\n"
        f"Available: `{bal:.4f} ETH`\n\n"
        f"Enter the amount to withdraw (numbers only):"
    )
    msg = await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup([_back_button()]), parse_mode="Markdown"
    )
    state["menu_message_id"] = msg.message_id


async def network_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    state = user_states[user_id]
    await _delete_old_menu(user_id, chat_id, context.bot)
    buttons = []
    for key, net in NETWORKS.items():
        marker = "\u2705 " if state["network"] == key else ""
        buttons.append([
            InlineKeyboardButton(
                f"{marker}{net['icon']} {net['name']}",
                callback_data=f"set_net_{key}",
            )
        ])
    buttons.append(_back_button())
    msg = await update.message.reply_text(
        "\U0001f310 *Select Active Chain:*",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    state["menu_message_id"] = msg.message_id


async def targets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias /targets -> copy mint settings panel."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    state = user_states[user_id]
    await _delete_old_menu(user_id, chat_id, context.bot)

    # Load persisted settings
    settings = db.get_sniper_settings(user_id)
    state.update(settings)
    state["step"] = "ADD_TARGET"

    text = _build_sniper_text(user_id)
    markup = _build_sniper_keyboard(user_id)
    msg = await update.message.reply_text(
        text, reply_markup=markup, parse_mode="Markdown"
    )
    state["menu_message_id"] = msg.message_id


async def restore_db_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin-only. One-time DB restore path: arm the bot, then send users.db
    as a Telegram document in the next message. Intended for migrating an
    existing users.db onto a fresh deploy's persistent volume, without
    needing SSH/CLI access to the host.
    """
    user_id = update.effective_user.id
    if not ADMIN_USER_IDS:
        await update.message.reply_text(
            "ADMIN_USER_IDS is not configured. Set it in your environment "
            "variables before using this command."
        )
        return
    if user_id not in ADMIN_USER_IDS:
        await update.message.reply_text("Not authorized.")
        return

    user_states[user_id]["step"] = "AWAITING_DB_RESTORE"
    await update.message.reply_text(
        "\u26a0\ufe0f *DB Restore Armed*\n\n"
        "Send `users.db` as a file attachment now to overwrite the live "
        "database. The current file will be backed up first. Send /cancel "
        "to abort.",
        parse_mode="Markdown",
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives users.db when a restore is armed via /restore_db."""
    user_id = update.effective_user.id
    state = user_states[user_id]

    if state.get("step") != "AWAITING_DB_RESTORE" or user_id not in ADMIN_USER_IDS:
        return  # ignore unsolicited documents

    state["step"] = None
    doc = update.message.document

    tg_file = await context.bot.get_file(doc.file_id)
    incoming_path = os.path.join(db.DATA_DIR, "_incoming_users.db")
    await tg_file.download_to_drive(incoming_path)

    # Sanity check: real SQLite files start with this 16-byte header.
    with open(incoming_path, "rb") as f:
        header = f.read(16)
    if header != b"SQLite format 3\x00":
        os.remove(incoming_path)
        await update.message.reply_text(
            "\u274c That file does not look like a SQLite database. "
            "Restore aborted, nothing was overwritten."
        )
        return

    async with user_locks[user_id]:
        if os.path.exists(db.DB_PATH):
            backup_path = db.DB_PATH + f".bak.{int(time.time())}"
            os.replace(db.DB_PATH, backup_path)
        else:
            backup_path = None
        os.replace(incoming_path, db.DB_PATH)

    try:
        db.init_db()
        with __import__("sqlite3").connect(db.DB_PATH) as conn:
            wallet_count = conn.execute("SELECT COUNT(*) FROM wallets").fetchone()[0]
    except Exception as e:
        await update.message.reply_text(f"\u26a0\ufe0f Restored, but verification failed: {e}")
        return

    msg = f"\u2705 Restored. `{wallet_count}` wallet row(s) found."
    if backup_path:
        msg += f"\nPrevious file backed up to `{os.path.basename(backup_path)}`."
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels any pending wizard step, including an armed DB restore."""
    user_states[update.effective_user.id]["step"] = None
    await update.message.reply_text("Cancelled.")


# ============================================================================
# CALLBACK ROUTER
# ============================================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data
    state = user_states[user_id]

    # -------------------------------------------------------------- nav home
    if data in ("nav_home", "refresh_home"):
        state["step"] = None
        force = (data == "refresh_home")
        text, markup = await _build_home(user_id, force_refresh=force)
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode="Markdown"
        )
        state["menu_message_id"] = query.message.message_id
        return

    # ----------------------------------------------------------- toggle mode
    if data == "toggle_mode":
        state["mode"] = "AUTO" if state["mode"] == "MANUAL" else "MANUAL"
        text, markup = await _build_home(user_id, force_refresh=False)
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode="Markdown"
        )
        return

    # -------------------------------------------------------------- deposit
    if data == "menu_deposit":
        addr = _get_active_address(user_id)
        if not addr:
            await query.edit_message_text(
                "No wallet found. Use the Wallets menu to create one.",
                reply_markup=InlineKeyboardMarkup([_back_button()]),
            )
            return
        net = NETWORKS[state["network"]]
        text = (
            f"\U0001f4e5 *Deposit Funds*\n\n"
            f"Send ETH to your execution wallet:\n"
            f"`{addr}`\n\n"
            f"Chain: *{net['name']}*\n"
            f"Funds arrive instantly and are ready to mint."
        )
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([_back_button()]),
            parse_mode="Markdown",
        )
        return

    # ------------------------------------------------------------ withdraw
    if data == "menu_withdraw":
        addr = _get_active_address(user_id)
        if not addr:
            await query.edit_message_text(
                "No wallet found. Create one first.",
                reply_markup=InlineKeyboardMarkup([_back_button()]),
            )
            return
        net_key = state["network"]
        bal = state.get("cached_balances", {}).get(net_key)
        if bal is None:
            bal = await _run_blocking(get_balance, NETWORKS[net_key]["rpc"], addr)
        state["step"] = "WITHDRAW_AMOUNT"
        text = (
            f"\U0001f4b8 *Withdraw Funds*\n\n"
            f"Chain: *{NETWORKS[net_key]['name']}*\n"
            f"Available: `{bal:.4f} ETH`\n\n"
            f"Enter the amount to withdraw (numbers only):"
        )
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([_back_button()]),
            parse_mode="Markdown",
        )
        return

    # -------------------------------------------------------- network picker
    if data == "menu_network":
        buttons = []
        for key, net in NETWORKS.items():
            marker = "\u2705 " if state["network"] == key else ""
            buttons.append([
                InlineKeyboardButton(
                    f"{marker}{net['icon']} {net['name']}",
                    callback_data=f"set_net_{key}",
                )
            ])
        buttons.append(_back_button())
        await query.edit_message_text(
            "\U0001f310 *Select Active Chain:*",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return

    if data.startswith("set_net_"):
        chosen = data.replace("set_net_", "")
        if chosen in NETWORKS:
            state["network"] = chosen
        text, markup = await _build_home(user_id, force_refresh=False)
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode="Markdown"
        )
        return

    # --------------------------------------------------------- wallet menu
    if data == "menu_wallets":
        text = _build_wallet_text(user_id)
        keyboard = [
            [
                InlineKeyboardButton("Create New", callback_data="wallet_create"),
                InlineKeyboardButton("Import", callback_data="wallet_import"),
            ],
            [InlineKeyboardButton("Switch Active", callback_data="wallet_switch")],
            [InlineKeyboardButton("Reveal Key", callback_data="wallet_reveal")],
            _back_button(),
        ]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    if data == "wallet_create":
        async with user_locks[user_id]:
            created = db.create_wallet(user_id)
        state["active_wallet_id"] = created["wallet_id"]
        msg = await query.message.reply_text(
            f"{created['label']} created.\n"
            f"`{created['address']}`\n\n"
            f"Private key:\n`{created['private_key']}`\n\n"
            f"Save this now. This message deletes in 5 minutes.",
            parse_mode="Markdown",
        )
        asyncio.create_task(
            _delete_after(query.message.chat_id, msg.message_id, 300, context)
        )
        # Refresh wallet menu on the existing message
        text = _build_wallet_text(user_id)
        keyboard = [
            [
                InlineKeyboardButton("Create New", callback_data="wallet_create"),
                InlineKeyboardButton("Import", callback_data="wallet_import"),
            ],
            [InlineKeyboardButton("Switch Active", callback_data="wallet_switch")],
            [InlineKeyboardButton("Reveal Key", callback_data="wallet_reveal")],
            _back_button(),
        ]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    if data == "wallet_import":
        state["step"] = "IMPORT_KEY"
        await query.edit_message_text(
            "\U0001f510 *Import Wallet*\n\n"
            "Paste the private key you want to import.\n"
            "Your message will be deleted the moment this bot reads it.",
            reply_markup=InlineKeyboardMarkup([
                _back_button("menu_wallets")
            ]),
            parse_mode="Markdown",
        )
        return

    if data == "wallet_switch":
        wallets = db.get_wallets(user_id)
        if not wallets:
            await query.edit_message_text(
                "No wallets yet. Create or import one first.",
                reply_markup=InlineKeyboardMarkup([_back_button("menu_wallets")]),
            )
            return
        active_id = _get_active_wallet_id(user_id)
        buttons = []
        for w in wallets:
            marker = "\u2705 " if w["wallet_id"] == active_id else ""
            buttons.append([
                InlineKeyboardButton(
                    f"{marker}{w['label']} [{w['source']}]",
                    callback_data=f"set_wallet_{w['wallet_id']}",
                )
            ])
        buttons.append(_back_button("menu_wallets"))
        await query.edit_message_text(
            "\U0001f510 *Select Active Wallet:*",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return

    if data.startswith("set_wallet_"):
        wid = int(data.replace("set_wallet_", ""))
        state["active_wallet_id"] = wid
        text, markup = await _build_home(user_id, force_refresh=False)
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode="Markdown"
        )
        return

    if data == "wallet_reveal":
        wallet_id = _get_active_wallet_id(user_id)
        if not wallet_id:
            await query.message.reply_text("No wallet selected.")
            return
        wallet = db.get_wallet_by_id(wallet_id, user_id)
        if not wallet:
            await query.message.reply_text("Wallet not found.")
            return
        msg = await query.message.reply_text(
            f"\U0001f511 *{wallet['label']} Private Key:*\n"
            f"`{wallet['private_key']}`\n\n"
            f"This message deletes in 5 minutes.",
            parse_mode="Markdown",
        )
        asyncio.create_task(
            _delete_after(query.message.chat_id, msg.message_id, 300, context)
        )
        return

    # ------------------------------------------------ copy mint settings menu
    if data == "menu_sniper":
        # Load persisted settings from DB
        settings = db.get_sniper_settings(user_id)
        state.update(settings)
        state["step"] = "ADD_TARGET"
        await query.edit_message_text(
            _build_sniper_text(user_id),
            reply_markup=_build_sniper_keyboard(user_id),
            parse_mode="Markdown",
        )
        return

    if data == "toggle_sniper_active":
        state["sniper_active"] = not state["sniper_active"]
        db.update_sniper_settings(user_id, sniper_active=state["sniper_active"])
        await query.edit_message_text(
            _build_sniper_text(user_id),
            reply_markup=_build_sniper_keyboard(user_id),
            parse_mode="Markdown",
        )
        return

    if data == "toggle_sniper_dryrun":
        state["dry_run"] = not state["dry_run"]
        db.update_sniper_settings(user_id, dry_run=state["dry_run"])
        await query.edit_message_text(
            _build_sniper_text(user_id),
            reply_markup=_build_sniper_keyboard(user_id),
            parse_mode="Markdown",
        )
        return

    if data.startswith("set_bump_"):
        val = data.replace("set_bump_", "")
        if val == "custom":
            state["step"] = "AWAITING_CUSTOM_BUMP"
            await query.edit_message_text(
                "Reply with a custom gas bump percentage (0 to 500):",
                reply_markup=InlineKeyboardMarkup([_back_button("menu_sniper")]),
            )
            return
        state["gas_bump_percent"] = int(val)
        db.update_sniper_settings(user_id, gas_bump_percent=state["gas_bump_percent"])
        await query.edit_message_text(
            _build_sniper_text(user_id),
            reply_markup=_build_sniper_keyboard(user_id),
            parse_mode="Markdown",
        )
        return

    if data == "set_daily_limit_prompt":
        state["step"] = "AWAITING_DAILY_LIMIT"
        cap = state.get("daily_limit_eth", DEFAULT_DAILY_LIMIT_ETH)
        await query.edit_message_text(
            f"\U0001f4b0 *Set Daily ETH Limit*\n\n"
            f"Current limit: `{cap} ETH`\n\n"
            f"Reply with your desired daily spend limit in ETH (e.g. 0.1, 1.5):",
            reply_markup=InlineKeyboardMarkup([_back_button("menu_sniper")]),
            parse_mode="Markdown",
        )
        return

    if data.startswith("rm_target_"):
        target = data.replace("rm_target_", "")
        db.remove_target(user_id, target)
        await query.edit_message_text(
            _build_sniper_text(user_id),
            reply_markup=_build_sniper_keyboard(user_id),
            parse_mode="Markdown",
        )
        return

    # --------------------------------------------------- mint flow callbacks
    if data == "qty_custom":
        state["step"] = "AWAITING_CUSTOM_QTY"
        await query.edit_message_text(
            "Reply with custom quantity to mint (1 to 1000):",
            reply_markup=InlineKeyboardMarkup([_back_button("refresh_mint_view")]),
        )
        return

    if data == "refresh_mint_view":
        state["step"] = None
        contract = context.user_data.get("target_contract")
        qty = context.user_data.get("selected_qty", 1)
        gas = context.user_data.get("selected_gas", 2)
        price = context.user_data.get("selected_price", 0.0)
        net_name = NETWORKS[state["network"]]["name"]
        await query.edit_message_text(
            f"\U0001f3af *Mint Configuration*\n\n"
            f"Contract: `{contract}`\n"
            f"Detected Price: `{price} ETH`\n"
            f"Network: *{net_name}*\n\n"
            f"Configure and confirm:",
            reply_markup=_build_mint_keyboard(qty, gas, price, state["network"]),
            parse_mode="Markdown",
        )
        return

    if data.startswith("qty_"):
        context.user_data["selected_qty"] = int(data.split("_")[1])
        await _refresh_mint_keyboard(query, context, user_id)
        return

    if data.startswith("gas_"):
        context.user_data["selected_gas"] = int(data.split("_")[1])
        await _refresh_mint_keyboard(query, context, user_id)
        return

    if data.startswith("price_"):
        context.user_data["selected_price"] = float(data.split("_")[1])
        await _refresh_mint_keyboard(query, context, user_id)
        return

    if data == "confirm_mint":
        contract = context.user_data.get("target_contract")
        if not contract:
            await query.edit_message_text("No contract address set. Paste one first.")
            return
        qty = context.user_data.get("selected_qty", 1)
        gas = context.user_data.get("selected_gas", 2)
        price = context.user_data.get("selected_price", 0.0)
        await query.edit_message_text(
            f"\u23f3 Simulating transaction for `{contract}`...",
            parse_mode="Markdown",
        )
        await _dispatch_mint(
            user_id, query.message.chat_id, contract, qty, price, gas, context
        )
        return

    # ---------------------------------------- Robinhood FCFS snipe trigger
    if data == "confirm_rbh_snipe":
        contract = context.user_data.get("target_contract")
        if not contract:
            await query.edit_message_text("No contract address set. Paste one first.")
            return
        qty = context.user_data.get("selected_qty", 1)
        price = context.user_data.get("selected_price", 0.0)
        await query.edit_message_text(
            f"\U0001f552 *FCFS Sniper Armed*\n\n"
            f"Contract: `{contract}`\n"
            f"Monitoring for bytecode deployment across {len(ROBINHOOD_RPCS)} RPC(s).\n"
            f"Transaction is pre-signed and will blast the moment the contract is live.\n\n"
            f"\u26a0\ufe0f *Warning:* Uses wallet balance/nonce at armed time - do not send other transactions from this wallet before it fires.\n\n"
            f"Timeout: 10 minutes.",
            parse_mode="Markdown",
        )
        asyncio.create_task(
            _dispatch_rbh_sniper(
                user_id, query.message.chat_id, contract, qty, price, context
            )
        )
        return


# ============================================================================
# VIEW BUILDERS
# ============================================================================

def _build_wallet_text(user_id: int) -> str:
    wallets = db.get_wallets(user_id)
    active_id = _get_active_wallet_id(user_id)
    if not wallets:
        return "\U0001f510 *Wallet Manager*\n\nNo wallets yet. Create or import one."

    lines = ["\U0001f510 *Wallet Manager*", ""]
    for i, w in enumerate(wallets):
        marker = " (active)" if w["wallet_id"] == active_id else ""
        connector = "\u2514" if i == len(wallets) - 1 else "\u251c"
        lines.append(f"{connector} *{w['label']}* [{w['source']}]{marker}")
        lines.append(f"  `{w['address']}`")
    return "\n".join(lines)


def _build_sniper_text(user_id: int) -> str:
    """Build the copy mint settings + target list panel."""
    state = user_states[user_id]
    targets = db.get_user_targets(user_id)

    status = "ACTIVE (Running)" if state["sniper_active"] else "INACTIVE (Stopped)"
    mode = "DRY RUN (Simulation Only)" if state["dry_run"] else "LIVE (Real Transactions)"
    cap = state.get("daily_limit_eth", DEFAULT_DAILY_LIMIT_ETH)
    spent = state.get("daily_spent_eth", 0.0)

    lines = [
        "\U0001f3af *Copy Mint Settings*",
        "",
        f"\u251c Status: *{status}*",
        f"\u251c Execution: *{mode}*",
        f"\u251c Daily Cap: *{cap} ETH* (Spent: `{spent:.4f} ETH`)",
        f"\u2514 Gas Bump: *+{state['gas_bump_percent']}%*",
        "",
        "*Tracked Targets:*",
    ]

    if targets:
        for i, t in enumerate(targets):
            connector = "\u2514" if i == len(targets) - 1 else "\u251c"
            lines.append(f"{connector} `{t}`")
    else:
        lines.append("\u2514 None")

    lines.extend(["", "Paste a target wallet address to add it."])
    return "\n".join(lines)


def _build_sniper_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Copy mint control panel with toggles, gas bump presets, daily limit, and per-target remove buttons."""
    state = user_states[user_id]
    targets = db.get_user_targets(user_id)
    bump = state["gas_bump_percent"]
    is_custom = bump not in (30, 50)
    custom_label = f"\u2705 {bump}%" if is_custom else "Custom"
    cap = state.get("daily_limit_eth", DEFAULT_DAILY_LIMIT_ETH)

    keyboard = [
        [
            InlineKeyboardButton(
                f"Copy Mint: {'ON (Turn Off)' if state['sniper_active'] else 'OFF (Turn On)'}",
                callback_data="toggle_sniper_active",
            ),
        ],
        [
            InlineKeyboardButton(
                f"Execution: {'Dry Run' if state['dry_run'] else 'LIVE'}",
                callback_data="toggle_sniper_dryrun",
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'\u2705 ' if bump == 30 else ''}30%",
                callback_data="set_bump_30",
            ),
            InlineKeyboardButton(
                f"{'\u2705 ' if bump == 50 else ''}50%",
                callback_data="set_bump_50",
            ),
            InlineKeyboardButton(custom_label, callback_data="set_bump_custom"),
        ],
        [
            InlineKeyboardButton(
                f"\U0001f4b0 Daily Limit: {cap} ETH",
                callback_data="set_daily_limit_prompt",
            )
        ],
    ]

    # Per-target remove buttons
    for t in targets:
        short = t[:6] + "..." + t[-4:]
        keyboard.append([
            InlineKeyboardButton(f"\U0001f5d1 Remove {short}", callback_data=f"rm_target_{t}")
        ])

    keyboard.append(_back_button())
    return InlineKeyboardMarkup(keyboard)


# ============================================================================
# MINT KEYBOARD BUILDER & REFRESH
# ============================================================================

def _build_mint_keyboard(qty: int, gas: int, price: float, network_key: str) -> InlineKeyboardMarkup:
    is_custom_qty = qty not in (1, 2, 5)
    custom_qty_label = f"\u2705 {qty}" if is_custom_qty else "Custom"

    keyboard = [
        [
            InlineKeyboardButton(f"{'\u2705 ' if qty == 1 else ''}1", callback_data="qty_1"),
            InlineKeyboardButton(f"{'\u2705 ' if qty == 2 else ''}2", callback_data="qty_2"),
            InlineKeyboardButton(f"{'\u2705 ' if qty == 5 else ''}5", callback_data="qty_5"),
            InlineKeyboardButton(custom_qty_label, callback_data="qty_custom"),
        ],
        [
            InlineKeyboardButton(f"{'\u2705 ' if price == 0.0 else ''}Free", callback_data="price_0.0"),
            InlineKeyboardButton(f"{'\u2705 ' if price == 0.01 else ''}0.01", callback_data="price_0.01"),
            InlineKeyboardButton(f"{'\u2705 ' if price == 0.05 else ''}0.05", callback_data="price_0.05"),
        ],
    ]

    # Gas priority buttons only for fee-market chains (not Robinhood FCFS)
    if network_key != "robinhood":
        keyboard.append([
            InlineKeyboardButton(f"{'\u2705 ' if gas == 2 else ''}2 Gwei", callback_data="gas_2"),
            InlineKeyboardButton(f"{'\u2705 ' if gas == 5 else ''}5 Gwei", callback_data="gas_5"),
            InlineKeyboardButton(f"{'\u2705 ' if gas == 10 else ''}10 Gwei", callback_data="gas_10"),
        ])

    keyboard.append([InlineKeyboardButton("\u26a1 Confirm & Mint", callback_data="confirm_mint")])

    # Robinhood exclusive: FCFS pre-sign snipe
    if network_key == "robinhood":
        keyboard.append([
            InlineKeyboardButton(
                "\U0001f552 FCFS Snipe (Wait for Deploy)",
                callback_data="confirm_rbh_snipe",
            )
        ])

    keyboard.append(_back_button())
    return InlineKeyboardMarkup(keyboard)


async def _refresh_mint_keyboard(query, context, user_id):
    qty = context.user_data.get("selected_qty", 1)
    gas = context.user_data.get("selected_gas", 2)
    price = context.user_data.get("selected_price", 0.0)
    network_key = user_states[user_id]["network"]
    await query.edit_message_reply_markup(
        reply_markup=_build_mint_keyboard(qty, gas, price, network_key)
    )


# ============================================================================
# MINT DISPATCHER
# ============================================================================

async def _dispatch_mint(
    user_id: int,
    chat_id: int,
    contract_address: str,
    qty: int,
    price: float,
    gas_gwei: int,
    context: ContextTypes.DEFAULT_TYPE,
):
    wallet_id = _get_active_wallet_id(user_id)
    if not wallet_id:
        await context.bot.send_message(
            chat_id=chat_id, text="No wallet found. Create one first."
        )
        return
    wallet = db.get_wallet_by_id(wallet_id, user_id)
    if not wallet:
        await context.bot.send_message(
            chat_id=chat_id, text="Wallet not found."
        )
        return

    rpc_url = _get_rpc(user_id)
    max_base_fee = user_states[user_id]["max_base_fee_gwei"]

    async with user_locks[user_id]:
        try:
            tx_hash = await _run_blocking(
                execute_mint, rpc_url, wallet["private_key"],
                contract_address, qty, price, gas_gwei, max_base_fee,
            )
            net_name = NETWORKS[user_states[user_id]["network"]]["name"]
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"\U0001f680 *Mint Broadcasted*\n\n"
                    f"Network: *{net_name}*\n"
                    f"TX: `{tx_hash}`"
                ),
                parse_mode="Markdown",
            )
        except RuntimeError as e:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"\u274c *Mint Aborted (Simulation Failed)*\n\n"
                    f"`{str(e)}`\n\n"
                    f"No transaction was broadcast. Zero gas spent."
                ),
                parse_mode="Markdown",
            )
        except ValueError as e:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"\u26a0\ufe0f *Invalid Target*\n{str(e)}",
                parse_mode="Markdown",
            )
        except Exception as e:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"\U0001f6a8 *Error:*\n`{str(e)}`",
                parse_mode="Markdown",
            )


# ============================================================================
# ROBINHOOD FCFS DISPATCHER
# ============================================================================

async def _dispatch_rbh_sniper(
    user_id: int,
    chat_id: int,
    contract_address: str,
    qty: int,
    price: float,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Background task: pre-sign, wait for deploy across all RPCs, blast."""
    wallet_id = _get_active_wallet_id(user_id)
    wallet = db.get_wallet_by_id(wallet_id, user_id)
    if not wallet:
        await context.bot.send_message(
            chat_id=chat_id, text="Wallet not found. Cannot arm sniper."
        )
        return

    rpc_url = NETWORKS["robinhood"]["rpc"]

    try:
        # Pre-sign (off the event loop)
        tx_payload = await _run_blocking(
            prepare_mint_tx, rpc_url, wallet["private_key"],
            contract_address, qty, price,
        )

        # Block until contract is live (10 min timeout) across all Robinhood RPCs
        is_live = await wait_for_mint_open(ROBINHOOD_RPCS, contract_address)

        if is_live:
            tx_hash = await broadcast_via_all(ROBINHOOD_RPCS, tx_payload["raw"])
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"\U0001f680 *FCFS Snipe Executed*\n\n"
                    f"Target went live. Payload blasted via {len(ROBINHOOD_RPCS)} RPC(s).\n"
                    f"TX: `{tx_hash}`"
                ),
                parse_mode="Markdown",
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="\u23f3 *FCFS Snipe Timeout*\nContract did not deploy within 10 minutes.",
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.exception("FCFS sniper failed for user %s, contract %s", user_id, contract_address)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"\U0001f6a8 *Sniper Error*\n`{str(e)}`",
            parse_mode="Markdown",
        )


# ============================================================================
# TEXT INPUT ROUTER
# ============================================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = user_states[user_id]
    chat_id = update.effective_chat.id

    # ------------------------------------------------ wallet import flow
    if state["step"] == "IMPORT_KEY":
        raw_key = text
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=update.message.message_id,
            )
        except Exception:
            pass

        state["step"] = None

        async with user_locks[user_id]:
            try:
                result = db.import_wallet(user_id, raw_key)
            except ValueError as e:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Import failed: {str(e)}",
                )
                await _send_home(user_id, chat_id, context)
                return
            except Exception:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="That does not look like a valid private key. Import cancelled.",
                )
                await _send_home(user_id, chat_id, context)
                return

        if result["already_existed"]:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Already imported as {result['label']}.\n`{result['address']}`",
                parse_mode="Markdown",
            )
        else:
            state["active_wallet_id"] = result["wallet_id"]
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"Imported as {result['label']}.\n"
                    f"`{result['address']}`\n\n"
                    f"The message with your key has been removed from this chat."
                ),
                parse_mode="Markdown",
            )
        await _send_home(user_id, chat_id, context)
        return

    # ---------------------------------------- custom gas bump input
    if state["step"] == "AWAITING_CUSTOM_BUMP":
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=update.message.message_id,
            )
        except Exception:
            pass

        try:
            bump = int(text)
            if not 0 <= bump <= 500:
                raise ValueError

            state["gas_bump_percent"] = bump
            state["step"] = "ADD_TARGET"
            db.update_sniper_settings(user_id, gas_bump_percent=bump)

            await _update_menu_message(
                user_id, chat_id, context,
                _build_sniper_text(user_id),
                _build_sniper_keyboard(user_id),
            )
        except ValueError:
            err = await update.message.reply_text(
                "Invalid percentage. Enter a whole number between 0 and 500."
            )
            asyncio.create_task(_delete_after(chat_id, err.message_id, 5, context))
        return

    # ---------------------------------------- custom daily limit input
    if state["step"] == "AWAITING_DAILY_LIMIT":
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=update.message.message_id,
            )
        except Exception:
            pass

        try:
            lim = float(text)
            if lim <= 0 or lim > 10000:
                raise ValueError

            state["daily_limit_eth"] = lim
            state["step"] = "ADD_TARGET"
            db.update_sniper_settings(user_id, daily_limit_eth=lim)

            await _update_menu_message(
                user_id, chat_id, context,
                _build_sniper_text(user_id),
                _build_sniper_keyboard(user_id),
            )
        except ValueError:
            err = await update.message.reply_text(
                "Invalid amount. Enter a positive number in ETH (e.g. 0.1 or 2.0):"
            )
            asyncio.create_task(_delete_after(chat_id, err.message_id, 5, context))
        return

    # ---------------------------------------- custom mint quantity input
    if state["step"] == "AWAITING_CUSTOM_QTY":
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=update.message.message_id,
            )
        except Exception:
            pass

        try:
            qty = int(text)
            if not 1 <= qty <= 1000:
                raise ValueError
            context.user_data["selected_qty"] = qty
            state["step"] = None

            contract = context.user_data.get("target_contract")
            gas = context.user_data.get("selected_gas", 2)
            price = context.user_data.get("selected_price", 0.0)
            net_name = NETWORKS[state["network"]]["name"]

            if state.get("menu_message_id"):
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=state["menu_message_id"],
                        text=(
                            f"\U0001f3af *Mint Configuration*\n\n"
                            f"Contract: `{contract}`\n"
                            f"Detected Price: `{price} ETH`\n"
                            f"Network: *{net_name}*\n\n"
                            f"Configure and confirm:"
                        ),
                        reply_markup=_build_mint_keyboard(qty, gas, price, state["network"]),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
        except ValueError:
            err = await update.message.reply_text(
                "Invalid quantity. Enter a whole number between 1 and 1000."
            )
            asyncio.create_task(_delete_after(chat_id, err.message_id, 5, context))
        return

    # ------------------------------------------ withdrawal wizard: amount
    if state["step"] == "WITHDRAW_AMOUNT":
        if not re.match(r"^\d+\.?\d*$", text):
            await update.message.reply_text(
                "Numbers only. Enter a valid amount (e.g. 0.05):"
            )
            return

        amt = float(text)
        if amt <= 0:
            await update.message.reply_text(
                "Amount must be greater than zero. Try again:"
            )
            return

        state["withdraw_amount"] = amt
        state["step"] = "WITHDRAW_ADDRESS"
        await update.message.reply_text(
            f"Amount: `{amt} ETH`\n\nNow paste the destination address:",
            parse_mode="Markdown",
        )
        return

    # ---------------------------------------- withdrawal wizard: address
    if state["step"] == "WITHDRAW_ADDRESS":
        if not re.match(r"^0x[a-fA-F0-9]{40}$", text):
            await update.message.reply_text(
                "Invalid address format. Paste a valid 0x address:"
            )
            return

        to_addr = text
        amt = state["withdraw_amount"]
        state["step"] = None

        wallet_id = _get_active_wallet_id(user_id)
        wallet = db.get_wallet_by_id(wallet_id, user_id) if wallet_id else None
        if not wallet:
            await update.message.reply_text("No wallet found.")
            return

        rpc_url = _get_rpc(user_id)
        msg = await update.message.reply_text(
            f"\u23f3 Broadcasting withdrawal of `{amt} ETH`...",
            parse_mode="Markdown",
        )

        async with user_locks[user_id]:
            try:
                tx_hash = await _run_blocking(
                    execute_withdraw, rpc_url,
                    wallet["private_key"], to_addr, amt,
                )
                await msg.edit_text(
                    f"\u2705 *Withdrawal Confirmed*\nTX: `{tx_hash}`",
                    parse_mode="Markdown",
                )
            except Exception as e:
                await msg.edit_text(
                    f"\u274c *Withdrawal Failed:*\n`{str(e)}`",
                    parse_mode="Markdown",
                )
        await _send_home(user_id, chat_id, context)
        return

    # ------------------------------------------- add target flow
    if state["step"] == "ADD_TARGET":
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=update.message.message_id,
            )
        except Exception:
            pass

        if re.match(r"^0x[a-fA-F0-9]{40}$", text):
            active_addr = _get_active_address(user_id)
            if active_addr and text.lower() == active_addr.lower():
                err = await update.message.reply_text(
                    "That is your own active wallet, not a target to copy."
                )
                asyncio.create_task(_delete_after(chat_id, err.message_id, 5, context))
                return

            db.add_target(user_id, text)
            # Refresh sniper panel in-place
            await _update_menu_message(
                user_id, chat_id, context,
                _build_sniper_text(user_id),
                _build_sniper_keyboard(user_id),
            )
            return
        return

    # ------------------------------------------- contract address (mint)
    if re.match(r"^0x[a-fA-F0-9]{40}$", text):
        addr = _get_active_address(user_id)
        if not addr:
            await update.message.reply_text(
                "No wallet found. Use /start to create one."
            )
            return

        context.user_data["target_contract"] = text
        rpc_url = _get_rpc(user_id)

        detected = await _run_blocking(detect_mint_price, rpc_url, text) or 0.0
        context.user_data["selected_qty"] = 1
        context.user_data["selected_gas"] = 2
        context.user_data["selected_price"] = detected

        network_key = state["network"]

        if state["mode"] == "AUTO":
            await update.message.reply_text(
                f"\u26a1 Auto Mode: simulating mint for `{text}` "
                f"at `{detected} ETH` per token...",
                parse_mode="Markdown",
            )
            await _dispatch_mint(
                user_id, chat_id,
                text, 1, detected, 2, context,
            )
            return

        # MANUAL mode: show mint config keyboard
        net_name = NETWORKS[network_key]["name"]
        await update.message.reply_text(
            f"\U0001f3af *Mint Configuration*\n\n"
            f"Contract: `{text}`\n"
            f"Detected Price: `{detected} ETH`\n"
            f"Network: *{net_name}*\n\n"
            f"Configure and confirm:",
            reply_markup=_build_mint_keyboard(1, 2, detected, network_key),
            parse_mode="Markdown",
        )
        return


# ============================================================================
# MEMPOOL SNIPER (per-user background worker, kept for direct invocation)
# ============================================================================

async def mempool_worker(user_id: int, chat_id: int, app):
    """WebSocket listener for a user's target wallets."""
    from websockets import connect

    state = user_states[user_id]
    net_key = state["network"]
    ws_url = NETWORKS[net_key].get("ws", "")

    if not ws_url:
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"No WebSocket RPC configured for {net_key.upper()}. Sniper cannot start.",
        )
        state["sniper_active"] = False
        return

    targets = db.get_user_targets(user_id)
    if not targets:
        await app.bot.send_message(
            chat_id=chat_id,
            text="No targets tracked. Add targets from the Copy Mint menu first.",
        )
        state["sniper_active"] = False
        return

    while state["sniper_active"]:
        try:
            async with connect(ws_url) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_subscribe",
                    "params": [
                        "alchemy_pendingTransactions",
                        {"fromAddress": targets},
                    ],
                }))

                await ws.recv()
                target_list = ", ".join(t[:8] + "..." for t in targets)
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"\U0001f3af *Copy Mint Listening*\n\n"
                        f"Targets: {target_list}\n"
                        f"Chain: *{net_key.upper()}*\n"
                        f"Mode: `{'DRY RUN' if state['dry_run'] else 'LIVE'}`"
                    ),
                    parse_mode="Markdown",
                )

                while state["sniper_active"]:
                    raw = await ws.recv()
                    msg_data = json.loads(raw)
                    if "params" not in msg_data or "result" not in msg_data["params"]:
                        continue

                    tx_data = msg_data["params"]["result"]
                    from web3 import Web3 as W3
                    if isinstance(tx_data, str):
                        w3 = W3(W3.HTTPProvider(_get_rpc(user_id)))
                        tx = w3.eth.get_transaction(tx_data)
                    else:
                        tx = tx_data

                    tx_hash = tx.get("hash")
                    if isinstance(tx_hash, bytes):
                        tx_hash = tx_hash.hex()
                    elif isinstance(tx_hash, str) and not tx_hash.startswith("0x"):
                        tx_hash = "0x" + tx_hash

                    if _is_seen(user_id, tx_hash):
                        continue

                    input_data = tx.get("input") or ""
                    if isinstance(input_data, bytes):
                        input_data = input_data.hex()
                    if len(input_data) < 10:
                        continue

                    func_sig = input_data[:10].lower()
                    if func_sig not in MINT_SIGNATURES:
                        continue

                    qty = parse_mint_quantity(input_data, func_sig)
                    val_eth = float(W3.from_wei(int(tx.get("value", 0)), "ether"))

                    if not _check_daily_limit(user_id, val_eth):
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"\u26a0\ufe0f Daily limit would be exceeded "
                                f"({state['daily_spent_eth']:.4f}/"
                                f"{state['daily_limit_eth']} ETH). Skipping."
                            ),
                        )
                        continue

                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"\U0001f3af *Mint Detected*\n\n"
                            f"Contract: `{tx.get('to')}`\n"
                            f"Qty: `{qty}` | Value: `{val_eth} ETH`\n"
                            f"Executing copy..."
                        ),
                        parse_mode="Markdown",
                    )

                    wallet_id = _get_active_wallet_id(user_id)
                    wallet = db.get_wallet_by_id(wallet_id, user_id) if wallet_id else None
                    if not wallet:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text="Wallet not found. Cannot execute.",
                        )
                        continue

                    rpc_url = _get_rpc(user_id)

                    async with user_locks[user_id]:
                        try:
                            result = await _run_blocking(
                                execute_copy_mint,
                                rpc_url, wallet["private_key"],
                                tx.get("to"), input_data,
                                qty, val_eth,
                                tx.get("gasPrice"),
                                tx.get("maxFeePerGas"),
                                tx.get("maxPriorityFeePerGas"),
                                state["gas_bump_percent"],
                                state["max_priority_gwei"],
                                state["max_base_fee_gwei"],
                                state["dry_run"],
                            )
                            state["successful_copies"] += 1
                            if not state["dry_run"]:
                                state["daily_spent_eth"] += val_eth
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=f"\u2705 *Result:*\n`{result}`",
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            state["failed_copies"] += 1
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=f"\u274c *Failed:*\n`{str(e)}`",
                                parse_mode="Markdown",
                            )

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Sniper reconnecting for user {user_id}: {e}")
            await asyncio.sleep(2)


# ============================================================================
# POST-INIT (blue menu bar + sniper listeners)
# ============================================================================

async def post_init(application):
    """Set the blue menu bar commands and start sniper listeners with shared locks."""
    commands = [
        BotCommand("start", "Launch Mntin Bot"),
        BotCommand("home", "Dashboard"),
        BotCommand("wallets", "Manage wallets"),
        BotCommand("deposit", "Show deposit address"),
        BotCommand("withdraw", "Withdraw funds"),
        BotCommand("network", "Switch chain"),
        BotCommand("targets", "Copy mint settings"),
    ]
    await application.bot.set_my_commands(commands)

    # Start multi-chain sniper listeners with shared user_locks
    try:
        from sniper import start_sniper_listeners
        start_sniper_listeners(user_states, user_locks)
    except Exception as e:
        print(f"Sniper listeners not started: {e}")


# ============================================================================
# MAIN
# ============================================================================

def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # Entry points
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("home", start_cmd))

    # Quick command handlers (blue menu bar)
    app.add_handler(CommandHandler("wallets", wallets_cmd))
    app.add_handler(CommandHandler("deposit", deposit_cmd))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd))
    app.add_handler(CommandHandler("network", network_cmd))
    app.add_handler(CommandHandler("targets", targets_cmd))

    # Admin-only one-time DB restore (see ADMIN_USER_IDS)
    app.add_handler(CommandHandler("restore_db", restore_db_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Buttons and text input
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()