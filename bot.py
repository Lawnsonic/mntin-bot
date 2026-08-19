import os
import re
import json
import asyncio
import time
from functools import wraps
from typing import Optional, Set
from dataclasses import dataclass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from web3 import Web3
from eth_abi import decode
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# CONFIGURATION & STATE
# ============================================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")
HTTP_RPC = os.getenv("HTTP_RPC")
WS_RPC = os.getenv("WS_RPC")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

if not all([BOT_TOKEN, ALLOWED_USER_ID, HTTP_RPC, WS_RPC, PRIVATE_KEY]):
    raise ValueError("Missing critical configuration variables in .env")

ALLOWED_USER_ID = int(ALLOWED_USER_ID)

w3 = Web3(Web3.HTTPProvider(HTTP_RPC))
account = w3.eth.account.from_key(PRIVATE_KEY)
user_address = account.address

tx_lock = asyncio.Lock()

class BotState:
    def __init__(self):
        self.mode = "MANUAL"  # "MANUAL" or "AUTO"
        self.default_qty = 1
        self.default_priority_gwei = 2
        self.default_value = 0.0
        
        # Sniper Settings
        self.sniper_active = False
        self.dry_run = True
        self.target_wallet = os.getenv("TARGET_WALLET", "").lower()
        self.daily_limit_eth = float(os.getenv("DAILY_LIMIT_ETH", "5.0"))
        self.max_base_fee_gwei = int(os.getenv("MAX_BASE_FEE_GWEI", "150"))
        self.gas_bump_percent = 30
        self.max_priority_gwei = 50
        
        # Sniper Tracking
        self.seen_txs: Set[str] = set()
        self.daily_spent_eth = 0.0
        self.daily_reset_time = time.time()
        self.successful_copies = 0
        self.failed_copies = 0
        self.sniper_task: Optional[asyncio.Task] = None

    def check_daily_limit(self, amount_eth: float) -> bool:
        if time.time() - self.daily_reset_time > 86400:
            self.daily_spent_eth = 0.0
            self.daily_reset_time = time.time()
        return (self.daily_spent_eth + amount_eth) <= self.daily_limit_eth

    def is_seen(self, tx_hash: str) -> bool:
        if tx_hash in self.seen_txs:
            return True
        self.seen_txs.add(tx_hash)
        if len(self.seen_txs) > 10000:
            self.seen_txs = set(list(self.seen_txs)[-5000:])
        return False

STATE = BotState()

MINT_SIGNATURES = {
    "0x1249c58b": ("mint()", [], None),
    "0xa0712d68": ("mint(uint256)", ["uint256"], 0),
    "0x6c2a8e54": ("mintPublic(uint256)", ["uint256"], 0),
    "0x8b94d9a5": ("mintPublic(uint256)", ["uint256"], 0),
    "0xefef39a1": ("mint(uint256,uint256,bytes)", ["uint256", "uint256", "bytes"], 0),
    "0x2309bbed": ("mint(uint256,bytes32[])", ["uint256", "bytes32[]"], 0),
    "0x7ba6b3f1": ("whitelistMint(uint256,bytes32[])", ["uint256", "bytes32[]"], 0),
    "0x4a7d1d5c": ("mint(uint256,bytes)", ["uint256", "bytes"], 0),
}

DEFAULT_MINT_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "quantity", "type": "uint256"}],
        "name": "mint",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    }
]

# ============================================================================
# AUTHENTICATION & KEYBOARD BUILDERS
# ============================================================================

def authorized_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != ALLOWED_USER_ID:
            if update.effective_message:
                await update.effective_message.reply_text("Unauthorized. Access denied.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def build_manual_keyboard(selected_qty: int, selected_gas: int) -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard with selected visual states."""
    qty_buttons = []
    for q in [1, 2, 5]:
        label = f"✅ {q}" if q == selected_qty else f"Qty: {q}"
        qty_buttons.append(InlineKeyboardButton(label, callback_data=f"qty_{q}"))

    gas_options = [(2, "Standard (2 Gwei)"), (5, "Fast (5 Gwei)"), (10, "Turbo (10 Gwei)")]
    gas_buttons = []
    for g_val, g_label in gas_options:
        label = f"✅ {g_label}" if g_val == selected_gas else g_label
        gas_buttons.append(InlineKeyboardButton(label, callback_data=f"gas_{g_val}"))

    keyboard = [
        qty_buttons,
        gas_buttons,
        [InlineKeyboardButton("⚡ Confirm & Mint", callback_data="confirm_mint")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ============================================================================
# WEB3 EXECUTION UTILITIES
# ============================================================================

def verify_contract_exists(address: str) -> str:
    checksum_addr = Web3.to_checksum_address(address)
    code = w3.eth.get_code(checksum_addr)
    if code in (b"", b"\x00", "0x", b"0x"):
        raise ValueError(f"Address {checksum_addr} has no bytecode. Target is an EOA or uninitialized contract.")
    return checksum_addr

def parse_mint_quantity(input_data: str, func_sig: str) -> int:
    if func_sig not in MINT_SIGNATURES:
        return 1
    name, types, qty_idx = MINT_SIGNATURES[func_sig]
    if not types:
        return 1
    hex_str = input_data[2:] if input_data.startswith("0x") else input_data
    data = hex_str[8:]
    if len(data) < 64:
        return 1
    try:
        encoded = bytes.fromhex(data)
        decoded = decode(types, encoded)
        if qty_idx is not None and qty_idx < len(decoded):
            qty = decoded[qty_idx]
            if isinstance(qty, int) and 0 < qty <= 100:
                return qty
    except Exception:
        pass
    return 1

def execute_mint(contract_address: str, quantity: int = 1, value_eth: float = 0.0, max_priority_fee_gwei: int = 2) -> str:
    target_addr = verify_contract_exists(contract_address)
    contract = w3.eth.contract(address=target_addr, abi=DEFAULT_MINT_ABI)
    
    required_value_wei = w3.to_wei(value_eth, "ether")
    current_balance = w3.eth.get_balance(account.address)
    if current_balance < required_value_wei:
        raise ValueError(f"Insufficient balance. Required: {value_eth} ETH, Available: {w3.from_wei(current_balance, 'ether'):.5f} ETH.")

    nonce = w3.eth.get_transaction_count(account.address, "pending")
    latest_block = w3.eth.get_block("latest")
    
    tx_data = {
        "from": account.address,
        "value": required_value_wei,
        "nonce": nonce,
        "chainId": w3.eth.chain_id,
    }
    
    if "baseFeePerGas" in latest_block and latest_block["baseFeePerGas"] is not None:
        base_fee = latest_block["baseFeePerGas"]
        base_fee_gwei = w3.from_wei(base_fee, "gwei")
        if base_fee_gwei > STATE.max_base_fee_gwei:
            raise RuntimeError(f"Base fee ({base_fee_gwei:.1f} Gwei) exceeds ceiling ({STATE.max_base_fee_gwei} Gwei).")
        priority_fee = w3.to_wei(max_priority_fee_gwei, "gwei")
        tx_data["maxFeePerGas"] = int(base_fee * 1.5) + priority_fee
        tx_data["maxPriorityFeePerGas"] = priority_fee
    else:
        tx_data["gasPrice"] = w3.eth.gas_price

    tx = contract.functions.mint(quantity).build_transaction(tx_data)
    
    try:
        estimated_gas = w3.eth.estimate_gas(tx)
        tx["gas"] = int(estimated_gas * 1.2)
    except Exception as sim_err:
        raise RuntimeError(f"Simulation failed (transaction would revert): {sim_err}") from sim_err

    signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    return w3.to_hex(tx_hash)

def execute_copy_mint(
    contract_address: str,
    raw_calldata: str,
    quantity: int,
    value_eth: float,
    target_gas_price: Optional[int],
    target_max_fee: Optional[int],
    target_priority: Optional[int]
) -> Optional[str]:
    if not STATE.check_daily_limit(value_eth):
        raise RuntimeError(f"Daily limit would be exceeded on mint price ({STATE.daily_spent_eth}/{STATE.daily_limit_eth} ETH)")

    target_addr = verify_contract_exists(contract_address)
    required_value_wei = w3.to_wei(value_eth, "ether")
    current_balance = w3.eth.get_balance(user_address)

    if current_balance < required_value_wei:
        raise ValueError("Insufficient balance for copy trade.")

    nonce = w3.eth.get_transaction_count(user_address, "pending")
    latest_block = w3.eth.get_block("latest")
    data = raw_calldata if raw_calldata.startswith("0x") else "0x" + raw_calldata

    tx_data = {
        "from": user_address,
        "to": target_addr,
        "value": required_value_wei,
        "nonce": nonce,
        "chainId": w3.eth.chain_id,
        "data": data,
    }

    if "baseFeePerGas" in latest_block and latest_block["baseFeePerGas"]:
        base_fee = latest_block["baseFeePerGas"]
        if target_priority:
            bumped = int(target_priority * (1 + STATE.gas_bump_percent / 100))
            priority_fee = min(bumped, w3.to_wei(STATE.max_priority_gwei, "gwei"))
        else:
            priority_fee = w3.to_wei(2, "gwei")
        max_fee = min(int(base_fee * 1.5) + priority_fee, w3.to_wei(STATE.max_base_fee_gwei, "gwei"))
        tx_data["maxFeePerGas"] = max_fee
        tx_data["maxPriorityFeePerGas"] = priority_fee
    else:
        gas_price = int(target_gas_price * (1 + STATE.gas_bump_percent / 100)) if target_gas_price else w3.eth.gas_price
        tx_data["gasPrice"] = min(gas_price, w3.to_wei(STATE.max_base_fee_gwei, "gwei"))

    estimated = w3.eth.estimate_gas(tx_data)
    tx_data["gas"] = int(estimated * 1.2)

    total_cost_wei = required_value_wei + (tx_data["gas"] * tx_data.get("maxFeePerGas", tx_data.get("gasPrice", 0)))
    total_cost_eth = float(w3.from_wei(total_cost_wei, "ether"))

    if not STATE.check_daily_limit(total_cost_eth):
        raise RuntimeError(f"Daily limit exceeded including gas ({STATE.daily_spent_eth + total_cost_eth:.4f}/{STATE.daily_limit_eth} ETH)")

    if STATE.dry_run:
        return f"[DRY_RUN_PASS] Simulated {quantity} NFTs for {total_cost_eth:.4f} ETH total"

    signed = w3.eth.account.sign_transaction(tx_data, private_key=PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    STATE.daily_spent_eth += total_cost_eth
    return w3.to_hex(tx_hash)

# ============================================================================
# MEMPOOL SNIPER BACKGROUND WORKER
# ============================================================================

async def mempool_worker(app):
    from websockets import connect
    while STATE.sniper_active:
        try:
            if not STATE.target_wallet or not STATE.target_wallet.startswith("0x") or len(STATE.target_wallet) != 42:
                await app.bot.send_message(chat_id=ALLOWED_USER_ID, text="⚠️ *Sniper paused:* Invalid target wallet configured.", parse_mode="Markdown")
                STATE.sniper_active = False
                break

            async with connect(WS_RPC) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_subscribe",
                    "params": ["alchemy_pendingTransactions", {"fromAddress": [STATE.target_wallet]}]
                }))
                
                await ws.recv()
                await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=f"🎯 *Mempool Sniper Listening*\nTarget: `{STATE.target_wallet}`\nMode: `{'DRY RUN' if STATE.dry_run else 'LIVE'}`", parse_mode="Markdown")

                while STATE.sniper_active:
                    msg = json.loads(await ws.recv())
                    if "params" in msg and "result" in msg["params"]:
                        tx_data = msg["params"]["result"]
                        tx = tx_data if isinstance(tx_data, dict) else w3.eth.get_transaction(tx_data)
                        
                        tx_hash = tx.get("hash")
                        if isinstance(tx_hash, bytes):
                            tx_hash = tx_hash.hex()
                        elif isinstance(tx_hash, str) and not tx_hash.startswith("0x"):
                            tx_hash = "0x" + tx_hash

                        if STATE.is_seen(tx_hash):
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
                        val_eth = float(w3.from_wei(int(tx.get("value", 0)), "ether"))
                        
                        await app.bot.send_message(
                            chat_id=ALLOWED_USER_ID,
                            text=f"🎯 *Target Mint Detected in Mempool!*\nTarget: `{STATE.target_wallet}`\nContract: `{tx.get('to')}`\nQty: `{qty}` | Val: `{val_eth} ETH`\nExecuting copy...",
                            parse_mode="Markdown"
                        )

                        try:
                            result = execute_copy_mint(
                                contract_address=tx.get("to"),
                                raw_calldata=input_data,
                                quantity=qty,
                                value_eth=val_eth,
                                target_gas_price=tx.get("gasPrice"),
                                target_max_fee=tx.get("maxFeePerGas"),
                                target_priority=tx.get("maxPriorityFeePerGas")
                            )
                            STATE.successful_copies += 1
                            await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=f"✅ *Sniper Result:*\n`{result}`", parse_mode="Markdown")
                        except Exception as e:
                            STATE.failed_copies += 1
                            await app.bot.send_message(chat_id=ALLOWED_USER_ID, text=f"❌ *Sniper Skipped/Failed:*\n`{str(e)}`", parse_mode="Markdown")
        except Exception as e:
            await asyncio.sleep(2)

# ============================================================================
# BOT COMMAND HANDLERS
# ============================================================================

@authorized_only
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = w3.from_wei(w3.eth.get_balance(user_address), "ether")
    await update.message.reply_text(
        f"🤖 *NFT Mint & Sniper Suite Active*\n\n"
        f"Wallet: `{user_address}`\n"
        f"Balance: `{bal:.4f} ETH`\n"
        f"Manual Mode: *{STATE.mode}*\n"
        f"Sniper Active: *{STATE.sniper_active}* (Dry Run: `{STATE.dry_run}`)\n\n"
        f"*Commands:*\n"
        f"• `/mode` : Toggle AUTO/MANUAL minting\n"
        f"• `/snipe` : Start/Stop Mempool Sniper\n"
        f"• `/target <addr>` : Set copy-trade target wallet\n"
        f"• `/dryrun` : Toggle Dry Run on/off\n"
        f"• `/status` : View current system status\n\n"
        f"Paste any EVM contract address to mint directly.",
        parse_mode="Markdown"
    )

@authorized_only
async def toggle_mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATE.mode = "AUTO" if STATE.mode == "MANUAL" else "MANUAL"
    await update.message.reply_text(f"Direct Mint Mode switched to: *{STATE.mode}*", parse_mode="Markdown")

@authorized_only
async def toggle_snipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATE.sniper_active = not STATE.sniper_active
    if STATE.sniper_active:
        if not STATE.target_wallet or not STATE.target_wallet.startswith("0x") or len(STATE.target_wallet) != 42:
            STATE.sniper_active = False
            await update.message.reply_text("❌ Please set a valid target wallet first using `/target 0x...`", parse_mode="Markdown")
            return
        STATE.sniper_task = asyncio.create_task(mempool_worker(context.application))
        await update.message.reply_text("🚀 *Sniper Activated!* Listening for target transactions...", parse_mode="Markdown")
    else:
        if STATE.sniper_task:
            STATE.sniper_task.cancel()
        await update.message.reply_text("🛑 *Sniper Deactivated.*", parse_mode="Markdown")

@authorized_only
async def set_target_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"Current target: `{STATE.target_wallet}`\n\nUsage: `/target 0x1234...`", parse_mode="Markdown")
        return
    new_target = context.args[0].strip().lower()
    if not re.match(r"^0x[a-fA-F0-9]{40}$", new_target):
        await update.message.reply_text("❌ Invalid EVM address format.")
        return
    STATE.target_wallet = new_target
    await update.message.reply_text(f"🎯 Target wallet updated to:\n`{STATE.target_wallet}`", parse_mode="Markdown")

@authorized_only
async def toggle_dryrun_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATE.dry_run = not STATE.dry_run
    await update.message.reply_text(f"Dry Run mode set to: *{STATE.dry_run}*", parse_mode="Markdown")

@authorized_only
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = w3.from_wei(w3.eth.get_balance(user_address), "ether")
    await update.message.reply_text(
        f"📊 *System Status*\n\n"
        f"• Wallet: `{user_address}`\n"
        f"• Balance: `{bal:.4f} ETH`\n"
        f"• Mint Mode: *{STATE.mode}*\n"
        f"• Sniper Active: *{STATE.sniper_active}*\n"
        f"• Sniper Dry Run: *{STATE.dry_run}*\n"
        f"• Target Wallet: `{STATE.target_wallet or 'None set'}`\n"
        f"• Sniper Stats: {STATE.successful_copies} ok / {STATE.failed_copies} failed\n"
        f"• Daily Spent: `{STATE.daily_spent_eth:.4f} / {STATE.daily_limit_eth} ETH`",
        parse_mode="Markdown"
    )

# ============================================================================
# DIRECT MINT HANDLERS
# ============================================================================

@authorized_only
async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not re.match(r"^0x[a-fA-F0-9]{40}$", text):
        await update.message.reply_text("Invalid EVM address format.")
        return

    context.user_data["target_contract"] = text

    if STATE.mode == "AUTO":
        await update.message.reply_text(f"Auto Mode: Simulating `{text}`...", parse_mode="Markdown")
        async with tx_lock:
            try:
                loop = asyncio.get_running_loop()
                tx_hash = await loop.run_in_executor(None, execute_mint, text, STATE.default_qty, STATE.default_value, STATE.default_priority_gwei)
                await update.message.reply_text(f"🚀 *Mint Broadcasted*\nTX: `{tx_hash}`", parse_mode="Markdown")
            except Exception as e:
                await update.message.reply_text(f"❌ *Mint Aborted:*\n`{str(e)}`", parse_mode="Markdown")
        return

    context.user_data["selected_qty"] = 1
    context.user_data["selected_gas"] = 2

    keyboard = build_manual_keyboard(selected_qty=1, selected_gas=2)
    await update.message.reply_text(
        f"Contract: `{text}`\nChoose quantity and gas below, then confirm:",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )

@authorized_only
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    qty = context.user_data.get("selected_qty", 1)
    gas = context.user_data.get("selected_gas", 2)

    if data.startswith("qty_"):
        qty = int(data.split("_")[1])
        context.user_data["selected_qty"] = qty
        await query.edit_message_reply_markup(reply_markup=build_manual_keyboard(qty, gas))

    elif data.startswith("gas_"):
        gas = int(data.split("_")[1])
        context.user_data["selected_gas"] = gas
        await query.edit_message_reply_markup(reply_markup=build_manual_keyboard(qty, gas))

    elif data == "confirm_mint":
        contract = context.user_data.get("target_contract")
        await query.edit_message_text(f"⏳ Simulating and broadcasting for `{contract}`...", parse_mode="Markdown")
        
        async with tx_lock:
            try:
                loop = asyncio.get_running_loop()
                tx_hash = await loop.run_in_executor(None, execute_mint, contract, qty, 0.0, gas)
                await query.message.reply_text(f"🚀 *Mint Successful!*\nTX Hash:\n`{tx_hash}`", parse_mode="Markdown")
            except Exception as e:
                await query.message.reply_text(f"❌ *Mint Aborted (Simulation Failed)*\n\n`{str(e)}`\n\n*Zero gas spent.*", parse_mode="Markdown")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("mode", toggle_mode_cmd))
    app.add_handler(CommandHandler("snipe", toggle_snipe_cmd))
    app.add_handler(CommandHandler("target", set_target_cmd))
    app.add_handler(CommandHandler("dryrun", toggle_dryrun_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    app.run_polling()

if __name__ == "__main__":
    main()