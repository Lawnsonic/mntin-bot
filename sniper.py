"""
sniper.py. Multi-chain, multi-target, multi-user copy-mint listener.

Covers Robinhood Chain, Arbitrum, Base, and Ethereum via low-latency
dual-stream WebSocket connections:
  1. newHeads: Instantly scans blocks (250ms-1s) for mined L2/L3 transactions (Orbit/Robinhood, Base, Arbitrum).
  2. alchemy_pendingTransactions: Catches pre-inclusion mempool transactions (Ethereum L1, public mempools).

Runs as background asyncio tasks inside the bot process, sharing db.py,
user_locks, and each user's live settings in bot.py's user_states.
Sends real-time Telegram notifications on detection, simulation, and broadcast.
"""

import asyncio
import json
import os
import time
from typing import Dict, List, Set

from web3 import Web3
from dotenv import load_dotenv

import db
from mint_engine import MINT_SIGNATURES, parse_mint_quantity, execute_copy_mint, get_w3

load_dotenv()

CHAINS = {
    "robinhood": {"http": os.getenv("ROBINHOOD_RPC"), "ws": os.getenv("ROBINHOOD_WS_RPC")},
    "arb": {"http": os.getenv("ARB_RPC"), "ws": os.getenv("ARB_WS_RPC")},
    "base": {"http": os.getenv("BASE_RPC"), "ws": os.getenv("BASE_WS_RPC")},
    "eth": {"http": os.getenv("ETH_RPC"), "ws": os.getenv("ETH_WS_RPC")},
}

_seen_txs: Set[str] = set()


def _mark_seen(tx_hash: str) -> bool:
    """Returns True if already seen. Adds to set if not."""
    if tx_hash in _seen_txs:
        return True
    _seen_txs.add(tx_hash)
    if len(_seen_txs) > 30000:
        _seen_txs.clear()
    return False


def _check_and_record_daily_limit(state: dict, amount_eth: float) -> bool:
    """Check if adding amount_eth would exceed the user's daily limit."""
    if time.time() - state["daily_reset_time"] > 86400:
        state["daily_spent_eth"] = 0.0
        state["daily_reset_time"] = time.time()
    return (state["daily_spent_eth"] + amount_eth) <= state["daily_limit_eth"]


async def _handle_target_tx(
    chain_key: str,
    http_rpc: str,
    tx: dict,
    user_states: dict,
    user_locks: dict,
    tg_bot=None,
):
    """Process a detected target transaction, replay it, and notify user on Telegram."""
    # Normalize tx_hash with 0x prefix
    tx_hash = tx.get("hash")
    if isinstance(tx_hash, bytes):
        tx_hash = tx_hash.hex()
    elif not isinstance(tx_hash, str):
        tx_hash = str(tx_hash)
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    if not tx_hash or _mark_seen(tx_hash):
        return

    # Normalize input_data with 0x prefix (Web3 HexBytes .hex() omits 0x)
    input_data = tx.get("input") or ""
    if isinstance(input_data, bytes):
        input_data = input_data.hex()
    elif not isinstance(input_data, str):
        input_data = str(input_data)
    if not input_data.startswith("0x"):
        input_data = "0x" + input_data

    if len(input_data) < 10:
        return

    func_sig = input_data[:10].lower()
    if func_sig not in MINT_SIGNATURES:
        print(
            f"[{chain_key}] Tracked wallet tx {tx_hash} has unrecognized "
            f"selector {func_sig} (not in MINT_SIGNATURES) - skipping"
        )
        return

    contract_address = tx.get("to")
    if isinstance(contract_address, bytes):
        contract_address = contract_address.hex()
    contract_address = str(contract_address or "").strip()

    quantity = parse_mint_quantity(input_data, func_sig)
    value_eth = float(Web3.from_wei(int(tx.get("value", 0)), "ether"))
    target_gas_price = tx.get("gasPrice")
    target_max_fee = tx.get("maxFeePerGas")
    target_priority = tx.get("maxPriorityFeePerGas")

    from_addr = tx.get("from")
    if isinstance(from_addr, bytes):
        from_addr = from_addr.hex()
    from_addr = str(from_addr or "").lower().strip()
    if not from_addr.startswith("0x"):
        from_addr = "0x" + from_addr

    subscribed_user_ids = db.get_all_active_targets().get(from_addr, [])
    if not subscribed_user_ids:
        return

    print(f"[{chain_key}] Target mint detected: {tx_hash} from {from_addr}")
    loop = asyncio.get_running_loop()

    for user_id in subscribed_user_ids:
        settings = db.get_sniper_settings(user_id)
        if not settings.get("sniper_active"):
            continue

        state = user_states[user_id]
        state["daily_limit_eth"] = settings.get("daily_limit_eth", 0.05)
        chat_id = state.get("chat_id") or user_id

        # Robust active wallet resolution (falls back to primary wallet in DB)
        wallet_id = state.get("active_wallet_id") or db.get_first_wallet_id(user_id)
        if not wallet_id:
            if tg_bot and chat_id:
                try:
                    await tg_bot.send_message(
                        chat_id=chat_id,
                        text="❌ *Copy Mint Failed*\n\nNo active wallet found. Use /start to create a wallet.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            continue

        wallet = db.get_wallet_by_id(wallet_id, user_id)
        if not wallet:
            continue

        # Send real-time Telegram notification on detection
        if tg_bot and chat_id:
            try:
                bump = settings.get("gas_bump_percent", 30)
                mode_str = "DRY RUN" if settings.get("dry_run", True) else "LIVE"
                await tg_bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"\U0001f3af *Target Mint Detected!*\n\n"
                        f"Chain: *{chain_key.upper()}*\n"
                        f"Target: `{from_addr}`\n"
                        f"Contract: `{contract_address}`\n"
                        f"Qty: `{quantity}` | Value: `{value_eth:.5f} ETH`\n"
                        f"Mode: `{mode_str}` | Gas Bump: *+{bump}%*\n\n"
                        f"\u26a1 Executing copy..."
                    ),
                    parse_mode="Markdown",
                )
            except Exception as e:
                print(f"Failed to send detection notification: {e}")

        # Shared user lock prevents nonce collision across concurrent targets
        async with user_locks[user_id]:
            if not _check_and_record_daily_limit(state, value_eth):
                if tg_bot and chat_id:
                    try:
                        await tg_bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"\u26a0\ufe0f *Daily Limit Exceeded*\n"
                                f"Spent: `{state['daily_spent_eth']:.4f}/{state['daily_limit_eth']} ETH`. "
                                f"Skipping copy mint."
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                continue

            try:
                result = await loop.run_in_executor(
                    None, execute_copy_mint,
                    http_rpc, wallet["private_key"], contract_address, input_data,
                    quantity, value_eth, target_gas_price, target_max_fee, target_priority,
                    settings.get("gas_bump_percent", 30), state.get("max_priority_gwei", 50),
                    state.get("max_base_fee_gwei", 100), settings.get("dry_run", True),
                )
                if not settings.get("dry_run", True):
                    state["daily_spent_eth"] += value_eth
                state["successful_copies"] += 1

                # Send success Telegram notification
                if tg_bot and chat_id:
                    try:
                        if settings.get("dry_run", True):
                            await tg_bot.send_message(
                                chat_id=chat_id,
                                text=f"\U0001f9ea *Copy Mint Simulated (Dry Run)*\n\n`{result}`",
                                parse_mode="Markdown",
                            )
                        else:
                            await tg_bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"\U0001f680 *Copy Mint Broadcasted!*\n\n"
                                    f"Network: *{chain_key.upper()}*\n"
                                    f"TX: `{result}`"
                                ),
                                parse_mode="Markdown",
                            )
                    except Exception as e:
                        print(f"Failed to send success notification: {e}")

            except Exception as e:
                state["failed_copies"] += 1
                if tg_bot and chat_id:
                    try:
                        await tg_bot.send_message(
                            chat_id=chat_id,
                            text=f"\u274c *Copy Mint Failed*\n\n`{str(e)}`",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass


async def _stream_chain(
    chain_key: str,
    ws_url: str,
    http_rpc: str,
    user_states: dict,
    user_locks: dict,
    tg_bot=None,
):
    """
    Dual-stream listener for one chain:
      - newHeads: Scans every newly mined block (250ms on Robinhood/Arbitrum/Base)
      - alchemy_pendingTransactions: Catches pending mempool transactions
    """
    from websockets import connect
    w3 = get_w3(http_rpc)

    while True:
        try:
            async with connect(ws_url, ping_timeout=15) as ws:
                # 1. Subscribe to newHeads (block level stream)
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                    "params": ["newHeads"],
                }))
                sub_heads = json.loads(await ws.recv()).get("result")

                # 2. Subscribe to pending transactions for tracked targets
                addresses = list(db.get_all_active_targets().keys())
                sub_pending = None
                if addresses:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": 2, "method": "eth_subscribe",
                        "params": ["alchemy_pendingTransactions", {"fromAddress": addresses}],
                    }))
                    sub_pending = json.loads(await ws.recv()).get("result")

                print(f"[{chain_key}] Sniper active. Watching blocks & {len(addresses)} target(s)")

                last_refresh = time.time()
                while True:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    if "params" not in msg or "result" not in msg["params"]:
                        continue

                    sub_id = msg["params"].get("subscription")
                    res = msg["params"]["result"]

                    # Case A: newHeads block message -> scan all txs in block
                    if sub_id == sub_heads and isinstance(res, dict) and "number" in res:
                        block_num = int(res["number"], 16)
                        # Fetch full block asynchronously. The WS node can announce a
                        # block slightly before the HTTP RPC node has it indexed, so retry
                        # briefly instead of dropping the block outright.
                        block = None
                        for attempt in range(3):
                            try:
                                block = await asyncio.to_thread(w3.eth.get_block, block_num, full_transactions=True)
                                break
                            except Exception as e:
                                if attempt < 2:
                                    await asyncio.sleep(0.2 * (attempt + 1))
                                else:
                                    print(f"[{chain_key}] Block fetch/scan failed for block {block_num} after 3 attempts: {e}")
                        try:
                            active_targets = set(db.get_all_active_targets().keys())
                            if active_targets and block and block.transactions:
                                for tx in block.transactions:
                                    from_addr = tx.get("from")
                                    if isinstance(from_addr, bytes):
                                        from_addr = from_addr.hex()
                                    from_addr = str(from_addr or "").lower().strip()
                                    if not from_addr.startswith("0x"):
                                        from_addr = "0x" + from_addr

                                    if from_addr in active_targets:
                                        asyncio.create_task(
                                            _handle_target_tx(chain_key, http_rpc, tx, user_states, user_locks, tg_bot)
                                        )
                        except Exception as e:
                            print(f"[{chain_key}] Block scan (post-fetch) failed for block {block_num}: {e}")

                    # Case B: alchemy_pendingTransactions message
                    elif sub_id == sub_pending:
                        if isinstance(res, dict):
                            tx = res
                        else:
                            tx = await asyncio.to_thread(w3.eth.get_transaction, res)
                        if tx:
                            asyncio.create_task(
                                _handle_target_tx(chain_key, http_rpc, tx, user_states, user_locks, tg_bot)
                            )

                    # Re-subscribe every 60s to refresh tracked target list
                    if time.time() - last_refresh > 60:
                        break
        except Exception as e:
            print(f"[{chain_key}] WebSocket disconnected, reconnecting in 3s: {e}")
            await asyncio.sleep(3)


def start_sniper_listeners(user_states: dict, user_locks: dict, tg_bot=None) -> list:
    """Call once at bot startup, from post_init. Returns list of tasks for all chains."""
    tasks = []
    for chain_key, cfg in CHAINS.items():
        if not cfg["ws"] or not cfg["http"]:
            print(f"[{chain_key}] Missing RPC config, skipping")
            continue
        task = asyncio.create_task(
            _stream_chain(chain_key, cfg["ws"], cfg["http"], user_states, user_locks, tg_bot)
        )
        tasks.append(task)
    return tasks
