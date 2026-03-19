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

import httpx
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from config import settings

IAAA_BASE = "https://iaaa.pku.edu.cn"
BB_CAMPUS_LOGIN_URL = "http://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
BB_BASE = "https://course.pku.edu.cn"


def _encrypt_password(password: str, public_key_pem: str) -> str:
    """RSA-encrypt password using PKCS1v15 (matching JSEncrypt behaviour)."""
    key = load_pem_public_key(public_key_pem.encode())
    assert isinstance(key, RSAPublicKey)
    encrypted = key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def get_session() -> httpx.Client:
    """Authenticate and return an httpx.Client with active Blackboard session."""
    if not settings.pku_username or not settings.pku_password:
        raise RuntimeError(
            "PKU_USERNAME and PKU_PASSWORD must be set in .env to authenticate."
        )

    client = httpx.Client(base_url=BB_BASE, follow_redirects=True, timeout=30, verify=False)

    # Step 1: fetch RSA public key
    key_resp = client.get(f"{IAAA_BASE}/iaaa/getPublicKey.do")
    key_resp.raise_for_status()
    key_data = key_resp.json()
    if not key_data.get("success"):
        raise RuntimeError(f"Failed to fetch IAAA public key: {key_data}")
    public_key_pem: str = key_data["key"]

    # Step 2: encrypt password
    encrypted_pwd = _encrypt_password(settings.pku_password, public_key_pem)

    # Step 3: POST credentials to oauthlogin.do
    login_resp = client.post(
        f"{IAAA_BASE}/iaaa/oauthlogin.do",
        data={
            "appid": "blackboard",
            "userName": settings.pku_username,
            "password": encrypted_pwd,
            "randCode": "",
            "smsCode": "",
            "otpCode": "",
            "redirUrl": BB_CAMPUS_LOGIN_URL,
        },
    )
    login_resp.raise_for_status()
    payload = login_resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"IAAA login failed: {payload.get('errors') or payload}")

    token: str = payload["token"]

    # Step 4: exchange token for Blackboard session cookies
    bb_resp = client.get(BB_CAMPUS_LOGIN_URL, params={"token": token})
    bb_resp.raise_for_status()

    return client
