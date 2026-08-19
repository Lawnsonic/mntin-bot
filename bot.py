"""
bot.py — Multi-user Telegram bot for NFT minting on multiple chains.

Each user gets their own encrypted wallet (via db.py), per-user concurrency
locks, and per-user state (active network, mode, sniper settings).
Default network: Robinhood Chain (chain ID 4663).
"""

import os
import re
import json
import asyncio
import time
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

# Multi-chain registry.
# Robinhood Chain (chain ID 4663) is its own separate L2 built on Arbitrum
# Orbit — it is NOT Arbitrum One. It has its own RPC and deployments.
NETWORKS = {
    "robinhood": os.getenv("ROBINHOOD_RPC", "https://rpc.mainnet.chain.robinhood.com"),
    "arb":       os.getenv("ARB_RPC", "https://arb1.arbitrum.io/rpc"),
    "base":      os.getenv("BASE_RPC", "https://mainnet.base.org"),
    "eth":       os.getenv("ETH_RPC", "https://eth.llamarpc.com"),
}

WS_RPC = os.getenv("WS_RPC", "")

# Safety defaults
DEFAULT_MAX_BASE_FEE_GWEI = int(os.getenv("MAX_BASE_FEE_GWEI", "100"))
DEFAULT_DAILY_LIMIT_ETH = float(os.getenv("DAILY_LIMIT_ETH", "0.05"))

# ============================================================================
# PER-USER STATE
# ============================================================================

# Per-user asyncio locks — prevents nonce collisions for a single user
# while letting different users transact concurrently.
user_locks = defaultdict(asyncio.Lock)


def _default_user_state() -> dict:
    """Factory for per-user state."""
    return {
        "network": "robinhood",
        "mode": "MANUAL",
        # Mint defaults
        "default_qty": 1,
        "default_priority_gwei": 2,
        "default_value": 0.0,
        # Sniper settings
        "sniper_active": False,
        "dry_run": True,
        "target_wallet": "",
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
    """Get the active RPC URL for a user."""
    return NETWORKS[user_states[user_id]["network"]]


def _check_daily_limit(user_id: int, amount_eth: float) -> bool:
    """Check if adding amount_eth would exceed the user's daily limit."""
    state = user_states[user_id]
    if time.time() - state["daily_reset_time"] > 86400:
        state["daily_spent_eth"] = 0.0
        state["daily_reset_time"] = time.time()
    return (state["daily_spent_eth"] + amount_eth) <= state["daily_limit_eth"]


def _is_seen(user_id: int, tx_hash: str) -> bool:
    """Dedup check for sniper transactions."""
    state = user_states[user_id]
    seen: Set[str] = state["seen_txs"]
    if tx_hash in seen:
        return True
    seen.add(tx_hash)
    if len(seen) > 10000:
        state["seen_txs"] = set(list(seen)[-5000:])
    return False


async def delete_message_task(
    chat_id: int,
    message_id: int,
    delay: int,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Background task to delete a message after `delay` seconds."""
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        print(f"Failed to delete message {message_id}: {e}")


def build_manual_keyboard(
    selected_qty: int, selected_gas: int
) -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard with selected visual states."""
    qty_buttons = []
    for q in [1, 2, 5]:
        label = f"✅ {q}" if q == selected_qty else f"Qty: {q}"
        qty_buttons.append(
            InlineKeyboardButton(label, callback_data=f"qty_{q}")
        )

    gas_options = [
        (2, "Standard (2 Gwei)"),
        (5, "Fast (5 Gwei)"),
        (10, "Turbo (10 Gwei)"),
    ]
    gas_buttons = []
    for g_val, g_label in gas_options:
        label = f"✅ {g_label}" if g_val == selected_gas else g_label
        gas_buttons.append(
            InlineKeyboardButton(label, callback_data=f"gas_{g_val}")
        )

    keyboard = [
        qty_buttons,
        gas_buttons,
        [InlineKeyboardButton("⚡ Confirm & Mint", callback_data="confirm_mint")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ============================================================================
# ONBOARDING COMMANDS
# ============================================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message shown on first interaction."""
    await update.message.reply_text(
        "🤖 *Welcome to the Multi-Chain Minting Suite*\n\n"
        "This bot gives you a dedicated execution wallet for minting.\n\n"
        "*Getting Started:*\n"
        "1. `/wallet` — Create your wallet (private key shown once)\n"
        "2. `/deposit` — Get your deposit address\n"
        "3. Fund your wallet, then paste any contract address to mint\n\n"
        "*Wallet Commands:*\n"
        "• `/balance` — Check your balance\n"
        "• `/withdraw <amount> <address>` — Send funds out\n"
        "• `/deposit` — Show your deposit address\n\n"
        "*Minting Commands:*\n"
        "• `/mode` — Toggle AUTO/MANUAL minting\n"
        "• `/network <name>` — Switch chains (robinhood, arb, base, eth)\n\n"
        "*Sniper Commands:*\n"
        "• `/snipe` — Start/Stop mempool sniper\n"
        "• `/target <address>` — Set copy-trade target wallet\n"
        "• `/dryrun` — Toggle dry run on/off\n"
        "• `/status` — View full system status\n\n"
        "Default network: *Robinhood Chain* (Chain ID 4663).",
        parse_mode="Markdown",
    )


async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create wallet on first call (shows key once). Repeat calls show address only."""
    user_id = update.effective_user.id
    wallet = db.get_or_create_wallet(user_id)

    if not wallet["created"]:
        # Wallet already exists — show address only, never re-expose the key
        await update.message.reply_text(
            f"You already have a wallet:\n`{wallet['address']}`\n\n"
            f"Your private key was shown once at creation.\n"
            f"Use `/deposit` to see your address anytime.",
            parse_mode="Markdown",
        )
        return

    # First time — show the key in a self-destructing message
    msg = await update.message.reply_text(
        f"🔐 *Your Execution Wallet — Created*\n\n"
        f"Address:\n`{wallet['address']}`\n\n"
        f"Private Key:\n`{wallet['private_key']}`\n\n"
        f"⚠️ *Save this now — this message self-destructs in 5 minutes "
        f"and the key won't be shown again.*\n"
        f"Don't keep large amounts here.",
        parse_mode="Markdown",
    )
    # Schedule deletion after 300 seconds (5 minutes)
    asyncio.create_task(
        delete_message_task(update.effective_chat.id, msg.message_id, 300, context)
    )


async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show deposit address (address-only lookup, no key decryption)."""
    user_id = update.effective_user.id
    address = db.get_address(user_id)
    if not address:
        await update.message.reply_text(
            "No wallet found. Use `/wallet` to create one.",
            parse_mode="Markdown",
        )
        return
    network = user_states[user_id]["network"]
    await update.message.reply_text(
        f"📥 *Deposit Address* ({network.upper()}):\n`{address}`",
        parse_mode="Markdown",
    )


# ============================================================================
# BALANCE, NETWORK, MODE COMMANDS
# ============================================================================

async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check balance on the user's active network (address-only, no key needed)."""
    user_id = update.effective_user.id
    address = db.get_address(user_id)
    if not address:
        await update.message.reply_text(
            "No wallet found. Use `/wallet` to create one.",
            parse_mode="Markdown",
        )
        return

    network_key = user_states[user_id]["network"]
    try:
        bal = get_balance(NETWORKS[network_key], address)
        await update.message.reply_text(
            f"📊 Balance on *{network_key.upper()}*:\n`{bal:.4f} ETH`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error fetching balance: {str(e)}")


async def network_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch the active chain for this user."""
    user_id = update.effective_user.id
    if not context.args:
        current = user_states[user_id]["network"]
        available = ", ".join(NETWORKS.keys())
        await update.message.reply_text(
            f"Current network: *{current.upper()}*\n"
            f"Available: {available}\n"
            f"Usage: `/network <name>`",
            parse_mode="Markdown",
        )
        return

    choice = context.args[0].lower()
    if choice not in NETWORKS:
        await update.message.reply_text(
            f"Unknown network. Available: {', '.join(NETWORKS.keys())}"
        )
        return

    user_states[user_id]["network"] = choice
    await update.message.reply_text(
        f"Network switched to *{choice.upper()}*",
        parse_mode="Markdown",
    )


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle between AUTO and MANUAL minting modes."""
    user_id = update.effective_user.id
    state = user_states[user_id]
    state["mode"] = "AUTO" if state["mode"] == "MANUAL" else "MANUAL"
    await update.message.reply_text(
        f"Mint mode switched to: *{state['mode']}*",
        parse_mode="Markdown",
    )


# ============================================================================
# WITHDRAW
# ============================================================================

async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send ETH from the user's bot wallet to an external address."""
    user_id = update.effective_user.id
    wallet = db.get_user_wallet(user_id)  # key genuinely needed — signs a tx
    if not wallet:
        await update.message.reply_text(
            "No wallet found. Use `/wallet` to create one.",
            parse_mode="Markdown",
        )
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: `/withdraw <amount> <destination_address>`",
            parse_mode="Markdown",
        )
        return

    try:
        amount = float(context.args[0])
        to_address = context.args[1]
    except ValueError:
        await update.message.reply_text("Invalid amount format.")
        return

    if not re.match(r"^0x[a-fA-F0-9]{40}$", to_address):
        await update.message.reply_text("Invalid destination address format.")
        return

    rpc_url = _get_rpc(user_id)
    msg = await update.message.reply_text("⏳ Processing withdrawal...")

    async with user_locks[user_id]:
        loop = asyncio.get_running_loop()
        try:
            tx_hash = await loop.run_in_executor(
                None, execute_withdraw, rpc_url, wallet["private_key"],
                to_address, amount,
            )
            await msg.edit_text(
                f"✅ *Withdrawal Successful*\nTX: `{tx_hash}`",
                parse_mode="Markdown",
            )
        except Exception as e:
            await msg.edit_text(f"❌ *Failed:*\n{str(e)}")


# ============================================================================
# DIRECT MINT (address paste handler + inline buttons)
# ============================================================================

async def dispatch_mint(
    user_id: int,
    chat_id: int,
    contract_address: str,
    qty: int,
    value: float,
    priority_gwei: int,
    context: ContextTypes.DEFAULT_TYPE,
):
    """Execute a mint, locked per-user to prevent nonce collisions."""
    wallet = db.get_user_wallet(user_id)
    if not wallet:
        await context.bot.send_message(
            chat_id=chat_id,
            text="No wallet found. Use `/wallet` first.",
            parse_mode="Markdown",
        )
        return

    rpc_url = _get_rpc(user_id)
    max_base_fee = user_states[user_id]["max_base_fee_gwei"]

    async with user_locks[user_id]:
        loop = asyncio.get_running_loop()
        try:
            tx_hash = await loop.run_in_executor(
                None,
                execute_mint,
                rpc_url,
                wallet["private_key"],
                contract_address,
                qty,
                value,
                priority_gwei,
                max_base_fee,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🚀 *Mint Broadcasted*\nTX: `{tx_hash}`",
                parse_mode="Markdown",
            )
        except RuntimeError as e:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ *Mint Aborted (Simulation Failed)*\n`{str(e)}`\n\n"
                    f"*No gas was spent.*"
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
                text=f"🚨 *Broadcast Error*\n`{str(e)}`",
                parse_mode="Markdown",
            )


async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detect pasted contract addresses and route to mint flow."""
    text = update.message.text.strip()
    if not re.match(r"^0x[a-fA-F0-9]{40}$", text):
        return  # Not an address — ignore silently

    user_id = update.effective_user.id
    state = user_states[user_id]

    # Check wallet exists
    if not db.get_address(user_id):
        await update.message.reply_text(
            "No wallet found. Use `/wallet` to create one first.",
            parse_mode="Markdown",
        )
        return

    context.user_data["target_contract"] = text

    if state["mode"] == "AUTO":
        await update.message.reply_text(
            f"Auto Mode: Simulating `{text}`...",
            parse_mode="Markdown",
        )
        await dispatch_mint(
            user_id, update.effective_chat.id, text,
            state["default_qty"], state["default_value"],
            state["default_priority_gwei"], context,
        )
        return

    # MANUAL mode — show quantity and gas picker
    context.user_data["selected_qty"] = 1
    context.user_data["selected_gas"] = 2

    keyboard = build_manual_keyboard(selected_qty=1, selected_gas=2)
    await update.message.reply_text(
        f"Contract: `{text}`\nChoose quantity and gas below, then confirm:",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button clicks for manual mint flow."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data

    qty = context.user_data.get("selected_qty", 1)
    gas = context.user_data.get("selected_gas", 2)

    if data.startswith("qty_"):
        qty = int(data.split("_")[1])
        context.user_data["selected_qty"] = qty
        await query.edit_message_reply_markup(
            reply_markup=build_manual_keyboard(qty, gas)
        )

    elif data.startswith("gas_"):
        gas = int(data.split("_")[1])
        context.user_data["selected_gas"] = gas
        await query.edit_message_reply_markup(
            reply_markup=build_manual_keyboard(qty, gas)
        )

    elif data == "confirm_mint":
        contract = context.user_data.get("target_contract")
        if not contract:
            await query.edit_message_text("No contract address set. Paste one first.")
            return
        await query.edit_message_text(
            f"⏳ Simulating and broadcasting for `{contract}`...",
            parse_mode="Markdown",
        )
        await dispatch_mint(
            user_id, update.effective_chat.id, contract, qty, 0.0, gas, context
        )


# ============================================================================
# SNIPER COMMANDS
# ============================================================================

async def toggle_snipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start or stop the mempool sniper for this user."""
    user_id = update.effective_user.id
    state = user_states[user_id]

    if not db.get_address(user_id):
        await update.message.reply_text(
            "No wallet found. Use `/wallet` to create one first.",
            parse_mode="Markdown",
        )
        return

    state["sniper_active"] = not state["sniper_active"]

    if state["sniper_active"]:
        target = state["target_wallet"]
        if not target or not re.match(r"^0x[a-fA-F0-9]{40}$", target):
            state["sniper_active"] = False
            await update.message.reply_text(
                "❌ Set a valid target wallet first: `/target 0x...`",
                parse_mode="Markdown",
            )
            return
        if not WS_RPC:
            state["sniper_active"] = False
            await update.message.reply_text(
                "❌ No WS_RPC configured in .env. Sniper requires a WebSocket endpoint.",
            )
            return
        state["sniper_task"] = asyncio.create_task(
            mempool_worker(user_id, update.effective_chat.id, context.application)
        )
        await update.message.reply_text(
            "🚀 *Sniper Activated!* Listening for target transactions...",
            parse_mode="Markdown",
        )
    else:
        task = state.get("sniper_task")
        if task and not task.done():
            task.cancel()
        await update.message.reply_text(
            "🛑 *Sniper Deactivated.*",
            parse_mode="Markdown",
        )


async def set_target_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the wallet address to copy-trade from."""
    user_id = update.effective_user.id
    state = user_states[user_id]

    if not context.args:
        current = state["target_wallet"] or "None set"
        await update.message.reply_text(
            f"Current target: `{current}`\n\nUsage: `/target 0x1234...`",
            parse_mode="Markdown",
        )
        return

    new_target = context.args[0].strip().lower()
    if not re.match(r"^0x[a-fA-F0-9]{40}$", new_target):
        await update.message.reply_text("❌ Invalid EVM address format.")
        return

    state["target_wallet"] = new_target
    await update.message.reply_text(
        f"🎯 Target wallet updated to:\n`{state['target_wallet']}`",
        parse_mode="Markdown",
    )


async def toggle_dryrun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle dry-run mode for the sniper."""
    user_id = update.effective_user.id
    state = user_states[user_id]
    state["dry_run"] = not state["dry_run"]
    await update.message.reply_text(
        f"Dry Run mode set to: *{state['dry_run']}*",
        parse_mode="Markdown",
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show full status for this user — wallet, balance, sniper, etc."""
    user_id = update.effective_user.id
    address = db.get_address(user_id)
    state = user_states[user_id]
    network_key = state["network"]

    if not address:
        await update.message.reply_text(
            "No wallet found. Use `/wallet` to create one.",
            parse_mode="Markdown",
        )
        return

    try:
        bal = get_balance(NETWORKS[network_key], address)
        bal_str = f"{bal:.4f} ETH"
    except Exception:
        bal_str = "Error fetching"

    await update.message.reply_text(
        f"📊 *System Status*\n\n"
        f"• Wallet: `{address}`\n"
        f"• Network: *{network_key.upper()}*\n"
        f"• Balance: `{bal_str}`\n"
        f"• Mint Mode: *{state['mode']}*\n"
        f"• Sniper Active: *{state['sniper_active']}*\n"
        f"• Sniper Dry Run: *{state['dry_run']}*\n"
        f"• Target Wallet: `{state['target_wallet'] or 'None set'}`\n"
        f"• Sniper Stats: {state['successful_copies']} ok / "
        f"{state['failed_copies']} failed\n"
        f"• Daily Spent: `{state['daily_spent_eth']:.4f} / "
        f"{state['daily_limit_eth']} ETH`",
        parse_mode="Markdown",
    )


# ============================================================================
# MEMPOOL SNIPER BACKGROUND WORKER (per-user)
# ============================================================================

async def mempool_worker(user_id: int, chat_id: int, app):
    """
    WebSocket listener for a user's target wallet.
    Runs as a background task per-user.
    """
    from websockets import connect

    state = user_states[user_id]

    while state["sniper_active"]:
        try:
            target = state["target_wallet"]
            if not target or not target.startswith("0x") or len(target) != 42:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ *Sniper paused:* Invalid target wallet.",
                    parse_mode="Markdown",
                )
                state["sniper_active"] = False
                break

            async with connect(WS_RPC) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_subscribe",
                    "params": [
                        "alchemy_pendingTransactions",
                        {"fromAddress": [target]},
                    ],
                }))

                await ws.recv()  # subscription confirmation
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🎯 *Mempool Sniper Listening*\n"
                        f"Target: `{target}`\n"
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
                    # Alchemy can return full tx object or just a hash
                    from web3 import Web3
                    if isinstance(tx_data, str):
                        w3 = Web3(Web3.HTTPProvider(_get_rpc(user_id)))
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
                    val_eth = float(Web3.from_wei(int(tx.get("value", 0)), "ether"))

                    # Check daily limit
                    if not _check_daily_limit(user_id, val_eth):
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"⚠️ *Sniper Skip:* Daily limit would be exceeded "
                                f"({state['daily_spent_eth']:.4f}/{state['daily_limit_eth']} ETH)"
                            ),
                            parse_mode="Markdown",
                        )
                        continue

                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🎯 *Target Mint Detected in Mempool!*\n"
                            f"Target: `{target}`\n"
                            f"Contract: `{tx.get('to')}`\n"
                            f"Qty: `{qty}` | Val: `{val_eth} ETH`\n"
                            f"Executing copy..."
                        ),
                        parse_mode="Markdown",
                    )

                    # Execute copy mint
                    wallet = db.get_user_wallet(user_id)
                    if not wallet:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text="❌ Wallet not found. Cannot execute copy mint.",
                        )
                        continue

                    rpc_url = _get_rpc(user_id)

                    async with user_locks[user_id]:
                        loop = asyncio.get_running_loop()
                        try:
                            result = await loop.run_in_executor(
                                None,
                                execute_copy_mint,
                                rpc_url,
                                wallet["private_key"],
                                tx.get("to"),
                                input_data,
                                qty,
                                val_eth,
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
                                # Track spend only on live trades
                                # (approximation — actual spend calculated inside engine)
                                state["daily_spent_eth"] += val_eth
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=f"✅ *Sniper Result:*\n`{result}`",
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            state["failed_copies"] += 1
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=f"❌ *Sniper Failed:*\n`{str(e)}`",
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

    # Onboarding
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("deposit", deposit_cmd))

    # Wallet operations
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("withdraw", withdraw_cmd))

    # Network & mode
    app.add_handler(CommandHandler("network", network_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))

    # Sniper
    app.add_handler(CommandHandler("snipe", toggle_snipe_cmd))
    app.add_handler(CommandHandler("target", set_target_cmd))
    app.add_handler(CommandHandler("dryrun", toggle_dryrun_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    # Address paste + inline buttons
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling()


if __name__ == "__main__":
    main()