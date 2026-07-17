"""
Automatic Dhan API Token Manager.

Handles token generation and renewal without manual intervention.
Strategy:
  1. Try RenewToken (fast, extends active token by 24h)
  2. If renewal fails (token expired), generate fresh token via PIN + TOTP
  3. Update .env file and optionally reinitialize the live DhanAPI client

Requires:
  - DHAN_CLIENT_ID in .env
  - DHAN_PIN (your 6-digit Dhan login PIN) in .env
  - DHAN_TOTP_SECRET (base32 secret from Dhan TOTP setup) in .env

Setup TOTP:
  1. Go to https://web.dhan.co -> Profile -> DhanHQ Trading APIs
  2. Click "Setup TOTP" and scan/copy the secret key
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
    """Reload .env and return current credentials."""
    load_dotenv(ENV_FILE, override=True)
    return {
        "client_id": os.getenv("DHAN_CLIENT_ID", "").strip(),
        "access_token": os.getenv("DHAN_ACCESS_TOKEN", "").strip(),
        "pin": os.getenv("DHAN_PIN", "").strip(),
        "totp_secret": os.getenv("DHAN_TOTP_SECRET", "").strip(),
    }


def _update_env_token(new_token: str):
    """Write the new access token into the .env file."""
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

        # The response should contain the new access token
        if isinstance(response, dict):
            new_token = response.get("accessToken") or response.get("access_token")
            if new_token:
                return new_token
            # Some versions return data nested
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
            totp = pyotp.TOTP(totp_secret)
            totp_code = totp.now()
            logger.info("Generated TOTP code, requesting new token... (attempt %d/%d)",
                        attempt, max_retries)

            login = DhanLogin(client_id)
            response = login.generate_token(pin=pin, totp=totp_code)
            logger.info("GenerateToken response keys: %s",
                         list(response.keys()) if isinstance(response, dict) else type(response))

            if isinstance(response, dict):
                # Check for rate limit
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


def refresh_token(dhan_api=None, max_retries: int = 3) -> bool:
    """
    Main entry point: refresh the Dhan access token.

    Strategy:
      1. Try RenewToken (extends active token, fast)
      2. If that fails, generate fresh token via PIN + TOTP

    Args:
        dhan_api: Optional live DhanAPI instance to reinitialize with new credentials.
        max_retries: Maximum attempts to try token generation on rate limits.

    Returns:
        True if token was refreshed successfully, False otherwise.
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
        new_token = generate_fresh_token(
            creds["client_id"], creds["pin"], creds["totp_secret"], max_retries=max_retries
        )
        if new_token:
            logger.info("Fresh token generated successfully")

    if not new_token:
        logger.error("All token refresh methods failed. Manual intervention required.")
        return False

    # Step 3: Persist to .env
    _update_env_token(new_token)

    # Step 4: Reinitialize live API client if provided
    if dhan_api is not None:
        dhan_api.reinitialize(creds["client_id"], new_token)
        logger.info("Live DhanAPI client reinitialized with new token")

    return True


def is_token_refresh_configured() -> bool:
    """Check if auto token refresh is properly configured."""
    creds = _load_env()
    return bool(creds["client_id"] and creds["pin"] and creds["totp_secret"])


# ── Standalone CLI usage ───────────────────────────────────────────────
# Can be run directly: python token_manager.py
# Used by cron jobs and manual refresh.

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform.log")
            ),
        ],
    )

    print("=" * 50)
    print("  Dhan Token Auto-Refresh")
    print("=" * 50)

    success = refresh_token()

    if success:
        print("\nToken refreshed successfully.")
        # Verify by loading the new token
        load_dotenv(ENV_FILE, override=True)
        token = os.getenv("DHAN_ACCESS_TOKEN", "")
        print(f"New token: {token[:20]}...{token[-10:]}")
    else:
        print("\nToken refresh FAILED. Check logs for details.")
        sys.exit(1)
