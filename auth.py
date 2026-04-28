"""
Auth Helper
============
Reads credentials from Lambda environment variables.
Handles MCSP token exchange for SaaS/DL instances.
For local testing, reads from orchestrate CLI cache.
"""

import os
import logging
import time
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Token exchange endpoints
AUTH_ENDPOINT_IBM_CLOUD = "https://iam.cloud.ibm.com/identity/token"
AUTH_ENDPOINT_SAAS = "https://iam.platform.saas.ibm.com/siusermgr/api/1.0/apikeys/token"

# Cache
_cached_token = None
_token_expires_at = 0


def _is_ibm_cloud_url(url: str) -> bool:
    """Check if URL is IBM Cloud (not SaaS/DL)."""
    return "cloud.ibm.com" in url and "dl.watson" not in url


def _exchange_token(api_key: str, instance_url: str) -> str:
    """Exchange API key for a bearer token."""
    global _cached_token, _token_expires_at

    if _cached_token and time.time() < _token_expires_at:
        return _cached_token

    if _is_ibm_cloud_url(instance_url):
        # IBM Cloud IAM
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
            "apikey": api_key,
        }
        resp = requests.post(AUTH_ENDPOINT_IBM_CLOUD, headers=headers, data=data, timeout=30)
    else:
        # SaaS / DL / MCSP
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        resp = requests.post(AUTH_ENDPOINT_SAAS, headers=headers, json={"apikey": api_key}, timeout=30)

    resp.raise_for_status()
    result = resp.json()

    token = result.get("access_token") or result.get("token")
    if not token:
        raise RuntimeError(f"No token in exchange response: {list(result.keys())}")

    expires_in = result.get("expires_in", 3600)
    _cached_token = token
    _token_expires_at = time.time() + int(0.8 * expires_in)

    logger.info(f"Token exchanged, expires in {expires_in}s")
    return token


def get_wxo_credentials() -> Tuple[Optional[str], Optional[str]]:
    """Get WxO bearer token and instance URL.

    In Lambda: reads API key from env, exchanges for bearer token.
    Locally: reads from orchestrate CLI cache.
    Returns (bearer_token, instance_url).
    """
    # 1. Environment variables (Lambda)
    api_key = os.environ.get("WXO_API_KEY")
    url = os.environ.get("WXO_INSTANCE_URL") or os.environ.get("WO_INSTANCE")

    if api_key and url:
        # Exchange API key for bearer token
        try:
            token = _exchange_token(api_key, url)
            return token, url
        except Exception as e:
            logger.error(f"Token exchange failed: {e}")
            # Fall back to using API key directly (might work for some endpoints)
            return api_key, url

    # 2. Check for pre-exchanged token (WO_TOKEN)
    token = os.environ.get("WO_TOKEN")
    if token and url:
        return token, url

    # 3. Orchestrate CLI cache (local dev)
    try:
        import yaml
        from pathlib import Path

        config_path = Path.home() / ".config" / "orchestrate" / "config.yaml"
        if not config_path.exists():
            return None, None

        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        active_env = config.get("context", {}).get("active_environment")
        if not active_env:
            return None, None

        env_config = config.get("environments", {}).get(active_env, {})
        url = env_config.get("wxo_url")

        creds_path = Path.home() / ".cache" / "orchestrate" / "credentials.yaml"
        if not creds_path.exists():
            return None, url

        with open(creds_path, encoding="utf-8") as f:
            creds = yaml.safe_load(f) or {}

        token = creds.get("auth", {}).get(active_env, {}).get("wxo_mcsp_token")
        return token, url

    except Exception as e:
        logger.warning(f"Failed to read orchestrate cache: {e}")
        return None, None


def get_openai_api_key() -> Optional[str]:
    """Get OpenAI API key from env vars."""
    return os.environ.get("OPENAI_API_KEY")


def get_wxo_env_name() -> str:
    """Get the orchestrate environment name."""
    return os.environ.get("WXO_ENV_NAME", "aws-commercial")
