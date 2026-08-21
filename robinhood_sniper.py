"""
robinhood_sniper.py. FCFS latency-based sniper for Robinhood Chain.

Robinhood Chain (chain ID 4663) uses an FCFS sequencer, meaning
gas-bumping is pointless. Instead, this module:
  1. Pre-signs the mint transaction (supporting SeaDrop & direct mint)
  2. Polls for contract deployment or mint-open state
  3. Blasts the pre-signed tx to multiple RPCs concurrently

The first submission to arrive wins, so latency is everything.
"""

import asyncio
import time
from typing import List

from web3 import Web3
from mint_engine import get_w3, build_mint_payload


def prepare_mint_tx(
    rpc_url: str,
    private_key: str,
    contract_address: str,
    quantity: int,
    value_eth: float,
) -> dict:
    """
    Build and sign a mint transaction ahead of time.
    Supports both SeaDrop and custom ERC721 contracts.
    Uses 3.0x base fee buffer so the pre-signed transaction is never
    rejected with 'max fee per gas less than block base fee'.
    """
    w3 = get_w3(rpc_url)
    account = w3.eth.account.from_key(private_key)

    target_addr, calldata = build_mint_payload(
        rpc_url, account.address, contract_address, quantity
    )

    total_value_wei = w3.to_wei(value_eth * quantity, "ether")
    tx_data = {
        "from": account.address,
        "to": target_addr,
        "data": calldata,
        "value": total_value_wei,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "chainId": w3.eth.chain_id,
        "gas": 350000,
    }

    latest_block = w3.eth.get_block("latest")
    if latest_block.get("baseFeePerGas") is not None:
        base_fee = latest_block["baseFeePerGas"]
        priority_fee = w3.to_wei(0.1, "gwei")
        tx_data["maxFeePerGas"] = int(base_fee * 3.0) + priority_fee
        tx_data["maxPriorityFeePerGas"] = priority_fee
    else:
        tx_data["gasPrice"] = int(w3.eth.gas_price * 2.0)

    signed = w3.eth.account.sign_transaction(tx_data, private_key=private_key)
    return {"raw": signed.raw_transaction, "hash": w3.to_hex(signed.hash)}


def _send_one(rpc_url: str, raw_tx: bytes):
    return get_w3(rpc_url).eth.send_raw_transaction(raw_tx)


async def broadcast_via_all(rpc_urls: List[str], raw_tx: bytes) -> str:
    """
    Fire the pre-signed payload at multiple RPC nodes concurrently.
    Returns the tx hash from whichever node accepts it first.
    """
    tasks = [
        asyncio.create_task(asyncio.to_thread(_send_one, u, raw_tx))
        for u in rpc_urls
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for p in pending:
        p.cancel()

    for t in done:
        if not t.cancelled() and not t.exception():
            return Web3.to_hex(t.result())

    for t in done:
        if t.exception():
            raise t.exception()
    return ""


async def _poll_one(rpc_url: str, checksum: str, poll_interval: float, deadline: float) -> bool:
    """Poll a single RPC node for bytecode, ignoring transient network/RPC glitches."""
    w3 = get_w3(rpc_url)
    while time.time() < deadline:
        try:
            code = await asyncio.to_thread(w3.eth.get_code, checksum)
            if code not in (b"", b"\x00", "0x", b"0x"):
                return True
        except Exception:
            pass  # Transient RPC hiccup; keep polling
        await asyncio.sleep(poll_interval)
    return False


async def wait_for_mint_open(
    rpc_urls: List[str],
    contract_address: str,
    poll_interval: float = 0.25,
    timeout: float = 600,
) -> bool:
    """
    Poll all RPC nodes concurrently for contract bytecode to appear.
    Returns True the instant any node detects bytecode, False on timeout.
    """
    checksum = Web3.to_checksum_address(contract_address)
    deadline = time.time() + timeout

    tasks = [
        asyncio.create_task(_poll_one(u, checksum, poll_interval, deadline))
        for u in rpc_urls
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for p in pending:
        p.cancel()

    return any(t.result() for t in done if not t.cancelled() and not t.exception())
