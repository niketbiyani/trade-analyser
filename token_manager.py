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

If DHAN_PIN / DHAN_TOTP_SECRET are not set here, falls back to the sibling
Risk-Management/.env so both apps can share credentials without duplication.

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
# Sibling risk-management .env — used as fallback for PIN/TOTP if not set locally
_RM_ENV  = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "Risk-Management", ".env"))


def _load_env():
    """Reload .env and return current credentials. Falls back to Risk-Management/.env for PIN/TOTP."""
    load_dotenv(ENV_FILE, override=True)
    # If PIN or TOTP secret are missing, try sibling Risk-Management app's .env
    if (not os.getenv("DHAN_PIN") or not os.getenv("DHAN_TOTP_SECRET")) and os.path.exists(_RM_ENV):
        logger.info("PIN/TOTP not in local .env — loading from %s", _RM_ENV)
        load_dotenv(_RM_ENV, override=False)  # override=False: don't overwrite already-set vars
    return {
        "client_id":    os.getenv("DHAN_CLIENT_ID", ""),
        "access_token": os.getenv("DHAN_ACCESS_TOKEN", ""),
        "pin":          os.getenv("DHAN_PIN", ""),
        "totp_secret":  os.getenv("DHAN_TOTP_SECRET", ""),
    }


def _update_env_token(new_token: str):
    """Write the new access token into the local .env file."""
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
    """
    Try to renew an active token. Returns new token or None if renewal fails.
    This only works if the current token has NOT expired yet.
    """
    try:
        login = DhanLogin(client_id)
        response = login.renew_token(current_token)
        logger.info("RenewToken response: %s", response)
        if isinstance(response, dict):
            new_token = response.get("accessToken") or response.get("access_token")
            if new_token:
                return new_token
            data = response.get("data", {})
            if isinstance(data, dict):
                new_token = data.get("accessToken") or data.get("access_token")
                if new_token:
                    return new_token
        logger.warning("RenewToken did not return a valid token: %s", response)
        return None
    except Exception as e:
        logger.warning("RenewToken failed (token likely expired): %s", e)
        return None


def generate_fresh_token(client_id: str, pin: str, totp_secret: str, max_retries: int = 3) -> str | None:
    """
    Generate a brand new token using PIN + TOTP. Fully headless, no browser needed.
    Retries on Dhan's rate limit ("Token can be generated once every 2 minutes").
    """
    if not pin or not totp_secret:
        logger.error("DHAN_PIN and DHAN_TOTP_SECRET must be set in .env for auto token generation")
        return None

    for attempt in range(1, max_retries + 1):
        try:
            totp_code = pyotp.TOTP(totp_secret).now()
            logger.info("Generated TOTP code, requesting new token... (attempt %d/%d)", attempt, max_retries)
            login = DhanLogin(client_id)
            response = login.generate_token(pin=pin, totp=totp_code)
            logger.info("GenerateToken response keys: %s",
                        list(response.keys()) if isinstance(response, dict) else type(response))

            if isinstance(response, dict):
                if response.get("status") == "error":
                    msg = response.get("message", "")
                    if "once every" in msg.lower() or "2 minute" in msg.lower():
                        if attempt < max_retries:
                            logger.warning("Rate limited by Dhan: %s. Waiting 130s before retry...", msg)
                            time.sleep(130)
                            continue
                        else:
                            logger.error("Rate limited by Dhan after %d attempts: %s", max_retries, msg)
                            return None

                new_token = response.get("accessToken") or response.get("access_token")
                if new_token:
                    return new_token
                data = response.get("data", {})
                if isinstance(data, dict):
                    new_token = data.get("accessToken") or data.get("access_token")
                    if new_token:
                        return new_token

            logger.error("GenerateToken did not return a valid token: %s", response)
            return None
        except Exception as e:
            logger.error("GenerateToken failed: %s", e)
            return None

    return None


def refresh_token() -> bool:
    """
    Refresh the Dhan access token and persist to local .env.
    1. Try RenewToken  (fast, works while token is still active)
    2. Fall back to PIN + TOTP fresh generation
    """
    creds = _load_env()
    if not creds["client_id"]:
        logger.error("DHAN_CLIENT_ID not set in .env")
        return False

    new_token = None

    # Step 1: Try renewal (works if current token is still active)
    if creds["access_token"]:
        logger.info("Attempting token renewal...")
        new_token = try_renew_token(creds["client_id"], creds["access_token"])
        if new_token:
            logger.info("Token renewed successfully via RenewToken")

    # Step 2: Fall back to fresh generation via PIN + TOTP
    if not new_token:
        logger.info("Attempting fresh token generation via PIN + TOTP...")
        new_token = generate_fresh_token(creds["client_id"], creds["pin"], creds["totp_secret"])
        if new_token:
            logger.info("Fresh token generated successfully")

    if not new_token:
        logger.error("All token refresh methods failed. Manual intervention required.")
        return False

    # Step 3: Persist to local .env
    _update_env_token(new_token)
    return True


def is_token_refresh_configured() -> bool:
    """Check if auto token refresh is properly configured."""
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
        print("Token refresh FAILED. Check analyser.log for details.")
        sys.exit(1)
