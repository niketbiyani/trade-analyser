"""
Automatic Dhan API Token Manager.

Handles token generation and renewal without manual intervention.
Strategy:
  1. Try RenewToken (fast, extends active token by 24h)
  2. If renewal fails (token expired), generate fresh token via PIN + TOTP
  3. Update .env file so the app picks up the new token on next _dhan_client() call

Requires in .env:
  DHAN_CLIENT_ID
  DHAN_PIN          (6-digit Dhan login PIN)
  DHAN_TOTP_SECRET  (base32 secret from Dhan TOTP setup)

Setup TOTP:
  1. Go to https://web.dhan.co -> Profile -> DhanHQ Trading APIs
  2. Click "Setup TOTP" and copy the secret key
  3. Add DHAN_TOTP_SECRET=YOUR_SECRET to .env
"""

import logging
import os
import re
import sys
import time

import pyotp
from dhanhq import DhanLogin
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_env():
    load_dotenv(ENV_FILE, override=True)
    return {
        "client_id":    os.getenv("DHAN_CLIENT_ID", ""),
        "access_token": os.getenv("DHAN_ACCESS_TOKEN", ""),
        "pin":          os.getenv("DHAN_PIN", ""),
        "totp_secret":  os.getenv("DHAN_TOTP_SECRET", ""),
    }


def _update_env_token(new_token: str):
    with open(ENV_FILE, "r") as f:
        content = f.read()
    content = re.sub(
        r"^DHAN_ACCESS_TOKEN=.*$",
        f"DHAN_ACCESS_TOKEN={new_token}",
        content,
        flags=re.MULTILINE,
    )
    with open(ENV_FILE, "w") as f:
        f.write(content)
    logger.info("Updated access token in .env")


def try_renew_token(client_id: str, current_token: str) -> str | None:
    """Renew an active token (extends it by 24 h). Returns new token or None."""
    try:
        login = DhanLogin(client_id)
        response = login.renew_token(current_token)
        logger.info("RenewToken response: %s", response)
        if isinstance(response, dict):
            tok = response.get("accessToken") or response.get("access_token")
            if tok:
                return tok
            data = response.get("data", {})
            if isinstance(data, dict):
                tok = data.get("accessToken") or data.get("access_token")
                if tok:
                    return tok
        logger.warning("RenewToken did not return a valid token: %s", response)
        return None
    except Exception as e:
        logger.warning("RenewToken failed (token likely expired): %s", e)
        return None


def generate_fresh_token(client_id: str, pin: str, totp_secret: str, max_retries: int = 3) -> str | None:
    """Generate a new token via PIN + TOTP. Retries on Dhan 2-minute rate limit."""
    if not pin or not totp_secret:
        logger.error("DHAN_PIN and DHAN_TOTP_SECRET must be set for auto token generation")
        return None

    for attempt in range(1, max_retries + 1):
        try:
            totp_code = pyotp.TOTP(totp_secret).now()
            logger.info("Requesting new token (attempt %d/%d)...", attempt, max_retries)
            login = DhanLogin(client_id)
            response = login.generate_token(pin=pin, totp=totp_code)

            if isinstance(response, dict):
                if response.get("status") == "error":
                    msg = response.get("message", "")
                    if "once every" in msg.lower() or "2 minute" in msg.lower():
                        if attempt < max_retries:
                            logger.warning("Rate limited by Dhan. Waiting 130 s...")
                            time.sleep(130)
                            continue
                        logger.error("Rate limited after %d attempts", max_retries)
                        return None

                tok = response.get("accessToken") or response.get("access_token")
                if tok:
                    return tok
                data = response.get("data", {})
                if isinstance(data, dict):
                    tok = data.get("accessToken") or data.get("access_token")
                    if tok:
                        return tok

            logger.error("GenerateToken did not return a valid token: %s", response)
            return None
        except Exception as e:
            logger.error("GenerateToken failed: %s", e)
            return None

    return None


def refresh_token() -> bool:
    """
    Refresh the Dhan access token and persist to .env.
    1. Try RenewToken  (fast, works while token is still active)
    2. Fall back to PIN + TOTP fresh generation
    """
    creds = _load_env()
    if not creds["client_id"]:
        logger.error("DHAN_CLIENT_ID not set")
        return False

    new_token = None

    if creds["access_token"]:
        logger.info("Attempting token renewal...")
        new_token = try_renew_token(creds["client_id"], creds["access_token"])
        if new_token:
            logger.info("Token renewed via RenewToken")

    if not new_token:
        logger.info("Attempting fresh token via PIN + TOTP...")
        new_token = generate_fresh_token(creds["client_id"], creds["pin"], creds["totp_secret"])
        if new_token:
            logger.info("Fresh token generated")

    if not new_token:
        logger.error("All token refresh methods failed")
        return False

    _update_env_token(new_token)
    return True


def is_token_refresh_configured() -> bool:
    creds = _load_env()
    return bool(creds["client_id"] and creds["pin"] and creds["totp_secret"])


# ── Standalone CLI: python token_manager.py ────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyser.log")
            ),
        ],
    )
    success = refresh_token()
    if success:
        load_dotenv(ENV_FILE, override=True)
        tok = os.getenv("DHAN_ACCESS_TOKEN", "")
        print(f"Token refreshed: {tok[:20]}...{tok[-10:]}")
    else:
        print("Token refresh FAILED.")
        sys.exit(1)
