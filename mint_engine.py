import os
from web3 import Web3
from web3.exceptions import ContractLogicError
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("HTTP_RPC")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
MAX_BASE_FEE_GWEI = int(os.getenv("MAX_BASE_FEE_GWEI", "100"))

if not RPC_URL or not PRIVATE_KEY:
    raise ValueError("Missing HTTP_RPC or PRIVATE_KEY in environment variables.")

w3 = Web3(Web3.HTTPProvider(RPC_URL))
account = w3.eth.account.from_key(PRIVATE_KEY)

DEFAULT_MINT_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "quantity", "type": "uint256"}],
        "name": "mint",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    }
]

def verify_contract_exists(address: str) -> str:
    checksum_addr = Web3.to_checksum_address(address)
    code = w3.eth.get_code(checksum_addr)
    if code in (b"", b"\x00", "0x", b"0x"):
        raise ValueError(f"Address {checksum_addr} has no bytecode. Target is an EOA or uninitialized contract.")
    return checksum_addr

def execute_mint(contract_address: str, quantity: int = 1, value_eth: float = 0.0, max_priority_fee_gwei: int = 2) -> str:
    target_addr = verify_contract_exists(contract_address)
    contract = w3.eth.contract(address=target_addr, abi=DEFAULT_MINT_ABI)
    
    required_value_wei = w3.to_wei(value_eth, "ether")
    current_balance = w3.eth.get_balance(account.address)
    
    if current_balance < required_value_wei:
        raise ValueError(f"Insufficient wallet balance. Needed {value_eth} ETH, but wallet holds {w3.from_wei(current_balance, 'ether'):.5f} ETH.")

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
        
        if base_fee_gwei > MAX_BASE_FEE_GWEI:
            raise RuntimeError(f"Base fee ({base_fee_gwei:.1f} Gwei) exceeds ceiling ({MAX_BASE_FEE_GWEI} Gwei).")
            
        priority_fee = w3.to_wei(max_priority_fee_gwei, "gwei")
        max_fee = int(base_fee * 1.5) + priority_fee
        tx_data["maxFeePerGas"] = max_fee
        tx_data["maxPriorityFeePerGas"] = priority_fee
    else:
        tx_data["gasPrice"] = w3.eth.gas_price

    tx = contract.functions.mint(quantity).build_transaction(tx_data)

    try:
        estimated_gas = w3.eth.estimate_gas(tx)
        tx["gas"] = int(estimated_gas * 1.2)
    except ContractLogicError as sim_err:
        raise RuntimeError(f"Simulation failed (tx would revert): {sim_err}") from sim_err
    except Exception as sim_err:
        raise RuntimeError(f"Gas estimation failed: {sim_err}") from sim_err

    total_cost_estimate = required_value_wei + (tx["gas"] * tx_data.get("maxFeePerGas", tx_data.get("gasPrice", 0)))
    if current_balance < total_cost_estimate:
        raise ValueError("Insufficient balance to cover mint price plus gas.")

    signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    return w3.to_hex(tx_hash)

def wait_for_receipt(tx_hash_hex: str, timeout: int = 120):
    return w3.eth.wait_for_transaction_receipt(tx_hash_hex, timeout=timeout)
