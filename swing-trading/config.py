"""
config.py  -  Loads your settings and secrets in one place.

- Strategy numbers come from config.yaml (safe to edit by hand).
- Secrets (API keys) come from your .env file.

Every other file imports from here, so there is exactly one source of truth.
"""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# The folder this project lives in (wherever config.py sits).
PROJECT_DIR = Path(__file__).resolve().parent

# Where your secrets live. Matches the path in the spec. If you ever move the
# project, set the SWING_ENV_FILE environment variable to point at your .env.
ENV_FILE = Path(os.environ.get("SWING_ENV_FILE", "/home/trader/alpaca-trading/.env"))

# Data files the system reads/writes, all kept next to the code.
CONFIG_FILE = PROJECT_DIR / "config.yaml"
UNIVERSE_FILE = PROJECT_DIR / "universe.csv"
CANDIDATES_FILE = PROJECT_DIR / "candidates.json"
JOURNAL_FILE = PROJECT_DIR / "journal.csv"
LOG_FILE = PROJECT_DIR / "trades.log"
STATE_FILE = PROJECT_DIR / "state.json"


def load_env():
    """Load secrets from the .env file into memory. Falls back to a local
    .env in the project folder if the main one is missing (useful for testing)."""
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)
    else:
        local = PROJECT_DIR / ".env"
        if local.exists():
            load_dotenv(local)
    # Also load any variables already set in the shell environment (they win).
    load_dotenv(override=False)


def load_config():
    """Read config.yaml and return it as a plain dictionary."""
    with open(CONFIG_FILE, "r") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml did not parse into settings. Check the format.")
    return cfg


# Load once, on import, so everything shares the same values.
load_env()
CONFIG = load_config()


def env(name, default=None, required=False):
    """Read one secret from the environment by name."""
    val = os.environ.get(name, default)
    if required and (val is None or str(val).strip() == ""):
        raise RuntimeError(
            f"Missing required setting '{name}'. Add it to {ENV_FILE}"
        )
    return val


# --- Convenience accessors for the secrets, read lazily so importing this
#     module never crashes just because one key is missing. ---
def alpaca_keys():
    return (
        env("ALPACA_API_KEY", required=True),
        env("ALPACA_SECRET_KEY", required=True),
        env("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
    )


def thetadata_base_url():
    """Where to fetch market data.

    Priority:
      1. MARKETDATA_BASE_URL  (the hosted thetadata.net endpoint, e.g.
         https://marketdata.boxrun.xyz)
      2. THETADATA_BASE_URL   (a local ThetaData Terminal, e.g.
         http://127.0.0.1:25510)
      3. the hosted default
    """
    return (
        env("MARKETDATA_BASE_URL", "")
        or env("THETADATA_BASE_URL", "")
        or "https://marketdata.boxrun.xyz"
    )


def marketdata_api_key():
    """Bearer token for the hosted market-data endpoint. Empty for a local
    ThetaData Terminal (which needs no token)."""
    return env("MARKETDATA_API_KEY", "")


def telegram_creds():
    return (
        env("TELEGRAM_BOT_TOKEN", ""),
        env("TELEGRAM_CHAT_ID", ""),
    )


def is_paper():
    """True while we are pointed at Alpaca's paper (fake-money) endpoint."""
    _, _, base = alpaca_keys()
    return "paper" in base.lower()
