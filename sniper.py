"""
sniper.py. Multi-chain, multi-target, multi-user copy-mint listener.

Covers the fee-market chains only (Arbitrum, Base, Ethereum). Robinhood
Chain is excluded here since its FCFS sequencer makes gas-bump copying
pointless there; that chain needs a separate latency-based approach.

Runs as background asyncio tasks inside the bot process, sharing db.py
and each user's live settings in bot.py's user_states.
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

FEE_MARKET_CHAINS = {
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


async def _handle_target_tx(chain_key: str, http_rpc: str, tx: dict, user_states: dict):
    """Process a detected target transaction and fan out to subscribed users."""
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

    print(f"[{chain_key}] target mint detected: {tx_hash} from {from_addr}")
    loop = asyncio.get_running_loop()

    for user_id in subscribed_user_ids:
        state = user_states[user_id]
        if not state.get("sniper_active"):
            continue

        wallet_id = state.get("active_wallet_id")
        wallet = db.get_wallet_by_id(wallet_id, user_id) if wallet_id else None
        if not wallet:
            continue

        if not _check_and_record_daily_limit(state, value_eth):
            print(f"[{chain_key}] user {user_id} hit daily limit, skipping")
            continue

        try:
            result = await loop.run_in_executor(
                None, execute_copy_mint,
                http_rpc, wallet["private_key"], contract_address, input_data,
                quantity, value_eth, target_gas_price, target_max_fee, target_priority,
                state.get("gas_bump_percent", 30), state.get("max_priority_gwei", 50),
                state.get("max_base_fee_gwei", 100), state.get("dry_run", True),
            )
            state["daily_spent_eth"] += value_eth
            state["successful_copies"] += 1
            print(f"[{chain_key}] user {user_id}: {result}")
        except Exception as e:
            state["failed_copies"] += 1
            print(f"[{chain_key}] user {user_id} copy failed: {e}")


async def _stream_chain(chain_key: str, ws_url: str, http_rpc: str, user_states: dict):
    """Subscribe to pending transactions on one chain and process matches."""
    from websockets import connect

    while True:
        try:
            async with connect(ws_url) as ws:
                addresses = list(db.get_all_active_targets().keys())
                if not addresses:
                    await asyncio.sleep(10)
                    continue

                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                    "params": ["alchemy_pendingTransactions", {"fromAddress": addresses}],
                }))
                await ws.recv()  # subscription confirmation
                print(f"[{chain_key}] subscribed to {len(addresses)} target(s)")

                last_refresh = time.time()
                while True:
                    msg = json.loads(await ws.recv())
                    if "params" in msg and "result" in msg["params"]:
                        tx_data = msg["params"]["result"]
                        if isinstance(tx_data, dict):
                            tx = tx_data
                        else:
                            tx = get_w3(http_rpc).eth.get_transaction(tx_data)
                        await _handle_target_tx(chain_key, http_rpc, tx, user_states)

                    # Re-subscribe every 60s to pick up newly added targets.
                    # Brief gap on reconnect where events can be missed.
                    if time.time() - last_refresh > 60:
                        break
        except Exception as e:
            print(f"[{chain_key}] stream error, reconnecting in 3s: {e}")
            await asyncio.sleep(3)


def start_sniper_listeners(user_states: dict) -> list:
    """Call once at bot startup, from post_init. Returns list of tasks."""
    tasks = []
    for chain_key, cfg in FEE_MARKET_CHAINS.items():
        if not cfg["ws"]:
            print(f"[{chain_key}] no WS RPC configured, skipping")
            continue
        task = asyncio.create_task(
            _stream_chain(chain_key, cfg["ws"], cfg["http"], user_states)
        )
        tasks.append(task)
    return tasks
