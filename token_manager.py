"""
Automatic Dhan API Token Manager for Trade Analyser.

Primary strategy: copy DHAN_ACCESS_TOKEN from the sibling Risk-Management app's
.env at startup and on demand — risk-management already handles full token refresh
(PIN + TOTP), so no need to duplicate that logic here.

Fallback (if Risk-Management .env is unavailable): try RenewToken, then PIN + TOTP
from local .env.

Requires in local .env:
  DHAN_CLIENT_ID
  DHAN_ACCESS_TOKEN  (updated automatically by sync or refresh)

For fallback to work, also add:
  DHAN_PIN          (6-digit Dhan login PIN)
  DHAN_TOTP_SECRET  (base32 secret from Dhan TOTP setup)
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
RM_ENV   = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "..", "Risk-Management", ".env"))


def _load_env():
    load_dotenv(ENV_FILE, override=True)
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
    logger.info("Updated DHAN_ACCESS_TOKEN in local .env")


def sync_from_risk_management() -> bool:
    """
    Copy DHAN_ACCESS_TOKEN (and DHAN_CLIENT_ID) from the sibling Risk-Management
    .env into the local .env. Returns True if the token was copied successfully.
    """
    if not os.path.exists(RM_ENV):
        logger.warning("Risk-Management .env not found at %s — skipping sync", RM_ENV)
        return False

    # Read RM .env without polluting os.environ permanently
    rm_vars = {}
    with open(RM_ENV) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                rm_vars[k.strip()] = v.strip()

    token     = rm_vars.get("DHAN_ACCESS_TOKEN", "")
    client_id = rm_vars.get("DHAN_CLIENT_ID", "")

    if not token:
        logger.error("No DHAN_ACCESS_TOKEN found in Risk-Management .env")
        return False

    _update_env_token(token)

    # Also sync DHAN_CLIENT_ID if local .env has it — update in case it drifted
    if client_id:
        with open(ENV_FILE, "r") as f:
            content = f.read()
        if "DHAN_CLIENT_ID=" in content:
            content = re.sub(r"^DHAN_CLIENT_ID=.*$", f"DHAN_CLIENT_ID={client_id}",
                             content, flags=re.MULTILINE)
            with open(ENV_FILE, "w") as f:
                f.write(content)

    logger.info("Synced token from Risk-Management .env (%.20s...)", token)
    return True


def _try_renew(client_id: str, current_token: str) -> str | None:
    try:
        login = DhanLogin(client_id)
        resp  = login.renew_token(current_token)
        logger.info("RenewToken response: %s", resp)
        if isinstance(resp, dict):
            tok = resp.get("accessToken") or resp.get("access_token")
            if tok:
                return tok
            data = resp.get("data", {})
            if isinstance(data, dict):
                tok = data.get("accessToken") or data.get("access_token")
                if tok:
                    return tok
        logger.warning("RenewToken did not return a token: %s", resp)
        return None
    except Exception as e:
        logger.warning("RenewToken failed: %s", e)
        return None


def _try_generate(client_id: str, pin: str, totp_secret: str, max_retries: int = 3) -> str | None:
    if not pin or not totp_secret:
        logger.error("DHAN_PIN and DHAN_TOTP_SECRET must be set for auto token generation")
        return None
    for attempt in range(1, max_retries + 1):
        try:
            totp_code = pyotp.TOTP(totp_secret).now()
            logger.info("Generating token via PIN+TOTP (attempt %d/%d)...", attempt, max_retries)
            login = DhanLogin(client_id)
            resp  = login.generate_token(pin=pin, totp=totp_code)
            logger.info("GenerateToken response keys: %s",
                        list(resp.keys()) if isinstance(resp, dict) else type(resp))
            if isinstance(resp, dict):
                if resp.get("status") == "error":
                    msg = resp.get("message", "")
                    if "once every" in msg.lower() or "2 minute" in msg.lower():
                        if attempt < max_retries:
                            logger.warning("Rate limited — waiting 130s...")
                            time.sleep(130)
                            continue
                        logger.error("Rate limited after %d attempts", max_retries)
                        return None
                tok = resp.get("accessToken") or resp.get("access_token")
                if tok:
                    return tok
                data = resp.get("data", {})
                if isinstance(data, dict):
                    tok = data.get("accessToken") or data.get("access_token")
                    if tok:
                        return tok
            logger.error("GenerateToken did not return a token: %s", resp)
            return None
        except Exception as e:
            logger.error("GenerateToken failed: %s", e)
            return None
    return None


def refresh_token() -> bool:
    """
    Refresh the Dhan access token.

    1. Sync from Risk-Management .env (primary — RM already does the hard refresh)
    2. Try RenewToken via Dhan API  (fallback if RM not available)
    3. Generate fresh token via PIN + TOTP  (last resort)
    """
    # Primary: copy from risk-management
    if sync_from_risk_management():
        return True

    # Fallback: own renew / generate
    logger.info("Risk-Management sync unavailable — attempting own token refresh...")
    creds = _load_env()
    if not creds["client_id"]:
        logger.error("DHAN_CLIENT_ID not set")
        return False

    new_token = None
    if creds["access_token"]:
        new_token = _try_renew(creds["client_id"], creds["access_token"])
    if not new_token:
        new_token = _try_generate(creds["client_id"], creds["pin"], creds["totp_secret"])

    if not new_token:
        logger.error("All token refresh methods failed")
        return False

    _update_env_token(new_token)
    return True


def is_token_refresh_configured() -> bool:
    return os.path.exists(RM_ENV) or bool(_load_env()["pin"] and _load_env()["totp_secret"])


# ── Standalone CLI: python token_manager.py ───────────────────────────────────
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
        print(f"Token synced: {tok[:20]}...{tok[-10:]}")
    else:
        print("Token sync FAILED. Check analyser.log for details.")
        sys.exit(1)
