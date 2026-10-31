"""Connect the deployed single-user app to your AWS Events identity.

AWS Events uses a loopback-only (native) OAuth client, so the hosted website
cannot complete a browser sign-in. Instead, YOU run this locally: it performs
the native loopback flow (AWS Builder ID sign-in in your browser), then pushes
the resulting token to the deployed backend's POST /admin/connect, which stores
it server-side and links the app to your identity.

The browser that uses the website never sees the token — only this local script
sends it, authenticated by the admin key.

Usage:
    export APP_API_BASE_URL='https://<your-api-id>.execute-api.us-east-1.amazonaws.com'
    export APP_ADMIN_KEY='<the admin key you configured>'
    python docs/spikes/connect.py

Prereqs: pip install -r backend/requirements.txt  (for the token exchange this
uses only stdlib; strands/mcp not required here). Free port 8976 first.
"""

import json
import os
import sys
import urllib.request

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "backend")))

# Reuse the exact loopback token logic from get_token.py.
import importlib.util
_spec = importlib.util.spec_from_file_location("_get_token", os.path.join(HERE, "get_token.py"))
_gt = importlib.util.module_from_spec(_spec)


def _obtain_token():
    """Run the loopback PKCE flow and return the TokenBundle (from auth.py)."""
    import auth
    import config
    import http.server
    import threading
    import urllib.parse

    redirect_uri = os.environ.get("OAUTH_REDIRECT_URI", "http://127.0.0.1:8976")
    parsed = urllib.parse.urlparse(redirect_uri)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 8976

    provider = auth.AuthProvider(redirect_uri=redirect_uri)
    verifier = auth.generate_code_verifier()
    challenge = auth.code_challenge_s256(verifier)
    state = auth.generate_state()
    nonce = auth.generate_nonce()
    authorize_url = provider.build_authorize_redirect(state, nonce, challenge)

    result = {"code": None, "state": None}
    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = (q.get("code") or [None])[0]
            result["state"] = (q.get("state") or [None])[0]
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(b"<html><body><h3>Sign-in complete. Return to the terminal.</h3></body></html>")
            done.set()
        def log_message(self, *a):  # quiet
            pass

    class Reuse(http.server.HTTPServer):
        allow_reuse_address = True

    try:
        server = Reuse((host, port), H)
    except OSError as e:
        print(f"\nCannot bind http://{host}:{port}: {e}")
        print("Free port 8976 (quit/disconnect any other AWS Events MCP client) and retry.")
        sys.exit(2)

    threading.Thread(target=server.handle_request, daemon=True).start()
    import webbrowser
    print("Opening your browser to sign in with your AWS Builder ID...")
    print("If it doesn't open, paste this URL:\n\n" + authorize_url + "\n")
    try:
        webbrowser.open(authorize_url)
    except Exception:
        pass

    if not done.wait(timeout=300):
        print("Timed out waiting for sign-in."); sys.exit(1)
    if not result["code"] or result["state"] != state:
        print("Sign-in failed or state mismatch."); sys.exit(1)

    return provider.exchange_code(result["code"], verifier, expected_nonce=nonce)


def main():
    api = os.environ.get("APP_API_BASE_URL")
    admin_key = os.environ.get("APP_ADMIN_KEY")
    site = os.environ.get("APP_SITE_URL")  # CloudFront URL for the browser claim link
    if not api or not admin_key:
        print("Set APP_API_BASE_URL and APP_ADMIN_KEY first (see this file's docstring).")
        sys.exit(1)

    print("Step 1/2: obtaining a token via the AWS Events loopback flow...")
    bundle = _obtain_token()

    import time
    expires_in = max(60, int(bundle.access_expires_at - time.time()))
    payload = json.dumps({
        "accessToken": bundle.access_token,
        "refreshToken": bundle.refresh_token,
        "expiresIn": expires_in,
        "sub": bundle.id_token_claims.get("sub"),
        "email": bundle.id_token_claims.get("email"),
    }).encode()

    print("Step 2/2: pushing the token to the deployed backend (/admin/connect)...")
    req = urllib.request.Request(
        api.rstrip("/") + "/admin/connect",
        data=payload, method="POST",
        headers={"Content-Type": "application/json", "X-Admin-Key": admin_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"Connect failed: HTTP {e.code}. Check APP_ADMIN_KEY and that the app is deployed.")
        sys.exit(1)

    claim = resp.get("claimCode")
    access = resp.get("eventAccess")
    print("\n" + "=" * 60)
    print(f"  eventAccess = {access}")
    if access == "not_registered":
        print("  Authenticated, but AWS Events does not authorize this identity")
        print("  for re:Invent 2026.")
    if not claim:
        print("  (No claim code returned; is the backend up to date?)")
    else:
        base = (site or "").rstrip("/")
        if base:
            print("\n  FINISH IN YOUR BROWSER - open this one-time link (valid ~2 min):\n")
            print(f"    {base}/#connect={claim}\n")
            print("  It links THIS browser to your identity, then you can search,")
            print("  view details, and favorite sessions.")
        else:
            print("\n  Set APP_SITE_URL to your CloudFront URL to get a click-to-finish")
            print(f"  link. Claim code (valid ~2 min): {claim}")
    print("=" * 60)
    print("\nNote: the app session lasts ~1h (token lifetime). Re-run this to reconnect.")
    print("To fully sign out later: revoke the token and end your Builder ID session")
    print("at https://profile.aws.amazon.com (per the AWS Events docs).")


if __name__ == "__main__":
    main()
