"""
mint_engine.py. Universal Web3 execution engine.

Supports SeaDrop (OpenSea standard) and standard ERC-721/ERC-1155 minting
across Ethereum, Robinhood Chain, Base, and Arbitrum.
"""

import concurrent.futures
from typing import Optional, Tuple, List

from web3 import Web3
from web3.exceptions import ContractLogicError
from eth_abi import encode, decode

# ============================================================================
# CONSTANTS & SIGNATURES
# ============================================================================

SEADROP_ADDRESS = "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5"
DEFAULT_FEE_RECIPIENT = "0x0000a26b00c1f0df003000390027140000faa719"

MINT_SIGNATURES = {
    "0x1249c58b": ("mint()", [], None),
    "0xa0712d68": ("mint(uint256)", ["uint256"], 0),
    "0x6c2a8e54": ("mintPublic(uint256)", ["uint256"], 0),
    "0x8b94d9a5": ("mintPublic(uint256)", ["uint256"], 0),
    "0xefef39a1": ("mint(uint256,uint256,bytes)", ["uint256", "uint256", "bytes"], 0),
    "0x2309bbed": ("mint(uint256,bytes32[])", ["uint256", "bytes32[]"], 0),
    "0x7ba6b3f1": ("whitelistMint(uint256,bytes32[])", ["uint256", "bytes32[]"], 0),
    "0x4a7d1d5c": ("mint(uint256,bytes)", ["uint256", "bytes"], 0),
    "0x161ac21f": ("mintPublic(address,address,address,uint256)", ["address", "address", "address", "uint256"], 3),
}

PRICE_SELECTORS = [
    "0x13faede6",  # cost()
    "0xa035b1fe",  # price()
    "0x6817c76c",  # mintPrice()
    "0xddca3f43",  # fee()
    "0x98d5fdca",  # getPrice()
]

# Standard direct mint candidate generators: (selector, types, param_builder)
DIRECT_MINT_CANDIDATES = [
    ("0xa0712d68", ["uint256"], lambda qty, addr: [qty]),
    ("0x40c10f19", ["address", "uint256"], lambda qty, addr: [addr, qty]),
    ("0x6c2a8e54", ["uint256"], lambda qty, addr: [qty]),
    ("0x8b94d9a5", ["uint256"], lambda qty, addr: [qty]),
    ("0x379607f5", ["uint256"], lambda qty, addr: [qty]),
    ("0x1249c58b", [], lambda qty, addr: []),
    ("0x4e71d92d", [], lambda qty, addr: []),
    ("0x0d9f64f7", [], lambda qty, addr: []),
    ("0x0e89341c", [], lambda qty, addr: []),
    ("0x347c6e00", ["uint256"], lambda qty, addr: [qty]),
]


# ============================================================================
# HELPERS
# ============================================================================

_w3_cache: dict = {}


def get_w3(rpc_url: str) -> Web3:
    """Get or create a cached Web3 instance with strict timeout for the given RPC endpoint."""
    if rpc_url not in _w3_cache:
        _w3_cache[rpc_url] = Web3(
            Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 3.0})
        )
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
    Uses 2.5x base fee buffer so natural base fee fluctuations never cause reverts.
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
        tx_data["maxFeePerGas"] = int(base_fee * 2.5) + priority_fee
        tx_data["maxPriorityFeePerGas"] = priority_fee
    else:
        tx_data["gasPrice"] = int(w3.eth.gas_price * 1.5)
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
            if isinstance(qty, int) and 0 < qty <= 1000:
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
# SEADROP & MINT PRICE DETECTION
# ============================================================================

def get_seadrop_public_drop(rpc_url: str, contract_address: str) -> Tuple[bool, Optional[float], Optional[str]]:
    """
    Check if the given contract has an active public drop on SeaDrop.
    Returns (is_seadrop, mint_price_eth, fee_recipient).
    """
    try:
        w3 = get_w3(rpc_url)
        seadrop = Web3.to_checksum_address(SEADROP_ADDRESS)
        nft = Web3.to_checksum_address(contract_address)

        # getPublicDrop(address) -> selector 0xbc6a629c
        sel_pub = "0xbc6a629c"
        data = sel_pub + "000000000000000000000000" + nft[2:].lower()
        res = w3.eth.call({"to": seadrop, "data": data})
        if res and len(res) >= 32:
            decoded = decode(["uint80", "uint48", "uint48", "uint16", "uint16", "bool"], res)
            price_wei = decoded[0]
            price_eth = float(w3.from_wei(price_wei, "ether"))

            # Fee recipient lookup
            fee_recipient = DEFAULT_FEE_RECIPIENT
            try:
                # getAllowedFeeRecipients(address) -> 0xb7d10b7d
                sel_recip = "0xb7d10b7d" + "000000000000000000000000" + nft[2:].lower()
                res_recip = w3.eth.call({"to": seadrop, "data": sel_recip})
                recips = decode(["address[]"], res_recip)[0]
                if recips:
                    fee_recipient = recips[0]
            except Exception:
                pass

            return True, price_eth, fee_recipient
    except Exception:
        pass
    return False, None, None


def detect_mint_price(rpc_url: str, contract_address: str) -> Optional[float]:
    """
    Detect per-token mint price across SeaDrop and custom NFT contracts.
    """
    is_sd, sd_price, _ = get_seadrop_public_drop(rpc_url, contract_address)
    if is_sd and sd_price is not None:
        return sd_price

    w3 = get_w3(rpc_url)
    checksum = Web3.to_checksum_address(contract_address)

    def _probe_selector(selector: str) -> Optional[float]:
        try:
            result = w3.eth.call({"to": checksum, "data": selector})
            if result and len(result) >= 32:
                price_wei = int.from_bytes(result[:32], byteorder="big")
                if price_wei >= 0:
                    return float(w3.from_wei(price_wei, "ether"))
        except Exception:
            pass
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PRICE_SELECTORS)) as executor:
        results = list(executor.map(_probe_selector, PRICE_SELECTORS))

    for price in results:
        if price is not None:
            return price
    return 0.0


def build_mint_payload(
    rpc_url: str,
    minter_address: str,
    contract_address: str,
    quantity: int = 1,
) -> Tuple[str, str]:
    """
    Determine the correct contract destination and calldata for minting.
    Returns (target_address, calldata_hex).
    """
    w3 = get_w3(rpc_url)
    minter = Web3.to_checksum_address(minter_address)
    nft = Web3.to_checksum_address(contract_address)

    # 1. Check if SeaDrop collection
    is_sd, _, fee_recipient = get_seadrop_public_drop(rpc_url, contract_address)
    if is_sd:
        seadrop = Web3.to_checksum_address(SEADROP_ADDRESS)
        recip = Web3.to_checksum_address(fee_recipient or DEFAULT_FEE_RECIPIENT)
        # mintPublic(address,address,address,uint256) -> selector 0x161ac21f
        calldata = "0x161ac21f" + encode(
            ["address", "address", "address", "uint256"],
            [nft, recip, minter, quantity]
        ).hex()
        return seadrop, calldata

    # 2. Try direct mint candidates on custom contracts
    for selector, types, param_builder in DIRECT_MINT_CANDIDATES:
        params = param_builder(quantity, minter)
        if types:
            encoded_args = encode(types, params).hex()
            calldata = selector + encoded_args
        else:
            calldata = selector
        try:
            w3.eth.call({"from": minter, "to": nft, "data": calldata, "value": 0})
            return nft, calldata
        except Exception:
            continue

    # Fallback default: mint(uint256)
    return nft, "0xa0712d68" + encode(["uint256"], [quantity]).hex()


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
    Build, simulate, sign and broadcast a mint transaction.
    Automatically detects SeaDrop and custom ERC721 contracts.
    """
    w3 = get_w3(rpc_url)
    account = w3.eth.account.from_key(private_key)

    target_addr, calldata = build_mint_payload(
        rpc_url, account.address, contract_address, quantity
    )

    total_value_wei = w3.to_wei(value_eth * quantity, "ether")
    current_balance = w3.eth.get_balance(account.address)
    if current_balance < total_value_wei:
        raise ValueError(
            f"Insufficient balance. Needed {value_eth * quantity:.5f} ETH, "
            f"wallet holds {w3.from_wei(current_balance, 'ether'):.5f} ETH."
        )

    tx_data = {
        "from": account.address,
        "to": target_addr,
        "data": calldata,
        "value": total_value_wei,
        "nonce": w3.eth.get_transaction_count(account.address, "pending"),
        "chainId": w3.eth.chain_id,
    }
    tx_data = _build_fee_fields(w3, tx_data, max_priority_fee_gwei, max_base_fee_gwei)

    try:
        estimated_gas = w3.eth.estimate_gas(tx_data)
        tx_data["gas"] = int(estimated_gas * 1.25)
    except ContractLogicError as sim_err:
        raise RuntimeError(
            f"Simulation failed (tx would revert): {sim_err}"
        ) from sim_err
    except Exception as sim_err:
        raise RuntimeError(f"Gas estimation failed: {sim_err}") from sim_err

    # Final balance check including gas
    fee_per_gas = tx_data.get("maxFeePerGas", tx_data.get("gasPrice", 0))
    total_cost = total_value_wei + (tx_data["gas"] * fee_per_gas)
    if current_balance < total_cost:
        raise ValueError("Insufficient balance to cover mint price plus gas.")

    signed_tx = w3.eth.account.sign_transaction(tx_data, private_key=private_key)
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
        max_fee = int(base_fee * 2.0) + priority_fee
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
    tx_data["gas"] = int(estimated * 1.25)

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
