"""
mint_engine.py. Stateless Web3 execution engine.

Every function accepts rpc_url and private_key as parameters so the same
engine serves multiple users on multiple chains without any module-level
state.
"""

from typing import Optional

from web3 import Web3
from web3.exceptions import ContractLogicError
from eth_abi import decode

# ============================================================================
# CONSTANTS
# ============================================================================

DEFAULT_MINT_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "quantity", "type": "uint256"}],
        "name": "mint",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    }
]

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

# Corrected 4-byte selectors for standard NFT price view functions.
# Three of the five commonly cited selectors are wrong in most tutorials.
# These were verified by computing keccak256 of each function signature.
PRICE_SELECTORS = [
    "0x13faede6",  # cost()
    "0xa035b1fe",  # price()
    "0x6817c76c",  # mintPrice()
    "0xddca3f43",  # fee()
    "0x98d5fdca",  # getPrice()
]


# ============================================================================
# HELPERS
# ============================================================================

_w3_cache: dict = {}


def get_w3(rpc_url: str) -> Web3:
    """Get or create a cached Web3 instance for the given RPC endpoint."""
    if rpc_url not in _w3_cache:
        _w3_cache[rpc_url] = Web3(Web3.HTTPProvider(rpc_url))
    return _w3_cache[rpc_url]


def verify_contract_exists(rpc_url: str, address: str) -> str:
    """Verify target is a contract (not an EOA). Returns checksummed address."""
    w3 = get_w3(rpc_url)
    checksum_addr = Web3.to_checksum_address(address)
    code = w3.eth.get_code(checksum_addr)
    if code in (b"", b"\x00", "0x", b"0x"):
        raise ValueError(
            f"Address {checksum_addr} has no bytecode. "
            "Target is an EOA or uninitialized contract."
        )
    return checksum_addr


def _build_fee_fields(
    w3: Web3,
    tx_data: dict,
    priority_fee_gwei: int = 2,
    max_base_fee_gwei: int = 100,
) -> dict:
    """
    Populate EIP-1559 or legacy gas fields on tx_data in-place.
    Preserves the base-fee ceiling check from the original codebase.
    """
    latest_block = w3.eth.get_block("latest")
    if latest_block.get("baseFeePerGas") is not None:
        base_fee = latest_block["baseFeePerGas"]
        base_fee_gwei_actual = float(w3.from_wei(base_fee, "gwei"))
        if base_fee_gwei_actual > max_base_fee_gwei:
            raise RuntimeError(
                f"Base fee ({base_fee_gwei_actual:.1f} Gwei) exceeds "
                f"ceiling ({max_base_fee_gwei} Gwei)."
            )
        priority_fee = w3.to_wei(priority_fee_gwei, "gwei")
        tx_data["maxFeePerGas"] = int(base_fee * 1.5) + priority_fee
        tx_data["maxPriorityFeePerGas"] = priority_fee
    else:
        tx_data["gasPrice"] = w3.eth.gas_price
    return tx_data


def parse_mint_quantity(input_data: str, func_sig: str) -> int:
    """Extract mint quantity from calldata. Pure function, no Web3 needed."""
    if func_sig not in MINT_SIGNATURES:
        return 1
    _name, types, qty_idx = MINT_SIGNATURES[func_sig]
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


# ============================================================================
# BALANCE
# ============================================================================

def get_balance(rpc_url: str, address: str) -> float:
    """Get ETH balance for an address. Returns 0.0 on any failure."""
    try:
        w3 = get_w3(rpc_url)
        checksum = Web3.to_checksum_address(address)
        return float(w3.from_wei(w3.eth.get_balance(checksum), "ether"))
    except Exception:
        return 0.0


# ============================================================================
# MINT PRICE DETECTION
# ============================================================================

def detect_mint_price(rpc_url: str, contract_address: str) -> Optional[float]:
    """
    Probe contract bytecode for standard price view functions.
    Returns the per-token price in ETH, or None if no price function found.
    Falls through silently on reverts (most contracts only implement one).
    """
    w3 = get_w3(rpc_url)
    checksum = Web3.to_checksum_address(contract_address)
    for selector in PRICE_SELECTORS:
        try:
            result = w3.eth.call({"to": checksum, "data": selector})
            if result and len(result) >= 32:
                price_wei = int.from_bytes(result[:32], byteorder="big")
                if price_wei >= 0:
                    return float(w3.from_wei(price_wei, "ether"))
        except Exception:
            continue
    return None


# ============================================================================
# WITHDRAW (ETH transfer)
# ============================================================================

def execute_withdraw(
    rpc_url: str,
    private_key: str,
    to_address: str,
    amount_eth: float,
) -> str:
    """
    Send ETH from the user's wallet to a destination address.
    Uses estimate_gas (not hardcoded 21000) to support smart-account
    destinations on Robinhood Chain's ERC-4337 accounts.
    """
    w3 = get_w3(rpc_url)
    account = w3.eth.account.from_key(private_key)
    to_checksum = Web3.to_checksum_address(to_address)

    amount_wei = w3.to_wei(amount_eth, "ether")
    balance = w3.eth.get_balance(account.address)

    tx = {
        "from": account.address,
        "to": to_checksum,
        "value": amount_wei,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "chainId": w3.eth.chain_id,
    }
    tx = _build_fee_fields(w3, tx)

    try:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    except Exception:
        tx["gas"] = 21000  # fallback for plain EOA destinations

    fee_per_gas = tx.get("maxFeePerGas", tx.get("gasPrice", 0))
    total_cost = amount_wei + (tx["gas"] * fee_per_gas)
    if balance < total_cost:
        raise ValueError("Insufficient funds to cover amount plus gas.")

    signed_tx = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    return w3.to_hex(tx_hash)


# ============================================================================
# DIRECT MINT
# ============================================================================

def execute_mint(
    rpc_url: str,
    private_key: str,
    contract_address: str,
    quantity: int = 1,
    value_eth: float = 0.0,
    max_priority_fee_gwei: int = 2,
    max_base_fee_gwei: int = 100,
) -> str:
    """
    Build, simulate, sign and broadcast a mint(uint256) transaction.
    value_eth is the per-token price. Total value = value_eth * quantity.
    Raises RuntimeError on simulation failure (no gas spent).
    """
    w3 = get_w3(rpc_url)
    account = w3.eth.account.from_key(private_key)

    target_addr = verify_contract_exists(rpc_url, contract_address)
    contract = w3.eth.contract(address=target_addr, abi=DEFAULT_MINT_ABI)

    # value_eth is per-token price, scale by quantity
    total_value_wei = w3.to_wei(value_eth * quantity, "ether")
    current_balance = w3.eth.get_balance(account.address)
    if current_balance < total_value_wei:
        raise ValueError(
            f"Insufficient balance. Needed {value_eth * quantity:.5f} ETH, "
            f"wallet holds {w3.from_wei(current_balance, 'ether'):.5f} ETH."
        )

    tx_data = {
        "from": account.address,
        "value": total_value_wei,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "chainId": w3.eth.chain_id,
    }
    tx_data = _build_fee_fields(w3, tx_data, max_priority_fee_gwei, max_base_fee_gwei)

    tx = contract.functions.mint(quantity).build_transaction(tx_data)

    try:
        estimated_gas = w3.eth.estimate_gas(tx)
        tx["gas"] = int(estimated_gas * 1.2)
    except ContractLogicError as sim_err:
        raise RuntimeError(
            f"Simulation failed (tx would revert): {sim_err}"
        ) from sim_err
    except Exception as sim_err:
        raise RuntimeError(f"Gas estimation failed: {sim_err}") from sim_err

    # Final balance check including gas
    total_cost = total_value_wei + (
        tx["gas"] * tx_data.get("maxFeePerGas", tx_data.get("gasPrice", 0))
    )
    if current_balance < total_cost:
        raise ValueError("Insufficient balance to cover mint price plus gas.")

    signed_tx = w3.eth.account.sign_transaction(tx, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    return w3.to_hex(tx_hash)


# ============================================================================
# COPY MINT (Sniper)
# ============================================================================

def execute_copy_mint(
    rpc_url: str,
    private_key: str,
    contract_address: str,
    raw_calldata: str,
    quantity: int,
    value_eth: float,
    target_gas_price: Optional[int],
    target_max_fee: Optional[int],
    target_priority: Optional[int],
    gas_bump_percent: int = 30,
    max_priority_gwei: int = 50,
    max_base_fee_gwei: int = 150,
    dry_run: bool = True,
) -> str:
    """
    Replay a detected mint transaction with bumped gas.
    Returns tx hash on success, or a dry-run message string.
    Raises on failure (caller handles daily limit tracking).
    """
    w3 = get_w3(rpc_url)
    account = w3.eth.account.from_key(private_key)

    target_addr = verify_contract_exists(rpc_url, contract_address)

    required_value_wei = w3.to_wei(value_eth, "ether")
    current_balance = w3.eth.get_balance(account.address)
    if current_balance < required_value_wei:
        raise ValueError("Insufficient balance for copy trade.")

    data = raw_calldata if raw_calldata.startswith("0x") else "0x" + raw_calldata

    tx_data = {
        "from": account.address,
        "to": target_addr,
        "value": required_value_wei,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "chainId": w3.eth.chain_id,
        "data": data,
    }

    # Gas fee construction: bump target's gas by gas_bump_percent
    latest_block = w3.eth.get_block("latest")
    if latest_block.get("baseFeePerGas") is not None:
        base_fee = latest_block["baseFeePerGas"]
        if target_priority:
            bumped = int(target_priority * (1 + gas_bump_percent / 100))
            priority_fee = min(bumped, w3.to_wei(max_priority_gwei, "gwei"))
        else:
            priority_fee = w3.to_wei(2, "gwei")
        max_fee = int(base_fee * 1.5) + priority_fee
        max_fee_limit = w3.to_wei(max_base_fee_gwei, "gwei")
        if max_fee > max_fee_limit:
            max_fee = max_fee_limit
        tx_data["maxFeePerGas"] = max_fee
        tx_data["maxPriorityFeePerGas"] = priority_fee
    else:
        if target_gas_price:
            gas_price = int(target_gas_price * (1 + gas_bump_percent / 100))
        else:
            gas_price = w3.eth.gas_price
        tx_data["gasPrice"] = min(gas_price, w3.to_wei(max_base_fee_gwei, "gwei"))

    # Simulate
    estimated = w3.eth.estimate_gas(tx_data)
    tx_data["gas"] = int(estimated * 1.2)

    gas_cost_wei = tx_data["gas"] * tx_data.get(
        "maxFeePerGas", tx_data.get("gasPrice", 0)
    )
    total_cost_wei = required_value_wei + gas_cost_wei
    total_cost_eth = float(w3.from_wei(total_cost_wei, "ether"))

    if current_balance < total_cost_wei:
        raise ValueError("Insufficient balance for gas plus mint.")

    if dry_run:
        return (
            f"[DRY_RUN_PASS] Simulated {quantity} NFTs for "
            f"{total_cost_eth:.4f} ETH total"
        )

    signed = w3.eth.account.sign_transaction(tx_data, private_key=private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    return w3.to_hex(tx_hash)
