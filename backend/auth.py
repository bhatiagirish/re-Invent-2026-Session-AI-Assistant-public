"""OAuth (Amazon Cognito) authorization-code + PKCE for AWS Events.

Implements the AuthProvider abstraction from design.md. Supports BOTH:
  * public clients  -> PKCE code_verifier only, no secret
  * confidential clients -> client_secret_basic (secret from Secrets Manager)

The concrete mode is decided at runtime from whether OAUTH_CLIENT_SECRET_ARN
is configured (resolved in Phase 0 task 0.2). All token exchange happens
SERVER-SIDE; no token is ever returned to the browser or logged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from functools import lru_cache
from typing import Optional

import config
from errors import AuthFailedError, TokenRefreshFailedError
from models import TokenBundle


# ── PKCE / state / nonce helpers ─────────────────────────────────────────────
def generate_code_verifier() -> str:
    """RFC 7636 code_verifier: 43-128 chars of unreserved characters."""
    # token_urlsafe(64) -> ~86 chars, within the 43-128 bound.
    return secrets.token_urlsafe(64)


def code_challenge_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state() -> str:
    """>=32 bytes of entropy, URL-safe."""
    return secrets.token_urlsafe(32)


def generate_nonce() -> str:
    return secrets.token_urlsafe(32)


# ── Secret resolution (confidential clients only) ───────────────────────────
@lru_cache(maxsize=1)
def _client_secret() -> Optional[str]:
    arn = config.OAUTH_CLIENT_SECRET_ARN
    if not arn:
        return None
    import boto3  # local import: not needed for public-client path / unit tests

    sm = boto3.client("secretsmanager", region_name=config.AWS_REGION)
    resp = sm.get_secret_value(SecretId=arn)
    raw = resp.get("SecretString", "")
    try:
        data = json.loads(raw)
        return data.get("client_secret", raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _basic_auth_header() -> Optional[str]:
    secret = _client_secret()
    if secret is None:
        return None
    creds = f"{config.OAUTH_CLIENT_ID}:{secret}".encode("utf-8")
    return "Basic " + base64.b64encode(creds).decode("ascii")


# ── JWKS caching for id-token verification ──────────────────────────────────
_jwks_cache: dict = {"keys": None, "fetched_at": 0.0}
_JWKS_TTL = 3600.0


def _fetch_jwks() -> dict:
    now = time.time()
    if _jwks_cache["keys"] is not None and (now - _jwks_cache["fetched_at"]) < _JWKS_TTL:
        return _jwks_cache["keys"]
    req = urllib.request.Request(config.OAUTH_JWKS_URI, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=config.OAUTH_EXCHANGE_TIMEOUT) as r:  # noqa: S310
        keys = json.loads(r.read().decode("utf-8"))
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys


class AuthProvider:
    """OAuth operations. Keep this the only place tokens are handled."""

    def __init__(self, redirect_uri: Optional[str] = None):
        self.redirect_uri = redirect_uri or config.OAUTH_REDIRECT_URI

    # ---- authorize redirect ----
    def build_authorize_redirect(self, state: str, nonce: str, code_challenge: str) -> str:
        params = {
            "response_type": "code",
            "client_id": config.OAUTH_CLIENT_ID,
            "redirect_uri": self.redirect_uri,
            "scope": config.OAUTH_SCOPES,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return config.OAUTH_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)

    # ---- token endpoint calls ----
    def _post_token(self, form: dict) -> dict:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        basic = _basic_auth_header()
        if basic:
            headers["Authorization"] = basic
        else:
            # public client: include client_id in the body
            form.setdefault("client_id", config.OAUTH_CLIENT_ID)
        data = urllib.parse.urlencode(form).encode("ascii")
        req = urllib.request.Request(
            config.OAUTH_TOKEN_URL, data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=config.OAUTH_EXCHANGE_TIMEOUT) as r:  # noqa: S310
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # noqa: PERF203
            # Do NOT surface the raw OAuth error body to callers.
            raise AuthFailedError(f"token endpoint HTTP {e.code}") from None
        except Exception as e:  # timeout, connection, JSON
            raise AuthFailedError("token endpoint call failed") from None

    def exchange_code(self, code: str, code_verifier: str, expected_nonce: Optional[str] = None) -> TokenBundle:
        if not code or not code_verifier:
            raise AuthFailedError("missing code or verifier")
        resp = self._post_token({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
        })
        return self._bundle_from_token_response(resp, expected_nonce=expected_nonce)

    def refresh(self, refresh_token: str) -> TokenBundle:
        if not refresh_token:
            raise TokenRefreshFailedError("missing refresh token")
        try:
            resp = self._post_token({
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            })
        except AuthFailedError:
            raise TokenRefreshFailedError("refresh failed") from None
        # Cognito refresh may not return a new refresh_token unless rotation is on.
        return self._bundle_from_token_response(resp, prior_refresh_token=refresh_token)

    def revoke(self, refresh_token: str) -> None:
        if not refresh_token:
            return
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        basic = _basic_auth_header()
        form = {"token": refresh_token}
        if basic:
            headers["Authorization"] = basic
        else:
            form["client_id"] = config.OAUTH_CLIENT_ID
        data = urllib.parse.urlencode(form).encode("ascii")
        req = urllib.request.Request(
            config.OAUTH_REVOKE_URL, data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=config.OAUTH_EXCHANGE_TIMEOUT):  # noqa: S310
                return
        except Exception:
            # Best-effort revocation; local session is still destroyed by caller.
            return

    # ---- id token verification ----
    def verify_id_token(self, id_token: str, expected_nonce: Optional[str] = None) -> dict:
        from jose import jwt  # lazy: only needed for id-token verification
        """Verify signature (RS256 via JWKS) + iss/aud/token_use/exp + nonce."""
        if not id_token:
            raise AuthFailedError("missing id token")
        try:
            jwks = _fetch_jwks()
            unverified = jwt.get_unverified_header(id_token)
            kid = unverified.get("kid")
            key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
            if key is None:
                raise AuthFailedError("unknown signing key")
            claims = jwt.decode(
                id_token,
                key,
                algorithms=["RS256"],
                audience=config.OAUTH_CLIENT_ID,
                issuer=config.OAUTH_ISSUER,
                options={"verify_at_hash": False},
            )
        except AuthFailedError:
            raise
        except Exception:
            raise AuthFailedError("id token verification failed") from None

        if claims.get("token_use") not in (None, "id"):
            raise AuthFailedError("unexpected token_use")
        if expected_nonce is not None and claims.get("nonce") != expected_nonce:
            raise AuthFailedError("nonce mismatch")
        return claims

    # ---- helpers ----
    def _bundle_from_token_response(
        self,
        resp: dict,
        *,
        expected_nonce: Optional[str] = None,
        prior_refresh_token: Optional[str] = None,
    ) -> TokenBundle:
        access_token = resp.get("access_token")
        if not access_token:
            raise AuthFailedError("no access token in response")
        expires_in = int(resp.get("expires_in", 3600))
        access_expires_at = int(time.time()) + expires_in
        claims = {}
        id_token = resp.get("id_token")
        if id_token:
            claims = self.verify_id_token(id_token, expected_nonce=expected_nonce)
        refresh_token = resp.get("refresh_token") or prior_refresh_token
        return TokenBundle(
            access_token=access_token,
            refresh_token=refresh_token,
            id_token_claims={"sub": claims.get("sub"), "email": claims.get("email")},
            access_expires_at=access_expires_at,
        )
