"""Server-side session + OAuth transient store on DynamoDB (single-user app).

This is a SINGLE-USER personal application (Cloud With Girish demo), so there is
one app session at a time. Tokens are still kept SERVER-SIDE and never returned
to the browser — the browser only holds an opaque appSessionId in an HttpOnly
cookie. That security property matters regardless of user count.

Single DynamoDB table (config.DDB_TABLE_NAME):
  SESSION#<appSessionId> | META  -> auth state + KMS-encrypted access token +
                                    Secrets Manager ref to the refresh token
  OAUTH#<state>          | META  -> codeVerifier, nonce (short TTL, single-use)
  CONV#<...>             | META  -> conversation context (see session_context.py)

Simplifications vs the earlier multi-user design: no concurrency refresh-lock
(a single user won't race its own refresh), and a straightforward
refresh-and-persist path.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Optional

import config
from auth import AuthProvider
from errors import AuthRequiredError, TokenRefreshFailedError
from models import AuthState, TokenBundle


def _now() -> int:
    return int(time.time())


def _key(app_session_id: str) -> dict:
    return {"PK": {"S": f"SESSION#{app_session_id}"}, "SK": {"S": "META"}}


class SessionManager:
    def __init__(self, ddb=None, kms=None, secrets_client=None,
                 auth_provider: Optional[AuthProvider] = None,
                 kms_key_id: Optional[str] = None):
        # Lazy boto3 so unit tests can inject fakes without AWS.
        self._ddb = ddb
        self._kms = kms
        self._sm = secrets_client
        self.auth = auth_provider or AuthProvider()
        self.kms_key_id = kms_key_id

    # ---- boto3 lazy accessors ----
    @property
    def ddb(self):
        if self._ddb is None:
            import boto3
            self._ddb = boto3.client("dynamodb", region_name=config.AWS_REGION)
        return self._ddb

    @property
    def kms(self):
        if self._kms is None:
            import boto3
            self._kms = boto3.client("kms", region_name=config.AWS_REGION)
        return self._kms

    @property
    def sm(self):
        if self._sm is None:
            import boto3
            self._sm = boto3.client("secretsmanager", region_name=config.AWS_REGION)
        return self._sm

    # ---- token encryption (KMS, with base64 fallback for local dev) ----
    def _encrypt(self, plaintext: str) -> str:
        if not self.kms_key_id:
            return "b64:" + base64.b64encode(plaintext.encode()).decode()
        resp = self.kms.encrypt(KeyId=self.kms_key_id, Plaintext=plaintext.encode())
        return "kms:" + base64.b64encode(resp["CiphertextBlob"]).decode()

    def _decrypt(self, blob: str) -> str:
        if blob.startswith("b64:"):
            return base64.b64decode(blob[4:]).decode()
        if blob.startswith("kms:"):
            resp = self.kms.decrypt(CiphertextBlob=base64.b64decode(blob[4:]))
            return resp["Plaintext"].decode()
        raise ValueError("unknown ciphertext format")

    # ---- refresh token storage (Secrets Manager) ----
    def _store_refresh_token(self, app_session_id: str, refresh_token: Optional[str]) -> Optional[str]:
        if not refresh_token:
            return None
        name = f"reinvent-assistant/refresh/{app_session_id}"
        payload = json.dumps({"refresh_token": refresh_token})
        try:
            return self.sm.create_secret(Name=name, SecretString=payload)["ARN"]
        except self.sm.exceptions.ResourceExistsException:
            self.sm.put_secret_value(SecretId=name, SecretString=payload)
            return name

    def _load_refresh_token(self, ref: Optional[str]) -> Optional[str]:
        if not ref:
            return None
        resp = self.sm.get_secret_value(SecretId=ref)
        return json.loads(resp["SecretString"]).get("refresh_token")

    def _delete_refresh_token(self, ref: Optional[str]) -> None:
        if not ref:
            return
        try:
            self.sm.delete_secret(SecretId=ref, ForceDeleteWithoutRecovery=True)
        except Exception:
            pass

    # ---- OAuth transient (state/nonce/verifier), single-use ----
    def put_oauth_transient(self, state: str, nonce: str, code_verifier: str) -> None:
        self.ddb.put_item(
            TableName=config.DDB_TABLE_NAME,
            Item={
                "PK": {"S": f"OAUTH#{state}"},
                "SK": {"S": "META"},
                "nonce": {"S": nonce},
                "codeVerifier": {"S": code_verifier},
                "ttl": {"N": str(_now() + config.OAUTH_TRANSIENT_TTL_SECONDS)},
            },
        )

    def pop_oauth_transient(self, state: str) -> Optional[dict]:
        key = {"PK": {"S": f"OAUTH#{state}"}, "SK": {"S": "META"}}
        item = self.ddb.get_item(TableName=config.DDB_TABLE_NAME, Key=key).get("Item")
        if not item:
            return None
        self.ddb.delete_item(TableName=config.DDB_TABLE_NAME, Key=key)  # single-use
        if int(item.get("ttl", {}).get("N", "0")) < _now():
            return None
        return {
            "nonce": item.get("nonce", {}).get("S"),
            "codeVerifier": item.get("codeVerifier", {}).get("S"),
        }

    # ---- one-time browser claim code (native-connect handoff) ----
    def create_claim_code(self, app_session_id: str, ttl_seconds: int = 120) -> str:
        """Create a short-lived, single-use code that the browser exchanges for
        the session cookie. Maps CLAIM#<code> -> appSessionId in DynamoDB."""
        code = secrets.token_urlsafe(32)
        self.ddb.put_item(
            TableName=config.DDB_TABLE_NAME,
            Item={
                "PK": {"S": f"CLAIM#{code}"},
                "SK": {"S": "META"},
                "appSessionId": {"S": app_session_id},
                "ttl": {"N": str(_now() + ttl_seconds)},
            },
        )
        return code

    def redeem_claim_code(self, code: str) -> Optional[str]:
        """Single-use: return the appSessionId for a valid, unexpired code and
        delete it. None if missing/expired."""
        key = {"PK": {"S": f"CLAIM#{code}"}, "SK": {"S": "META"}}
        item = self.ddb.get_item(TableName=config.DDB_TABLE_NAME, Key=key).get("Item")
        if not item:
            return None
        self.ddb.delete_item(TableName=config.DDB_TABLE_NAME, Key=key)  # single-use
        if int(item.get("ttl", {}).get("N", "0")) < _now():
            return None
        return item.get("appSessionId", {}).get("S")

    # ---- session lifecycle ----
    def create_session(self, tokens: TokenBundle, auth_state: AuthState) -> str:
        app_session_id = secrets.token_urlsafe(32)
        refresh_ref = self._store_refresh_token(app_session_id, tokens.refresh_token)
        item = {
            **_key(app_session_id),
            "authState": {"S": auth_state.value},
            "accessTokenEnc": {"S": self._encrypt(tokens.access_token)},
            "accessExpiresAt": {"N": str(tokens.access_expires_at)},
            "csrfToken": {"S": secrets.token_urlsafe(32)},
            "ttl": {"N": str(_now() + config.SESSION_TTL_SECONDS)},
        }
        if refresh_ref:
            item["refreshTokenRef"] = {"S": refresh_ref}
        if tokens.id_token_claims.get("sub"):
            item["sub"] = {"S": tokens.id_token_claims["sub"]}
        if tokens.id_token_claims.get("email"):
            item["email"] = {"S": tokens.id_token_claims["email"]}
        self.ddb.put_item(TableName=config.DDB_TABLE_NAME, Item=item)
        return app_session_id

    def get_session(self, app_session_id: Optional[str]) -> Optional[dict]:
        if not app_session_id:
            return None
        item = self.ddb.get_item(
            TableName=config.DDB_TABLE_NAME, Key=_key(app_session_id)
        ).get("Item")
        if not item or int(item.get("ttl", {}).get("N", "0")) < _now():
            return None
        return {
            "appSessionId": app_session_id,
            "authState": item.get("authState", {}).get("S", AuthState.ERROR.value),
            "sub": item.get("sub", {}).get("S"),
            "email": item.get("email", {}).get("S"),
            "accessExpiresAt": int(item.get("accessExpiresAt", {}).get("N", "0")),
            "csrfToken": item.get("csrfToken", {}).get("S"),
            "refreshTokenRef": item.get("refreshTokenRef", {}).get("S"),
            "_accessTokenEnc": item.get("accessTokenEnc", {}).get("S"),
        }

    def set_auth_state(self, app_session_id: str, auth_state: AuthState) -> None:
        self.ddb.update_item(
            TableName=config.DDB_TABLE_NAME, Key=_key(app_session_id),
            UpdateExpression="SET authState = :s",
            ExpressionAttributeValues={":s": {"S": auth_state.value}},
        )

    def delete_session(self, app_session_id: str) -> Optional[str]:
        """Delete session + refresh secret. Returns the refresh token (for revoke)."""
        sess = self.get_session(app_session_id)
        refresh_token = None
        if sess:
            try:
                refresh_token = self._load_refresh_token(sess.get("refreshTokenRef"))
            except Exception:
                refresh_token = None
            self._delete_refresh_token(sess.get("refreshTokenRef"))
        self.ddb.delete_item(TableName=config.DDB_TABLE_NAME, Key=_key(app_session_id))
        return refresh_token

    # ---- access token retrieval, refreshing when expired ----
    def get_valid_access_token(self, app_session_id: str, skew_seconds: int = 60) -> str:
        sess = self.get_session(app_session_id)
        if not sess:
            raise AuthRequiredError()
        if sess["accessExpiresAt"] - skew_seconds > _now():
            return self._decrypt(sess["_accessTokenEnc"])

        # Expired: refresh once using the stored refresh token and persist.
        refresh_token = None
        try:
            refresh_token = self._load_refresh_token(sess.get("refreshTokenRef"))
        except Exception:
            refresh_token = None
        if not refresh_token:
            raise TokenRefreshFailedError()

        new_tokens = self.auth.refresh(refresh_token)  # raises TokenRefreshFailedError on failure

        new_ref = sess.get("refreshTokenRef")
        if new_tokens.refresh_token and new_tokens.refresh_token != refresh_token:
            new_ref = self._store_refresh_token(app_session_id, new_tokens.refresh_token)

        self.ddb.update_item(
            TableName=config.DDB_TABLE_NAME, Key=_key(app_session_id),
            UpdateExpression="SET accessTokenEnc = :a, accessExpiresAt = :e, refreshTokenRef = :r",
            ExpressionAttributeValues={
                ":a": {"S": self._encrypt(new_tokens.access_token)},
                ":e": {"N": str(new_tokens.access_expires_at)},
                ":r": {"S": new_ref or sess.get("refreshTokenRef", "")},
            },
        )
        return new_tokens.access_token
