"""OpenID Connect login: authorization code flow with PKCE, state and nonce.

The ID token is taken straight from the provider's token endpoint over a verified TLS connection,
which OpenID Connect Core 1.0 (3.1.3.7, item 6) allows in place of checking its signature; its
issuer, audience, authorized party, expiry, issue time and nonce are still checked. That keeps
this module free of a JOSE/crypto dependency.

The per-login state (state, nonce, PKCE verifier, where to go afterwards) travels in a short-lived
cookie signed with the server's secret key, so nothing is stored server-side between the two legs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx

from ..config import Settings

FLOW_COOKIE = "dvrbridge_oidc"
FLOW_TTL = 600
MAX_BODY = 1024 * 1024
CLOCK_SKEW = 120


class OidcError(Exception):
    """Something went wrong; the message is safe to show the user."""


@dataclass(frozen=True)
class Identity:
    username: str
    groups: tuple[str, ...]
    subject: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _secure_url(url: str) -> bool:
    u = urlparse(url or "")
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1"))


# ---- signed flow cookie -----------------------------------------------------------------------
def sign_flow(secret_key: str, data: dict) -> str:
    body = _b64url(json.dumps(data, separators=(",", ":")).encode())
    mac = hmac.new(secret_key.encode(), b"oidc-flow." + body.encode(), hashlib.sha256).digest()
    return body + "." + _b64url(mac)


def read_flow(secret_key: str, value: str | None) -> dict | None:
    try:
        body, mac = (value or "").split(".")
        want = hmac.new(secret_key.encode(), b"oidc-flow." + body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url_decode(mac), want):
            return None
        data = json.loads(_b64url_decode(body))
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and data.get("exp", 0) > time.time() else None


def safe_next(path: str | None) -> str:
    """Only same-site paths; never an absolute or protocol-relative URL."""
    p = path or "/"
    if not p.startswith("/") or p.startswith("//") or "\\" in p or any(ord(c) < 32 for c in p):
        return "/"
    return p


class OidcClient:
    def __init__(self, s: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.s = s
        self.redirect_uri = s.public_url.rstrip("/") + "/auth/oidc/callback"
        self._http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(10.0), follow_redirects=False,
                                       headers={"Accept": "application/json", "User-Agent": "dvr-bridge"})
        self._discovery: dict | None = None
        self._discovered_at = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    async def _json(self, method: str, url: str, **kw) -> dict:
        try:
            async with self._http.stream(method, url, **kw) as r:
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BODY:
                        raise OidcError("The identity provider sent an oversized response.")
                status = r.status_code
        except httpx.HTTPError as e:
            raise OidcError(f"Could not reach the identity provider ({type(e).__name__}).") from None
        try:
            doc = json.loads(body)
        except ValueError:
            raise OidcError(f"The identity provider answered HTTP {status} without JSON.") from None
        if not isinstance(doc, dict):
            raise OidcError("The identity provider sent an unexpected response.")
        if status >= 400:
            detail = doc.get("error_description") or doc.get("error") or f"HTTP {status}"
            raise OidcError(f"The identity provider refused the request: {str(detail)[:200]}")
        return doc

    async def discovery(self) -> dict:
        if self._discovery and time.time() - self._discovered_at < 3600:
            return self._discovery
        url = self.s.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
        doc = await self._json("GET", url)
        if str(doc.get("issuer", "")).rstrip("/") != self.s.oidc_issuer.rstrip("/"):
            raise OidcError("The identity provider's issuer does not match auth.oidc_issuer.")
        for key in ("authorization_endpoint", "token_endpoint"):
            if not _secure_url(doc.get(key, "")):
                raise OidcError(f"The identity provider's {key} is missing or not https.")
        if doc.get("userinfo_endpoint") and not _secure_url(doc["userinfo_endpoint"]):
            raise OidcError("The identity provider's userinfo_endpoint is not https.")
        self._discovery, self._discovered_at = doc, time.time()
        return doc

    async def start(self, next_path: str) -> tuple[str, str]:
        """(URL to send the browser to, value for the signed flow cookie)."""
        d = await self.discovery()
        verifier = secrets.token_urlsafe(48)
        flow = {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24), "verifier": verifier,
                "next": safe_next(next_path), "exp": time.time() + FLOW_TTL}
        params = {
            "response_type": "code",
            "client_id": self.s.oidc_client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.s.oidc_scopes,
            "state": flow["state"],
            "nonce": flow["nonce"],
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
        }
        sep = "&" if "?" in d["authorization_endpoint"] else "?"
        return d["authorization_endpoint"] + sep + urlencode(params), sign_flow(self.s.secret_key, flow)

    async def finish(self, flow: dict | None, params: dict[str, str]) -> tuple[Identity, str]:
        """Complete the callback. Returns the identity and where to send the user."""
        if not flow:
            raise OidcError("The login took too long or was started in another browser. Please try again.")
        if params.get("error"):
            raise OidcError(f"The identity provider did not log you in: {params.get('error_description') or params['error']}"[:300])
        if not hmac.compare_digest(str(params.get("state", "")), str(flow.get("state", ""))):
            raise OidcError("The login response did not match this browser's login. Please try again.")
        code = params.get("code")
        if not code:
            raise OidcError("The identity provider sent no authorization code.")
        d = await self.discovery()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                "code_verifier": flow["verifier"]}
        methods = d.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        kw: dict = {}
        if "client_secret_basic" in methods:
            from urllib.parse import quote

            kw["auth"] = (quote(self.s.oidc_client_id, safe=""), quote(self.s.oidc_client_secret, safe=""))
        else:
            data.update(client_id=self.s.oidc_client_id, client_secret=self.s.oidc_client_secret)
        tokens = await self._json("POST", d["token_endpoint"], data=data, **kw)
        claims = self._id_claims(tokens.get("id_token"), d["issuer"], flow["nonce"])
        if d.get("userinfo_endpoint") and tokens.get("access_token") and (
                self.s.oidc_groups_claim not in claims or self.s.oidc_username_claim not in claims):
            info = await self._json("GET", d["userinfo_endpoint"],
                                    headers={"Authorization": f"Bearer {tokens['access_token']}"})
            if info.get("sub") != claims["sub"]:
                raise OidcError("The identity provider's user info did not match the login.")
            claims = {**info, **claims}
        return self._identity(claims), safe_next(flow.get("next"))

    def _id_claims(self, id_token: str | None, issuer: str, nonce: str) -> dict:
        try:
            if not isinstance(id_token, str):
                raise ValueError
            _, payload, _sig = id_token.split(".")
            claims = json.loads(_b64url_decode(payload))
            if not isinstance(claims, dict):
                raise ValueError
        except (ValueError, TypeError):
            raise OidcError("The identity provider sent no usable ID token.") from None
        now = time.time()
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        problems = [
            claims.get("iss") != issuer and "issuer",
            self.s.oidc_client_id not in auds and "audience",
            len(auds) > 1 and claims.get("azp") != self.s.oidc_client_id and "authorized party",
            (not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - CLOCK_SKEW) and "expiry",
            isinstance(claims.get("iat"), (int, float)) and claims["iat"] > now + CLOCK_SKEW and "issue time",
            not hmac.compare_digest(str(claims.get("nonce", "")), nonce) and "nonce",
            not claims.get("sub") and "subject",
        ]
        bad = [p for p in problems if p]
        if bad:
            raise OidcError(f"The ID token failed its checks ({', '.join(bad)}).")
        return claims

    def _identity(self, claims: dict) -> Identity:
        # An unverified e-mail address is whatever the user typed at the provider: not an identity.
        email = claims.get("email") if claims.get("email_verified") is True else None
        raw = claims.get(self.s.oidc_username_claim) or email or claims.get("sub")
        username = str(raw or "").strip()
        if not 1 <= len(username) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in username):
            raise OidcError("The identity provider sent no usable username.")
        groups = claims.get(self.s.oidc_groups_claim) or []
        if isinstance(groups, str):
            groups = [g for g in groups.replace("|", ",").split(",")]
        groups = tuple(str(g).strip() for g in groups if str(g).strip()) if isinstance(groups, (list, tuple, str)) else ()
        return Identity(username, groups, f"{claims['iss']} {claims['sub']}")
