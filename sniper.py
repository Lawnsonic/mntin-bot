"""
sniper.py. Multi-chain, multi-target, multi-user copy-mint listener.

Covers Robinhood Chain, Arbitrum, Base, and Ethereum via low-latency
Alchemy WebSocket connections.

Runs as background asyncio tasks inside the bot process, sharing db.py,
user_locks, and each user's live settings in bot.py's user_states.
Sends real-time Telegram notifications on detection, simulation, and broadcast.
"""

import asyncio
import json
import os
import time

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

_seen_txs: set = set()


def _mark_seen(tx_hash: str) -> bool:
    """Returns True if already seen. Adds to set if not."""
    if tx_hash in _seen_txs:
        return True
    _seen_txs.add(tx_hash)
    if len(_seen_txs) > 20000:
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
    tx_hash = tx.get("hash")
    if isinstance(tx_hash, bytes):
        tx_hash = tx_hash.hex()
    elif isinstance(tx_hash, str) and not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    if not tx_hash or _mark_seen(tx_hash):
        return

    input_data = tx.get("input") or ""
    if isinstance(input_data, bytes):
        input_data = input_data.hex()
    if len(input_data) < 10:
        return

    func_sig = input_data[:10].lower()
    if func_sig not in MINT_SIGNATURES:
        return

    contract_address = tx.get("to")
    quantity = parse_mint_quantity(input_data, func_sig)
    value_eth = float(Web3.from_wei(int(tx.get("value", 0)), "ether"))
    target_gas_price = tx.get("gasPrice")
    target_max_fee = tx.get("maxFeePerGas")
    target_priority = tx.get("maxPriorityFeePerGas")

    from_addr = (tx.get("from") or "").lower()
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
        chat_id = state.get("chat_id", user_id)

        wallet_id = state.get("active_wallet_id")
        wallet = db.get_wallet_by_id(wallet_id, user_id) if wallet_id else None
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
    """Subscribe to pending transactions on one chain and process matches."""
    from websockets import connect

    while True:
        try:
            async with connect(ws_url, ping_timeout=15) as ws:
                addresses = list(db.get_all_active_targets().keys())
                if not addresses:
                    await asyncio.sleep(5)
                    continue

                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                    "params": ["alchemy_pendingTransactions", {"fromAddress": addresses}],
                }))
                await ws.recv()  # subscription confirmation
                print(f"[{chain_key}] Sniper subscribed to {len(addresses)} target(s)")

                last_refresh = time.time()
                while True:
                    msg = json.loads(await ws.recv())
                    if "params" in msg and "result" in msg["params"]:
                        tx_data = msg["params"]["result"]
                        if isinstance(tx_data, dict):
                            tx = tx_data
                        else:
                            tx = get_w3(http_rpc).eth.get_transaction(tx_data)

                        # Non-blocking dispatch to keep WebSocket receive loop responsive
                        asyncio.create_task(
                            _handle_target_tx(chain_key, http_rpc, tx, user_states, user_locks, tg_bot)
                        )

                    # Re-subscribe every 60s to pick up newly added targets
                    if time.time() - last_refresh > 60:
                        break
        except Exception as e:
            print(f"[{chain_key}] WebSocket error, reconnecting in 3s: {e}")
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
