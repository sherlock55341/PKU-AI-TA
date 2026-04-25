"""
PKU IAAA SSO authentication.

Actual flow (reverse-engineered from OAuthLogin.js on iaaa.pku.edu.cn):
  1. GET /iaaa/getPublicKey.do → RSA public key (PEM)
  2. Encrypt password with RSA PKCS1v15 using that key
  3. POST /iaaa/oauthlogin.do with appid, userName, encrypted password, redirectUrl
     → returns JSON { success: true, token: "..." }
  4. GET campusLogin?token=... on course.pku.edu.cn → Blackboard session cookies

PKU CA is not in the default macOS trust store, so verify=False is used.
"""
from __future__ import annotations

import base64
import ssl
import time

import httpx
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from config import settings

IAAA_BASE = "https://iaaa.pku.edu.cn"
BB_CAMPUS_LOGIN_REDIRECT_URL = "http://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
BB_CAMPUS_LOGIN_URL = "https://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
BB_CAMPUS_LOGIN_FALLBACK_URL = BB_CAMPUS_LOGIN_REDIRECT_URL
BB_BASE = "https://course.pku.edu.cn"
_RETRYABLE_STATUS_CODES = {502, 503, 504}


def _encrypt_password(password: str, public_key_pem: str) -> str:
    """RSA-encrypt password using PKCS1v15 (matching JSEncrypt behaviour)."""
    key = load_pem_public_key(public_key_pem.encode())
    assert isinstance(key, RSAPublicKey)
    encrypted = key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def get_session(username: str | None = None, password: str | None = None) -> httpx.Client:
    """Authenticate and return an httpx.Client with active Blackboard session."""
    username = username if username is not None else settings.pku_username
    password = password if password is not None else settings.pku_password
    if not username or not password:
        raise RuntimeError(
            "PKU_USERNAME and PKU_PASSWORD must be set in .env to authenticate."
        )

    try:
        return _get_session_with_client(username, password, trust_env=True)
    except RuntimeError as exc:
        if "SSL EOF" not in str(exc):
            raise
        return _get_session_with_client(username, password, trust_env=False)


def _get_session_with_client(username: str, password: str, *, trust_env: bool) -> httpx.Client:
    client = httpx.Client(base_url=BB_BASE, follow_redirects=True, timeout=30, verify=False, trust_env=trust_env)
    network_label = "terminal proxy/VPN environment" if trust_env else "direct connection fallback"
    # Step 1: fetch RSA public key
    key_resp = _request_with_retries(
        "GET",
        f"{IAAA_BASE}/iaaa/getPublicKey.do",
        client=client,
        stage=f"IAAA public key via {network_label}",
    )
    key_resp.raise_for_status()
    key_data = key_resp.json()
    if not key_data.get("success"):
        raise RuntimeError(f"Failed to fetch IAAA public key: {key_data}")
    public_key_pem: str = key_data["key"]

    # Step 2: encrypt password
    encrypted_pwd = _encrypt_password(password, public_key_pem)

    # Step 3: POST credentials to oauthlogin.do
    login_resp = _request_with_retries(
        "POST",
        f"{IAAA_BASE}/iaaa/oauthlogin.do",
        client=client,
        stage=f"IAAA oauthlogin via {network_label}",
        data={
            "appid": "blackboard",
            "userName": username,
            "password": encrypted_pwd,
            "randCode": "",
            "smsCode": "",
            "otpCode": "",
            "redirUrl": BB_CAMPUS_LOGIN_REDIRECT_URL,
        },
    )
    login_resp.raise_for_status()
    payload = login_resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"IAAA login failed: {payload.get('errors') or payload}")

    token: str = payload["token"]

    # Step 4: exchange token for Blackboard session cookies
    _exchange_token_for_blackboard_session(client, token, network_label=network_label)

    return client


def _request_with_retries(
    method: str,
    url: str,
    *,
    client: httpx.Client,
    stage: str,
    attempts: int = 3,
    **kwargs,
) -> httpx.Response:
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            return client.request(method, url, **kwargs)
        except (httpx.HTTPError, ssl.SSLError, OSError) as exc:
            errors.append(f"{stage}: {_safe_url_label(url)} failed: {_describe_network_error(exc)}")
            if attempt < attempts:
                time.sleep(attempt)
    detail = "; ".join(errors[-attempts:])
    raise RuntimeError(f"PKU login network error ({stage}): {detail}")


def _exchange_token_for_blackboard_session(client: httpx.Client, token: str, *, network_label: str = "terminal proxy/VPN environment") -> None:
    """Exchange the IAAA token for Blackboard cookies with retry and HTTP fallback."""
    errors: list[str] = []
    for url in (BB_CAMPUS_LOGIN_URL, BB_CAMPUS_LOGIN_FALLBACK_URL):
        for attempt in range(1, 4):
            try:
                resp = client.get(url, params={"token": token})
                resp.raise_for_status()
                return
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                errors.append(f"{network_label}: {_safe_url_label(url)} returned HTTP {status}")
                if status not in _RETRYABLE_STATUS_CODES:
                    break
            except httpx.HTTPError as exc:
                errors.append(f"{network_label}: {_safe_url_label(url)} failed: {_describe_network_error(exc)}")
            except (ssl.SSLError, OSError) as exc:
                errors.append(f"{network_label}: {_safe_url_label(url)} failed: {_describe_network_error(exc)}")

            if attempt < 3:
                time.sleep(attempt)

    detail = "; ".join(errors[-4:]) if errors else "unknown error"
    raise RuntimeError(f"Could not complete PKU Blackboard SSO login: {detail}")


def _safe_url_label(url: str) -> str:
    parsed = httpx.URL(url)
    return f"{parsed.scheme}://{parsed.host}{parsed.path}"


def _describe_network_error(exc: Exception) -> str:
    text = str(exc)
    if "UNEXPECTED_EOF_WHILE_READING" in text:
        return "SSL EOF"
    return exc.__class__.__name__
