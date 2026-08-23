# Copyright 2026 Code Lite contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Ported from openai-oauth (https://github.com/EvanZhouDev/openai-oauth),
# packages/core/src/runtime.ts (JWT/refresh helpers) and
# packages/local/src/auth-file.ts (auth.json load/save/refresh), Apache-2.0.

"""Creates, loads, refreshes, and persists Code Lite's ChatGPT OAuth tokens.

Tokens live in Code Lite's own data directory, completely separate from the
official Codex CLI. Code Lite creates its token file itself through the OAuth
authorization-code flow with PKCE.

Security note: token values are never logged. Helpers here only ever report
presence/length for diagnostics, never the token text itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import (
    REFRESH_EXPIRY_MARGIN_SECONDS,
    REFRESH_INTERVAL_SECONDS,
    ProviderConfig,
)


class AuthError(RuntimeError):
    """Raised when tokens cannot be loaded, refreshed, or are missing."""


@dataclass
class OAuthRequest:
    """The public URL and private verifier for one PKCE login attempt."""

    authorization_url: str
    state: str
    code_verifier: str
    redirect_uri: str


@dataclass
class AuthStatus:
    """Secret-free account state suitable for the local UI."""

    authenticated: bool
    account_label: str | None = None
    email: str | None = None
    source_path: Path | None = None
    error: str | None = None


# -- JWT helpers --------------------------------------------------------------


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def parse_jwt_claims(token: str | None) -> dict[str, Any] | None:
    """Decode a JWT's payload without verifying its signature.

    We never need to verify the signature here: the token is only ever used
    by round-tripping it straight back to OpenAI, which does its own
    verification. We only read `exp`/`chatgpt_account_id` out of it locally.
    """
    if not token or "." not in token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = _b64url_decode(parts[1])
        parsed = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def derive_account_id(token: str | None) -> str | None:
    claims = parse_jwt_claims(token)
    if not claims:
        return None

    auth_claim = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        account_id = auth_claim.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            return account_id

    top_level = claims.get("chatgpt_account_id")
    if isinstance(top_level, str) and top_level:
        return top_level

    organizations = claims.get("organizations")
    if isinstance(organizations, list) and organizations:
        first = organizations[0]
        if isinstance(first, dict) and isinstance(first.get("id"), str) and first["id"]:
            return first["id"]

    return None


def derive_is_fedramp(token: str | None) -> bool:
    claims = parse_jwt_claims(token)
    if not claims:
        return False
    auth_claim = claims.get("https://api.openai.com/auth")
    return isinstance(auth_claim, dict) and auth_claim.get("chatgpt_account_is_fedramp") is True


# -- OAuth token endpoint -------------------------------------------------------


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str | None
    id_token: str | None
    expires_in: int | None
    account_id: str | None
    is_fedramp: bool
    raw: dict[str, Any]


def _resolve_token_url(issuer: str, token_url: str | None) -> str:
    return token_url or f"{issuer.rstrip('/')}/oauth/token"


def _request_token_payload(
    url: str,
    payload: dict[str, str],
    *,
    form_encoded: bool,
    timeout: float = 30.0,
) -> dict[str, Any]:
    if form_encoded:
        body = urllib.parse.urlencode(payload).encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(payload).encode("utf-8")
        content_type = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raw_body = error.read().decode("utf-8", errors="replace")
        detail = ""
        try:
            parsed_error = json.loads(raw_body)
            if isinstance(parsed_error, dict):
                message = (
                    parsed_error.get("error_description")
                    or parsed_error.get("message")
                    or parsed_error.get("detail")
                )
                if isinstance(message, str):
                    detail = f" {message}"
        except ValueError:
            pass
        raise AuthError(
            f"OpenAI OAuth token request failed with HTTP {error.code}.{detail}"
        ) from error
    except urllib.error.URLError as error:
        raise AuthError(f"OpenAI OAuth token request failed: {error.reason}") from error

    try:
        parsed = json.loads(raw_body)
    except ValueError as error:
        raise AuthError("OpenAI OAuth token response was not valid JSON.") from error
    if not isinstance(parsed, dict):
        raise AuthError("OpenAI OAuth token response must be a JSON object.")
    return parsed


def _post_json(url: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    strings = {key: str(value) for key, value in payload.items()}
    return _request_token_payload(url, strings, form_encoded=False, timeout=timeout)


def _to_token_response(payload: dict[str, Any]) -> TokenResponse:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise AuthError("OpenAI OAuth token response did not include access_token.")

    id_token = payload.get("id_token") if isinstance(payload.get("id_token"), str) else None
    refresh_token = (
        payload.get("refresh_token") if isinstance(payload.get("refresh_token"), str) else None
    )
    expires_in = payload.get("expires_in") if isinstance(payload.get("expires_in"), int) else None

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        expires_in=expires_in,
        account_id=derive_account_id(id_token) or derive_account_id(access_token),
        is_fedramp=derive_is_fedramp(id_token) or derive_is_fedramp(access_token),
        raw=payload,
    )


def refresh_oauth_tokens(
    refresh_token: str,
    *,
    client_id: str,
    issuer: str,
    token_url: str | None = None,
) -> TokenResponse:
    """Exchange a refresh token for a new access/id/refresh token triple."""
    url = _resolve_token_url(issuer, token_url)
    payload = _post_json(
        url,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )
    return _to_token_response(payload)


def create_oauth_request(
    *,
    redirect_uri: str,
    client_id: str,
    issuer: str,
    scope: str,
) -> OAuthRequest:
    """Create the ChatGPT authorization URL and its PKCE verifier."""
    state = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
    )
    return OAuthRequest(
        authorization_url=f"{issuer.rstrip('/')}/oauth/authorize?{query}",
        state=state,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
    )


def exchange_oauth_code(
    code: str,
    code_verifier: str,
    redirect_uri: str,
    *,
    client_id: str,
    issuer: str,
    token_url: str | None = None,
) -> TokenResponse:
    """Exchange the local callback's authorization code for Codex tokens."""
    payload = _request_token_payload(
        _resolve_token_url(issuer, token_url),
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
        form_encoded=True,
    )
    return _to_token_response(payload)


# -- auth.json load / refresh / persist -----------------------------------------


@dataclass
class EffectiveAuth:
    access_token: str
    account_id: str
    is_fedramp: bool = False
    id_token: str | None = None
    refresh_token: str | None = None
    source_path: Path | None = None
    last_refresh: str | None = None


def _read_auth_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            content = handle.read()
    except FileNotFoundError:
        return {}
    try:
        parsed = json.loads(content)
    except ValueError as error:
        raise AuthError(f"Auth file at {path} is not valid JSON.") from error
    if not isinstance(parsed, dict):
        raise AuthError(f"Auth file at {path} must contain a JSON object.")
    return parsed


def _write_auth_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)


def save_auth_tokens(
    config: ProviderConfig,
    token: TokenResponse,
    *,
    now: datetime | None = None,
) -> EffectiveAuth:
    """Atomically save a successful login without discarding unrelated fields."""
    account_id = token.account_id or derive_account_id(token.id_token)
    if not account_id:
        raise AuthError("ChatGPT account id not found in OpenAI OAuth token response.")
    moment = now or datetime.now(tz=timezone.utc)
    path = config.auth_file_path
    existing = _read_auth_file(path)
    last_refresh = moment.isoformat()
    _write_auth_file(
        path,
        {
            **existing,
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": token.id_token,
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "account_id": account_id,
            },
            "last_refresh": last_refresh,
        },
    )
    return EffectiveAuth(
        access_token=token.access_token,
        account_id=account_id,
        is_fedramp=token.is_fedramp,
        id_token=token.id_token,
        refresh_token=token.refresh_token,
        source_path=path,
        last_refresh=last_refresh,
    )


def get_auth_status(config: ProviderConfig) -> AuthStatus:
    """Inspect the local token file without refreshing or revealing secrets."""
    path = config.auth_file_path
    try:
        data = _read_auth_file(path)
    except AuthError as error:
        return AuthStatus(False, source_path=path, error=str(error))
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    access_token = tokens.get("access_token")
    id_token = tokens.get("id_token")
    account_id = tokens.get("account_id") or derive_account_id(id_token)
    if not isinstance(access_token, str) or not access_token or not account_id:
        return AuthStatus(False, source_path=path)

    claims = parse_jwt_claims(id_token) or parse_jwt_claims(access_token) or {}
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    name = claims.get("name") if isinstance(claims.get("name"), str) else None
    return AuthStatus(
        True,
        account_label=name or email or "ChatGPT account",
        email=email,
        source_path=path,
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _should_refresh(access_token: str | None, last_refresh: str | None, now: datetime) -> bool:
    if not access_token:
        return True

    claims = parse_jwt_claims(access_token)
    exp = claims.get("exp") if claims else None
    if isinstance(exp, (int, float)):
        expiry = datetime.fromtimestamp(exp, tz=timezone.utc)
        if expiry.timestamp() <= now.timestamp() + REFRESH_EXPIRY_MARGIN_SECONDS:
            return True

    refreshed_at = _parse_iso(last_refresh)
    if refreshed_at:
        return refreshed_at.timestamp() <= now.timestamp() - REFRESH_INTERVAL_SECONDS
    return False


def load_auth_tokens(config: ProviderConfig, *, now: datetime | None = None) -> EffectiveAuth:
    """Load tokens from ``auth.json``, refreshing them first if they're stale.

    Mirrors ``loadAuthTokens`` in the reference implementation: a refresh is
    triggered when the access token's own `exp` claim is near expiry, or
    (as a fallback for tokens without a readable `exp`) when more than
    ``REFRESH_INTERVAL_SECONDS`` have passed since the last recorded refresh.
    A successful refresh is written straight back to the same file.
    """
    moment = now or datetime.now(tz=timezone.utc)
    path = config.auth_file_path
    auth_data = _read_auth_file(path)
    tokens = auth_data.get("tokens") if isinstance(auth_data.get("tokens"), dict) else {}

    access_token = tokens.get("access_token")
    id_token = tokens.get("id_token")
    refresh_token = tokens.get("refresh_token")
    account_id = tokens.get("account_id") or derive_account_id(id_token)
    is_fedramp = derive_is_fedramp(id_token) or derive_is_fedramp(access_token)
    last_refresh = auth_data.get("last_refresh")

    if (
        config.ensure_fresh_tokens
        and isinstance(refresh_token, str)
        and refresh_token
        and _should_refresh(access_token, last_refresh, moment)
    ):
        refreshed = refresh_oauth_tokens(
            refresh_token,
            client_id=config.oauth_client_id,
            issuer=config.oauth_issuer,
            token_url=config.oauth_token_url,
        )
        access_token = refreshed.access_token
        id_token = refreshed.id_token or id_token
        refresh_token = refreshed.refresh_token or refresh_token
        account_id = refreshed.account_id or account_id
        is_fedramp = is_fedramp or refreshed.is_fedramp
        last_refresh = moment.isoformat()

        auth_data = {
            **auth_data,
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": id_token,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "account_id": account_id,
            },
            "last_refresh": last_refresh,
        }
        _write_auth_file(path, auth_data)

    if not access_token:
        raise AuthError(
            f"ChatGPT access token not found in {path}. Sign in from Code Lite."
        )
    if not account_id:
        raise AuthError(
            f"ChatGPT account id not found in {path}. Sign in from Code Lite again."
        )

    return EffectiveAuth(
        access_token=access_token,
        account_id=account_id,
        is_fedramp=is_fedramp,
        id_token=id_token,
        refresh_token=refresh_token,
        source_path=path,
        last_refresh=last_refresh,
    )


def describe_auth_file(config: ProviderConfig) -> str:
    """Human-readable, secret-free description of the auth file's state.

    Only reports presence/length of fields -- never their values. Useful for
    troubleshooting without risking a token leaking into logs or chat.
    """
    path = config.auth_file_path
    if not path.exists():
        return f"{path}: not found"
    try:
        data = _read_auth_file(path)
    except AuthError as error:
        return f"{path}: {error}"
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    fields = ", ".join(
        f"{key}=len:{len(value)}" if isinstance(value, str) else f"{key}=missing"
        for key, value in (
            ("access_token", tokens.get("access_token")),
            ("id_token", tokens.get("id_token")),
            ("refresh_token", tokens.get("refresh_token")),
            ("account_id", tokens.get("account_id")),
        )
    )
    return f"{path}: auth_mode={data.get('auth_mode')!r}, last_refresh={data.get('last_refresh')!r}, {fields}"
