import os
import asyncio
import json
import time
from typing import Optional, Set
from dataclasses import dataclass
from web3 import Web3
from eth_abi import decode
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:
    HTTP_RPC: str = os.getenv("HTTP_RPC")
    WS_RPC: str = os.getenv("WS_RPC")
    PRIVATE_KEY: str = os.getenv("PRIVATE_KEY")
    TARGET_WALLET: str = os.getenv("TARGET_WALLET", "").lower()
    MAX_BASE_FEE_GWEI: int = int(os.getenv("MAX_BASE_FEE_GWEI", "150"))
    DAILY_LIMIT_ETH: float = float(os.getenv("DAILY_LIMIT_ETH", "5.0"))
    GAS_BUMP_PERCENT: int = 30
    MAX_PRIORITY_FEE_GWEI: int = 50
    DRY_RUN: bool = True  # Set to False to enable actual broadcasting

CONFIG = Config()

if not CONFIG.HTTP_RPC or not CONFIG.WS_RPC or not CONFIG.PRIVATE_KEY or not CONFIG.TARGET_WALLET:
    raise ValueError("Missing critical environment variables in .env")

w3 = Web3(Web3.HTTPProvider(CONFIG.HTTP_RPC))
account = w3.eth.account.from_key(CONFIG.PRIVATE_KEY)
user_address = account.address

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

class SniperState:
    def __init__(self):
        self.seen_txs: Set[str] = set()
        self.daily_spent_eth: float = 0.0
        self.daily_reset_time: float = time.time()
        self.successful_copies: int = 0
        self.failed_copies: int = 0
        
    def check_daily_limit(self, amount_eth: float) -> bool:
        if time.time() - self.daily_reset_time > 86400:
            self.daily_spent_eth = 0.0
            self.daily_reset_time = time.time()
        return (self.daily_spent_eth + amount_eth) <= CONFIG.DAILY_LIMIT_ETH
    
    def record_spend(self, amount_eth: float):
        self.daily_spent_eth += amount_eth
        
    def is_seen(self, tx_hash: str) -> bool:
        if tx_hash in self.seen_txs:
            return True
        self.seen_txs.add(tx_hash)
        if len(self.seen_txs) > 10000:
            self.seen_txs = set(list(self.seen_txs)[-5000:])
        return False

STATE = SniperState()

def verify_contract_exists(address: str) -> str:
    checksum_addr = Web3.to_checksum_address(address)
    code = w3.eth.get_code(checksum_addr)
    if code in (b"", b"\x00", "0x", b"0x"):
        raise ValueError(f"Address {checksum_addr} has no bytecode.")
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
    except Exception as e:
        print(f"Decode error: {e}")

    return 1

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
        print(f"\u274c Daily limit would be exceeded on mint value alone ({STATE.daily_spent_eth}/{CONFIG.DAILY_LIMIT_ETH} ETH)")
        return None

    try:
        target_addr = verify_contract_exists(contract_address)
    except ValueError as e:
        print(f"\u274c {e}")
        return None

    required_value_wei = w3.to_wei(value_eth, "ether")
    current_balance = w3.eth.get_balance(user_address)

    if current_balance < required_value_wei:
        print(f"\u274c Insufficient balance")
        return None

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
            bumped_priority = int(target_priority * (1 + CONFIG.GAS_BUMP_PERCENT / 100))
            priority_fee = min(bumped_priority, w3.to_wei(CONFIG.MAX_PRIORITY_FEE_GWEI, "gwei"))
        else:
            priority_fee = w3.to_wei(2, "gwei")
            
        max_fee = int(base_fee * 1.5) + priority_fee
        max_fee_gwei_limit = w3.to_wei(CONFIG.MAX_BASE_FEE_GWEI, "gwei")
        if max_fee > max_fee_gwei_limit:
            print(f"\u26a0\ufe0f Max fee {w3.from_wei(max_fee, 'gwei')} gwei exceeds limit, capping")
            max_fee = max_fee_gwei_limit
            
        tx_data["maxFeePerGas"] = max_fee
        tx_data["maxPriorityFeePerGas"] = priority_fee
    else:
        if target_gas_price:
            gas_price = int(target_gas_price * (1 + CONFIG.GAS_BUMP_PERCENT / 100))
        else:
            gas_price = w3.eth.gas_price
        tx_data["gasPrice"] = min(gas_price, w3.to_wei(CONFIG.MAX_BASE_FEE_GWEI, "gwei"))

    try:
        estimated = w3.eth.estimate_gas(tx_data)
        tx_data["gas"] = int(estimated * 1.2)
    except Exception as e:
        print(f"\u274c Simulation failed: {e}")
        return None

    gas_cost_wei = tx_data["gas"] * tx_data.get("maxFeePerGas", tx_data.get("gasPrice", 0))
    total_cost_wei = required_value_wei + gas_cost_wei
    total_cost_eth = float(w3.from_wei(total_cost_wei, "ether"))

    if current_balance < total_cost_wei:
        print(f"\u274c Insufficient balance for gas + mint")
        return None

    if not STATE.check_daily_limit(total_cost_eth):
        print(f"\u274c Daily limit would be exceeded including gas ({STATE.daily_spent_eth + total_cost_eth:.4f}/{CONFIG.DAILY_LIMIT_ETH} ETH)")
        return None

    if CONFIG.DRY_RUN:
        print(f"\u2705 [DRY RUN] Simulation passed. Would broadcast tx for {quantity} items costing {total_cost_eth:.4f} ETH.")
        return "0x_dry_run_success"

    try:
        signed = w3.eth.account.sign_transaction(tx_data, private_key=CONFIG.PRIVATE_KEY)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        hex_hash = w3.to_hex(tx_hash)

        STATE.record_spend(total_cost_eth)
        print(f"\u2705 Copy mint sent: {hex_hash}")
        return hex_hash

    except Exception as e:
        print(f"\u274c Send failed: {e}")
        STATE.failed_copies += 1
        return None

class MempoolMonitor:
    def __init__(self):
        self.target = CONFIG.TARGET_WALLET
        
    async def stream_filtered(self):
        from websockets import connect
        async with connect(CONFIG.WS_RPC) as ws:
            await ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_subscribe",
                "params": ["alchemy_pendingTransactions", {"fromAddress": [self.target]}]
            }))
            
            response = await ws.recv()
            print(f"Subscription response: {response}")
            
            while True:
                try:
                    msg = json.loads(await ws.recv())
                    if "params" in msg and "result" in msg["params"]:
                        tx_data = msg["params"]["result"]
                        
                        if isinstance(tx_data, dict):
                            tx = tx_data
                        elif isinstance(tx_data, str):
                            tx = w3.eth.get_transaction(tx_data)
                        else:
                            continue
                            
                        await self.handle_target_tx(tx)
                except Exception as e:
                    print(f"Stream error: {e}")
                    await asyncio.sleep(1)
                    
    async def handle_target_tx(self, tx: dict):
        tx_hash = tx.get("hash")
        if isinstance(tx_hash, bytes):
            tx_hash = tx_hash.hex()
        elif isinstance(tx_hash, str) and not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
            
        if STATE.is_seen(tx_hash):
            return
            
        input_data = tx.get("input") or ""
        if isinstance(input_data, bytes):
            input_data = input_data.hex()
            
        if len(input_data) < 10:
            return
            
        func_sig = input_data[:10].lower()
        if func_sig not in MINT_SIGNATURES:
            return
            
        print(f"\n\U0001f3af TARGET MINT PENDING IN MEMPOOL")
        print(f"Hash: {tx_hash}")
        print(f"Contract: {tx.get('to')}")
        print(f"Func: {MINT_SIGNATURES[func_sig][0]}")
        
        quantity = parse_mint_quantity(input_data, func_sig)
        value = int(tx.get("value", 0))
        value_eth = w3.from_wei(value, "ether")
        
        gas_price = tx.get("gasPrice")
        max_fee = tx.get("maxFeePerGas")
        max_priority = tx.get("maxPriorityFeePerGas")
        
        print(f"Quantity: {quantity}, Value: {value_eth} ETH")
        
        result = execute_copy_mint(
            contract_address=tx.get("to"),
            raw_calldata=input_data,
            quantity=quantity,
            value_eth=float(value_eth),
            target_gas_price=gas_price,
            target_max_fee=max_fee,
            target_priority=max_priority
        )
        if result:
            STATE.successful_copies += 1

async def main():
    print(f"\U0001f680 Sniper starting in {'DRY RUN' if CONFIG.DRY_RUN else 'LIVE'} mode...")
    print(f"Target: {CONFIG.TARGET_WALLET}")
    print(f"Wallet: {user_address}")
    print(f"Balance: {w3.from_wei(w3.eth.get_balance(user_address), 'ether'):.4f} ETH")
    
    monitor = MempoolMonitor()
    
    try:
        await monitor.stream_filtered()
    except KeyboardInterrupt:
        print(f"\n\U0001f4ca Stats: {STATE.successful_copies} copies, {STATE.failed_copies} fails")
        print(f"Daily spend: {STATE.daily_spent_eth:.4f}/{CONFIG.DAILY_LIMIT_ETH} ETH")

if __name__ == "__main__":
    asyncio.run(main())
