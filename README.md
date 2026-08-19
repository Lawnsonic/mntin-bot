# nft-mint-bot

Simple Web3 + Telegram NFT mint bot scaffold.

Files
- .env.example — example env variables
- mint_engine.py — pure Web3 execution (connect, estimate, sign, send)
- bot.py — Telegram handlers, inline buttons, mode routing

Setup
1. Create a virtualenv and install deps

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env` and fill values for `RPC_URL`, `PRIVATE_KEY`, and `TELEGRAM_BOT_TOKEN`.

Usage
- Test the engine directly (recommended) before starting the bot:

```bash
python -c "from mint_engine import execute_mint; print('OK' if execute_mint.__doc__ else 'NO')"
```

- Run the bot locally:

```bash
python bot.py
```

Notes
- Keep the blockchain logic in `mint_engine.py` so it can be tested independently of Telegram.
- Use testnets (Goerli, Sepolia, etc.) and small funds while testing.
- This scaffold uses a minimal `mint(quantity)` ABI — adapt the ABI to the target contract if different.
