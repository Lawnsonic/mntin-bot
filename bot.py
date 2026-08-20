"""
bot.py. Bloom-style interactive multi-user Telegram bot.

Button-driven UI with step-by-step wizards. No slash commands needed
beyond /start and /home. Multi-wallet per user, multi-chain support,
variable mint fee detection, multi-target sniper tracking.

Default network: Robinhood Chain (chain ID 4663).
"""

import os
import re
import json
import asyncio
import time
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional, Set

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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

load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in environment variables.")

# Multi-chain registry with display metadata.
# Robinhood Chain (chain ID 4663) is its own L2 built on Arbitrum Orbit.
NETWORKS = {
    "robinhood": {
        "name": "Robinhood",
        "rpc": os.getenv("ROBINHOOD_RPC", "https://rpc.mainnet.chain.robinhood.com"),
        "ws": os.getenv("ROBINHOOD_WS_RPC", ""),
        "icon": "🟢",
    },
    "arb": {
        "name": "Arbitrum",
        "rpc": os.getenv("ARB_RPC", "https://arb1.arbitrum.io/rpc"),
        "ws": os.getenv("ARB_WS_RPC", ""),
        "icon": "🔷",
    },
    "base": {
        "name": "Base",
        "rpc": os.getenv("BASE_RPC", "https://mainnet.base.org"),
        "ws": os.getenv("BASE_WS_RPC", ""),
        "icon": "🟦",
    },
    "eth": {
        "name": "Ethereum",
        "rpc": os.getenv("ETH_RPC", "https://eth.llamarpc.com"),
        "ws": os.getenv("ETH_WS_RPC", ""),
        "icon": "💎",
    },
}

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
        # Wizard step tracking
        "step": None,
        "withdraw_amount": None,
        # Sniper settings
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
# UTILITY FUNCTIONS
# ============================================================================

def _get_rpc(user_id: int) -> str:
    return NETWORKS[user_states[user_id]["network"]]["rpc"]


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
    return [InlineKeyboardButton("« Back", callback_data=target)]


# ============================================================================
# HOME DASHBOARD
# ============================================================================

def _build_home(user_id: int) -> tuple:
    """Build the home dashboard text and keyboard."""
    state = user_states[user_id]
    wallets = db.get_wallets(user_id)
    active_id = _get_active_wallet_id(user_id)

    # Find active wallet info
    active_label = "None"
    active_addr = "No wallet created yet"
    for w in wallets:
        if w["wallet_id"] == active_id:
            active_label = w["label"]
            active_addr = w["address"]
            break

    # Multi-chain balance scan
    net_keys = list(NETWORKS.keys())
    bal_lines = []
    for i, key in enumerate(net_keys):
        net = NETWORKS[key]
        bal = get_balance(net["rpc"], active_addr) if active_addr != "No wallet created yet" else 0.0
        connector = "└" if i == len(net_keys) - 1 else "├"
        bal_lines.append(f"{connector} {net['icon']} {key.upper()}: `{bal:.4f} ETH`")

    bal_block = "\n".join(bal_lines)
    net_name = NETWORKS[state["network"]]["name"]
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")

    text = (
        f"🌸 *MintSuite Dashboard*\n\n"
        f"🔵 *Active Wallet:* {active_label}\n"
        f"└ `{active_addr}`\n\n"
        f"💰 *Balances:*\n"
        f"{bal_block}\n\n"
        f"⚙️ *Settings:*\n"
        f"├ Chain: *{net_name}*\n"
        f"└ Mode: *{state['mode']}*\n\n"
        f"Paste any contract address to mint.\n\n"
        f"🕒 Updated: `{now} UTC`"
    )

    keyboard = [
        [
            InlineKeyboardButton("📥 Deposit", callback_data="menu_deposit"),
            InlineKeyboardButton("💸 Withdraw", callback_data="menu_withdraw"),
        ],
        [
            InlineKeyboardButton(
                f"🌐 {state['network'].upper()}", callback_data="menu_network"
            ),
            InlineKeyboardButton(
                f"⚡ {state['mode']}", callback_data="toggle_mode"
            ),
        ],
        [
            InlineKeyboardButton("🎯 Copy Trade", callback_data="menu_targets"),
            InlineKeyboardButton("🔐 Wallets", callback_data="menu_wallets"),
        ],
        [InlineKeyboardButton("🔄 Refresh", callback_data="nav_home")],
    ]

    return text, InlineKeyboardMarkup(keyboard)


# ============================================================================
# ENTRY POINT: /start and /home
# ============================================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_states[user_id]
    state["step"] = None

    # Auto-provision W1 on first ever interaction
    if not db.get_wallets(user_id):
        async with user_locks[user_id]:
            created = db.create_wallet(user_id)
        state["active_wallet_id"] = created["wallet_id"]

        msg = await update.message.reply_text(
            f"Welcome to MintSuite. {created['label']} has been created.\n\n"
            f"`{created['address']}`\n\n"
            f"Private key:\n`{created['private_key']}`\n\n"
            f"Save this now. This message deletes in 5 minutes "
            f"and the key will not be shown again here.\n"
            f"Use the Wallets menu to import or create more.",
            parse_mode="Markdown",
        )
        asyncio.create_task(
            _delete_after(update.effective_chat.id, msg.message_id, 300, context)
        )

    text, markup = _build_home(user_id)
    await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


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
    if data == "nav_home":
        state["step"] = None
        text, markup = _build_home(user_id)
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode="Markdown"
        )
        return

    # ----------------------------------------------------------- toggle mode
    if data == "toggle_mode":
        state["mode"] = "AUTO" if state["mode"] == "MANUAL" else "MANUAL"
        text, markup = _build_home(user_id)
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
            f"📥 *Deposit Funds*\n\n"
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
        bal = get_balance(NETWORKS[net_key]["rpc"], addr)
        state["step"] = "WITHDRAW_AMOUNT"
        text = (
            f"💸 *Withdraw Funds*\n\n"
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
            marker = "✅ " if state["network"] == key else ""
            buttons.append([
                InlineKeyboardButton(
                    f"{marker}{net['icon']} {net['name']}",
                    callback_data=f"set_net_{key}",
                )
            ])
        buttons.append(_back_button())
        await query.edit_message_text(
            "🌐 *Select Active Chain:*",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return

    if data.startswith("set_net_"):
        chosen = data.replace("set_net_", "")
        if chosen in NETWORKS:
            state["network"] = chosen
        text, markup = _build_home(user_id)
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
        # Refresh wallet menu
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
            "🔐 *Import Wallet*\n\n"
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
            marker = "✅ " if w["wallet_id"] == active_id else ""
            buttons.append([
                InlineKeyboardButton(
                    f"{marker}{w['label']} [{w['source']}]",
                    callback_data=f"set_wallet_{w['wallet_id']}",
                )
            ])
        buttons.append(_back_button("menu_wallets"))
        await query.edit_message_text(
            "🔐 *Select Active Wallet:*",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
        return

    if data.startswith("set_wallet_"):
        wid = int(data.replace("set_wallet_", ""))
        state["active_wallet_id"] = wid
        text, markup = _build_home(user_id)
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
            f"🔑 *{wallet['label']} Private Key:*\n"
            f"`{wallet['private_key']}`\n\n"
            f"This message deletes in 5 minutes.",
            parse_mode="Markdown",
        )
        asyncio.create_task(
            _delete_after(query.message.chat_id, msg.message_id, 300, context)
        )
        return

    # ---------------------------------------------------- copy trade targets
    if data == "menu_targets":
        targets = db.get_user_targets(user_id)
        text = "🎯 *Copy Trade Targets*\n\n"
        if targets:
            for i, t in enumerate(targets):
                connector = "└" if i == len(targets) - 1 else "├"
                text += f"{connector} `{t}`\n"
        else:
            text += "No targets tracked yet.\n"
        text += "\nPaste a wallet address to add it as a target."

        keyboard = []
        # Add remove buttons for each target
        for t in targets:
            short = t[:6] + "..." + t[-4:]
            keyboard.append([
                InlineKeyboardButton(
                    f"🗑 Remove {short}", callback_data=f"rm_target_{t}"
                )
            ])
        state["step"] = "ADD_TARGET"
        keyboard.append(_back_button())
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    if data.startswith("rm_target_"):
        target = data.replace("rm_target_", "")
        db.remove_target(user_id, target)
        # Re-render targets menu
        targets = db.get_user_targets(user_id)
        text = "🎯 *Copy Trade Targets*\n\n"
        if targets:
            for i, t in enumerate(targets):
                connector = "└" if i == len(targets) - 1 else "├"
                text += f"{connector} `{t}`\n"
        else:
            text += "No targets tracked yet.\n"
        text += "\nPaste a wallet address to add it as a target."
        keyboard = []
        for t in targets:
            short = t[:6] + "..." + t[-4:]
            keyboard.append([
                InlineKeyboardButton(
                    f"🗑 Remove {short}", callback_data=f"rm_target_{t}"
                )
            ])
        keyboard.append(_back_button())
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    # --------------------------------------------------- mint flow callbacks
    if data.startswith("qty_"):
        context.user_data["selected_qty"] = int(data.split("_")[1])
        await _refresh_mint_keyboard(query, context)
        return

    if data.startswith("gas_"):
        context.user_data["selected_gas"] = int(data.split("_")[1])
        await _refresh_mint_keyboard(query, context)
        return

    if data.startswith("price_"):
        context.user_data["selected_price"] = float(data.split("_")[1])
        await _refresh_mint_keyboard(query, context)
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
            f"⏳ Simulating transaction for `{contract}`...",
            parse_mode="Markdown",
        )
        await _dispatch_mint(
            user_id, query.message.chat_id, contract, qty, price, gas, context
        )
        return


# ============================================================================
# WALLET TEXT BUILDER
# ============================================================================

def _build_wallet_text(user_id: int) -> str:
    wallets = db.get_wallets(user_id)
    active_id = _get_active_wallet_id(user_id)
    if not wallets:
        return "🔐 *Wallet Manager*\n\nNo wallets yet. Create or import one."

    lines = ["🔐 *Wallet Manager*", ""]
    for i, w in enumerate(wallets):
        marker = " (active)" if w["wallet_id"] == active_id else ""
        connector = "└" if i == len(wallets) - 1 else "├"
        lines.append(f"{connector} *{w['label']}* [{w['source']}]{marker}")
        lines.append(f"  `{w['address']}`")
    return "\n".join(lines)


# ============================================================================
# MINT KEYBOARD BUILDER & REFRESH
# ============================================================================

def _build_mint_keyboard(qty: int, gas: int, price: float) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton(f"{'✅ ' if qty == 1 else ''}1", callback_data="qty_1"),
            InlineKeyboardButton(f"{'✅ ' if qty == 2 else ''}2", callback_data="qty_2"),
            InlineKeyboardButton(f"{'✅ ' if qty == 5 else ''}5", callback_data="qty_5"),
        ],
        [
            InlineKeyboardButton(f"{'✅ ' if price == 0.0 else ''}Free", callback_data="price_0.0"),
            InlineKeyboardButton(f"{'✅ ' if price == 0.01 else ''}0.01", callback_data="price_0.01"),
            InlineKeyboardButton(f"{'✅ ' if price == 0.05 else ''}0.05", callback_data="price_0.05"),
        ],
        [
            InlineKeyboardButton(f"{'✅ ' if gas == 2 else ''}2 Gwei", callback_data="gas_2"),
            InlineKeyboardButton(f"{'✅ ' if gas == 5 else ''}5 Gwei", callback_data="gas_5"),
            InlineKeyboardButton(f"{'✅ ' if gas == 10 else ''}10 Gwei", callback_data="gas_10"),
        ],
        [InlineKeyboardButton("⚡ Confirm & Mint", callback_data="confirm_mint")],
        _back_button(),
    ]
    return InlineKeyboardMarkup(keyboard)


async def _refresh_mint_keyboard(query, context):
    qty = context.user_data.get("selected_qty", 1)
    gas = context.user_data.get("selected_gas", 2)
    price = context.user_data.get("selected_price", 0.0)
    await query.edit_message_reply_markup(
        reply_markup=_build_mint_keyboard(qty, gas, price)
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
        loop = asyncio.get_running_loop()
        try:
            tx_hash = await loop.run_in_executor(
                None, execute_mint, rpc_url, wallet["private_key"],
                contract_address, qty, price, gas_gwei, max_base_fee,
            )
            net_name = NETWORKS[user_states[user_id]["network"]]["name"]
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🚀 *Mint Broadcasted*\n\n"
                    f"Network: *{net_name}*\n"
                    f"TX: `{tx_hash}`"
                ),
                parse_mode="Markdown",
            )
        except RuntimeError as e:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ *Mint Aborted (Simulation Failed)*\n\n"
                    f"`{str(e)}`\n\n"
                    f"No transaction was broadcast. Zero gas spent."
                ),
                parse_mode="Markdown",
            )
        except ValueError as e:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ *Invalid Target*\n{str(e)}",
                parse_mode="Markdown",
            )
        except Exception as e:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🚨 *Error:*\n`{str(e)}`",
                parse_mode="Markdown",
            )


# ============================================================================
# TEXT INPUT ROUTER
# ============================================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    state = user_states[user_id]

    # ------------------------------------------------ wallet import flow
    if state["step"] == "IMPORT_KEY":
        raw_key = text
        # Delete the message containing the private key immediately
        try:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
            )
        except Exception:
            pass

        state["step"] = None
        try:
            result = db.import_wallet(user_id, raw_key)
        except Exception:
            await update.effective_chat.send_message(
                "That does not look like a valid private key. Import cancelled."
            )
            return

        if result["already_existed"]:
            await update.effective_chat.send_message(
                f"Already imported as {result['label']}.\n`{result['address']}`",
                parse_mode="Markdown",
            )
        else:
            state["active_wallet_id"] = result["wallet_id"]
            await update.effective_chat.send_message(
                f"Imported as {result['label']}.\n"
                f"`{result['address']}`\n\n"
                f"The message with your key has been removed from this chat.",
                parse_mode="Markdown",
            )
        return

    # ------------------------------------------ withdrawal wizard: amount
    if state["step"] == "WITHDRAW_AMOUNT":
        # Numbers only validation
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
            f"⏳ Broadcasting withdrawal of `{amt} ETH`...",
            parse_mode="Markdown",
        )

        async with user_locks[user_id]:
            loop = asyncio.get_running_loop()
            try:
                tx_hash = await loop.run_in_executor(
                    None, execute_withdraw, rpc_url,
                    wallet["private_key"], to_addr, amt,
                )
                await msg.edit_text(
                    f"✅ *Withdrawal Confirmed*\nTX: `{tx_hash}`",
                    parse_mode="Markdown",
                )
            except Exception as e:
                await msg.edit_text(
                    f"❌ *Withdrawal Failed:*\n`{str(e)}`",
                    parse_mode="Markdown",
                )
        return

    # ------------------------------------------- add target flow
    if state["step"] == "ADD_TARGET":
        if re.match(r"^0x[a-fA-F0-9]{40}$", text):
            db.add_target(user_id, text)
            state["step"] = None
            await update.message.reply_text(
                f"🎯 Target added: `{text}`",
                parse_mode="Markdown",
            )
            return
        # Not an address while in target mode, ignore silently
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

        # Auto-detect mint price from contract
        detected = detect_mint_price(rpc_url, text) or 0.0
        context.user_data["selected_qty"] = 1
        context.user_data["selected_gas"] = 2
        context.user_data["selected_price"] = detected

        if state["mode"] == "AUTO":
            await update.message.reply_text(
                f"⚡ Auto Mode: simulating mint for `{text}` "
                f"at `{detected} ETH` per token...",
                parse_mode="Markdown",
            )
            await _dispatch_mint(
                user_id, update.effective_chat.id,
                text, 1, detected, 2, context,
            )
            return

        # MANUAL mode: show mint config keyboard
        net_name = NETWORKS[state["network"]]["name"]
        await update.message.reply_text(
            f"🎯 *Mint Configuration*\n\n"
            f"Contract: `{text}`\n"
            f"Detected Price: `{detected} ETH`\n"
            f"Network: *{net_name}*\n\n"
            f"Configure and confirm:",
            reply_markup=_build_mint_keyboard(1, 2, detected),
            parse_mode="Markdown",
        )
        return


# ============================================================================
# MEMPOOL SNIPER (per-user background worker)
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
            text="No targets tracked. Add targets from the Copy Trade menu first.",
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

                await ws.recv()  # subscription confirmation
                target_list = ", ".join(t[:8] + "..." for t in targets)
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🎯 *Sniper Listening*\n\n"
                        f"Targets: {target_list}\n"
                        f"Chain: *{net_key.upper()}*\n"
                        f"Mode: `{'DRY RUN' if state['dry_run'] else 'LIVE'}`"
                    ),
                    parse_mode="Markdown",
                )

                while state["sniper_active"]:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    if "params" not in msg or "result" not in msg["params"]:
                        continue

                    tx_data = msg["params"]["result"]
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
                                f"⚠️ Daily limit would be exceeded "
                                f"({state['daily_spent_eth']:.4f}/"
                                f"{state['daily_limit_eth']} ETH). Skipping."
                            ),
                        )
                        continue

                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🎯 *Mint Detected*\n\n"
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
                        loop = asyncio.get_running_loop()
                        try:
                            result = await loop.run_in_executor(
                                None, execute_copy_mint,
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
                                text=f"✅ *Result:*\n`{result}`",
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            state["failed_copies"] += 1
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=f"❌ *Failed:*\n`{str(e)}`",
                                parse_mode="Markdown",
                            )

        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Sniper reconnecting for user {user_id}: {e}")
            await asyncio.sleep(2)


# ============================================================================
# MAIN
# ============================================================================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Entry points (only two commands needed)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("home", start_cmd))

    # Everything else is buttons or text input
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()