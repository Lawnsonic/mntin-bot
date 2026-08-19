# Multi-Chain NFT Mint & Sniper Bot

Multi-user Telegram bot for NFT minting and mempool sniping across multiple EVM chains. Default network: **Robinhood Chain** (Chain ID 4663).

## Architecture

| File | Purpose |
|------|---------|
| `bot.py` | Telegram handlers, per-user state, command routing |
| `mint_engine.py` | Stateless Web3 execution (mint, withdraw, copy-trade) |
| `db.py` | Encrypted wallet storage (SQLite + Fernet) |
| `sniper.py` | Standalone sniper (legacy, single-user) |

## Setup

1. **Create a virtualenv and install deps**

```bash
python -m venv .venv
.\.venv\Scripts\activate        # Windows
# source .venv/bin/activate     # Linux/Mac
pip install -r requirements.txt
```

2. **Generate an encryption key**

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

3. **Configure environment**

Copy `env.example` to `.env` and fill in:
- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
- `ENCRYPTION_KEY` — the Fernet key from step 2
- RPC endpoints as needed (Robinhood is pre-filled)

4. **Run the bot**

```bash
python bot.py
```

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and command overview |
| `/wallet` | Create your wallet (private key shown once, auto-deletes after 5 min) |
| `/deposit` | Show your deposit address |
| `/balance` | Check ETH balance on active network |
| `/withdraw <amt> <addr>` | Send ETH to external wallet |
| `/network <name>` | Switch chains: robinhood, arb, base, eth |
| `/mode` | Toggle AUTO/MANUAL minting |
| `/snipe` | Start/stop mempool copy-trade sniper |
| `/target <addr>` | Set wallet to copy-trade from |
| `/dryrun` | Toggle sniper dry-run mode |
| `/status` | Full system status |

**To mint:** paste any EVM contract address in chat.

## Security Notes

- Private keys are encrypted at rest with Fernet symmetric encryption
- Keys are shown only once at wallet creation (message auto-deletes)
- Each user gets isolated concurrency locks (no cross-user nonce collisions)
- The `ENCRYPTION_KEY` in `.env` is the master secret — protect it accordingly
- This is a custodial architecture — the bot operator can decrypt all keys

## Supported Networks

| Name | Chain ID | RPC |
|------|----------|-----|
| Robinhood (default) | 4663 | `rpc.mainnet.chain.robinhood.com` |
| Arbitrum One | 42161 | `arb1.arbitrum.io/rpc` |
| Base | 8453 | `mainnet.base.org` |
| Ethereum | 1 | `eth.llamarpc.com` |
