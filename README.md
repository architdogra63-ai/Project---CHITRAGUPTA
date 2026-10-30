# Chitragupta Backend

FastAPI + SQLite service for sealing, verifying and auditing case evidence, with an Obsidian-style vault export.

## Setup

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

`backend/.env` already holds the local secrets. Fill the `LLM_*` lines when the Kaggle-hosted model is ready. The app runs without them.

## Run

```bash
python scripts/seed_users.py                  # demo users, password: ChangeMe123!
uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs

## Test

```bash
python -m pytest -q        # 85 tests
```

## Layout

- `app/`: routers, custody (seal/verify), ledger, scanner, crypto, auth, vault writer and consumer
- `scripts/`: seed users, rebuild vault, tamper demo
- `tests/`: unit and API tests
- `vault_template/graph.json`: Obsidian graph settings

Change the demo passwords before any real use.
