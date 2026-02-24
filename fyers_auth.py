"""
Fyers DOM Analyzer - Authentication Module.
Handles Fyers OAuth 2.0 token exchange and credential management.
"""

import os
import json
import hashlib
from typing import Tuple, Optional

import requests
from fyers_database import upsert_fyers_auth


def authenticate_fyers_broker(request_token: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Authenticate with Fyers API using the OAuth request token.

    Returns:
        Tuple of (access_token, error_message).
        On success: (token_string, None)
        On failure: (None, error_string)
    """
    broker_api_key = os.getenv('FYERS_API_KEY')
    broker_api_secret = os.getenv('FYERS_API_SECRET')

    if not broker_api_key or not broker_api_secret:
        return None, "Missing FYERS_API_KEY or FYERS_API_SECRET in environment"

    if not request_token:
        return None, "No request token provided"

    url = 'https://api-t1.fyers.in/api/v3/validate-authcode'

    try:
        checksum_input = f"{broker_api_key}:{broker_api_secret}"
        app_id_hash = hashlib.sha256(checksum_input.encode('utf-8')).hexdigest()

        payload = {
            'grant_type': 'authorization_code',
            'appIdHash': app_id_hash,
            'code': request_token
        }

        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        print(f"[FYERS AUTH] Authenticating with Fyers API...")

        response = requests.post(url, headers=headers, json=payload, timeout=30.0)
        response.raise_for_status()
        auth_data = response.json()

        if auth_data.get('s') == 'ok':
            access_token = auth_data.get('access_token')
            if not access_token:
                return None, "Auth succeeded but no access token returned"

            print("[FYERS AUTH] Successfully authenticated with Fyers API")
            return access_token, None
        else:
            error_msg = auth_data.get('message', 'Authentication failed')
            print(f"[FYERS AUTH] API error: {error_msg}")
            return None, f"API error: {error_msg}"

    except Exception as e:
        error_msg = f"Authentication failed: {e}"
        print(f"[FYERS AUTH] {error_msg}")
        return None, error_msg


def handle_fyers_auth_success(auth_token, username, broker='fyers'):
    """Handle successful authentication by storing token in database."""
    try:
        api_key = os.getenv('FYERS_API_KEY')
        api_secret = os.getenv('FYERS_API_SECRET')

        inserted_id = upsert_fyers_auth(
            name=username,
            auth_token=auth_token,
            broker=broker,
            api_key=api_key,
            api_secret=api_secret
        )
        if inserted_id is not None:
            print(f"[FYERS AUTH] Auth token stored for user: {username}")
            return True
        else:
            print(f"[FYERS AUTH] Failed to store auth token for: {username}")
            return False
    except Exception as e:
        print(f"[FYERS AUTH] Error storing auth: {e}")
        return False


def mask_api_credential(credential):
    """Mask API credentials for display."""
    if not credential or len(credential) < 8:
        return "****"
    return credential[:4] + "*" * (len(credential) - 8) + credential[-4:]
