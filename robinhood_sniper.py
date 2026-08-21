"""
robinhood_sniper.py. FCFS latency-based sniper for Robinhood Chain.

Robinhood Chain (chain ID 4663) uses an FCFS sequencer, meaning
gas-bumping is pointless. Instead, this module:
  1. Pre-signs the mint transaction (no estimate_gas round trip)
  2. Polls for contract deployment or mint-open state
  3. Blasts the pre-signed tx to multiple RPCs concurrently

The first submission to arrive wins, so latency is everything.
"""

import asyncio
import time
from typing import List

from web3 import Web3
from mint_engine import get_w3, DEFAULT_MINT_ABI


def prepare_mint_tx(
    rpc_url: str,
    private_key: str,
    contract_address: str,
    quantity: int,
    value_eth: float,
) -> dict:
    """
    Build and sign a mint transaction ahead of time.
    Hardcodes gas to 300k to skip the estimate_gas HTTP round trip
    that would add latency at execution time.
    Returns dict with 'raw' (signed bytes) and 'hash' (hex string).
    """
    w3 = get_w3(rpc_url)
    account = w3.eth.account.from_key(private_key)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(contract_address),
        abi=DEFAULT_MINT_ABI,
    )

    tx_data = {
        "from": account.address,
        "value": w3.to_wei(value_eth * quantity, "ether"),
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "chainId": w3.eth.chain_id,
        "gas": 300000,
        "gasPrice": w3.eth.gas_price,
    }

    tx = contract.functions.mint(quantity).build_transaction(tx_data)
    signed = w3.eth.account.sign_transaction(tx, private_key=private_key)

    return {"raw": signed.raw_transaction, "hash": w3.to_hex(signed.hash)}


async def broadcast_via_all(rpc_urls: List[str], raw_tx: bytes) -> str:
    """
    Fire the pre-signed payload at multiple RPC nodes concurrently.
    Returns the tx hash from whichever node accepts it first.
    """
    def send_one(rpc_url):
        return get_w3(rpc_url).eth.send_raw_transaction(raw_tx)

    tasks = [
        asyncio.create_task(asyncio.to_thread(send_one, u))
        for u in rpc_urls
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for p in pending:
        p.cancel()

    return Web3.to_hex(list(done)[0].result())


async def wait_for_mint_open(
    rpc_url: str,
    contract_address: str,
    poll_interval: float = 0.25,
    timeout: float = 600,
) -> bool:
    """
    Poll for contract bytecode to appear (stealth drop detection).
    Returns True when bytecode is found, False on timeout.
    The 250ms interval balances latency vs RPC rate limits.
    """
    w3 = get_w3(rpc_url)
    checksum = Web3.to_checksum_address(contract_address)
    deadline = time.time() + timeout

    while time.time() < deadline:
        code = await asyncio.to_thread(w3.eth.get_code, checksum)
        if code not in (b"", b"\x00"):
            return True
        await asyncio.sleep(poll_interval)

    return False
