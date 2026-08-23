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
# packages/core/src/runtime.ts (createCodexOAuthFetch and friends), Apache-2.0.

"""Talks to `chatgpt.com/backend-api/codex` with a ChatGPT OAuth session.

:class:`CodexTransport` is the one place that knows how to authenticate a
request, normalize a `/responses` body the way the Codex backend expects,
and buffer/relay its (always-streaming) SSE replies. It is deliberately not
an HTTP server: an in-process agent loop can hold a `CodexTransport` and
call its methods directly, with the local HTTP proxy in
:mod:`codelite.provider.server` being just one more caller.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .auth import EffectiveAuth
from .config import ProviderConfig
from .models import CodexModelInfo, fetch_codex_model_catalog, is_public_codex_model
from .sse import collect_completed_response_from_sse

USER_AGENT = "codelite-provider/0.1 (+https://github.com/; provider layer)"

_MODEL_CATALOG_TTL_SECONDS = 5 * 60
_MODEL_CATALOG_FAILURE_TTL_SECONDS = 60


@dataclass
class UpstreamResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class UpstreamError(RuntimeError):
    pass


class CodexTransport:
    def __init__(self, config: ProviderConfig, get_auth: Callable[[], EffectiveAuth]) -> None:
        self._config = config
        self._get_auth = get_auth
        self._model_cache: dict[str, tuple[float, list[CodexModelInfo], Exception | None]] = {}

    # -- low-level request -----------------------------------------------------

    def _base_headers(self, auth: EffectiveAuth) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "chatgpt-account-id": auth.account_id,
            "User-Agent": USER_AGENT,
        }
        if auth.is_fedramp:
            headers["X-OpenAI-Fedramp"] = "true"
        return headers

    def _raw_request(
        self,
        path: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        stream: bool = False,
    ):
        """Issue one authenticated request against the Codex base URL.

        Returns an open `urllib` response object when `stream=True` (caller
        must read/close it), otherwise a buffered :class:`UpstreamResponse`.
        """
        auth = self._get_auth()
        url = f"{self._config.codex_base_url.rstrip('/')}/{path.lstrip('/')}"
        merged_headers = self._base_headers(auth)
        merged_headers.update(headers or {})

        request = urllib.request.Request(
            url, data=body, method=method, headers=merged_headers
        )
        try:
            response = urllib.request.urlopen(request, timeout=120.0)
        except urllib.error.HTTPError as error:
            if stream:
                return error
            body_bytes = error.read()
            return UpstreamResponse(
                status=error.code, headers=dict(error.headers or {}), body=body_bytes
            )
        except urllib.error.URLError as error:
            raise UpstreamError(f"Request to Codex failed: {error.reason}") from error

        if stream:
            return response
        with response:
            body_bytes = response.read()
        return UpstreamResponse(
            status=response.status, headers=dict(response.headers), body=body_bytes
        )

    def _iter_body(self, response, chunk_size: int = 4096) -> Iterator[bytes]:
        try:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            response.close()

    # -- model catalog -----------------------------------------------------------

    def _fetch_model_catalog(self, auth: EffectiveAuth) -> list[CodexModelInfo]:
        cache_key = f"{auth.account_id}:{auth.is_fedramp}"
        cached = self._model_cache.get(cache_key)
        now = time.monotonic()
        if cached and cached[0] > now:
            if cached[2] is not None:
                raise cached[2]
            return cached[1]

        def do_request(path: str) -> tuple[int, bytes]:
            result = self._raw_request(path)
            assert isinstance(result, UpstreamResponse)
            return result.status, result.body

        try:
            models = fetch_codex_model_catalog(
                do_request, codex_version=self._config.codex_client_version
            )
            self._model_cache[cache_key] = (now + _MODEL_CATALOG_TTL_SECONDS, models, None)
            return models
        except Exception as error:  # noqa: BLE001 - cached and re-raised as-is
            self._model_cache[cache_key] = (
                now + _MODEL_CATALOG_FAILURE_TTL_SECONDS,
                [],
                error,
            )
            raise

    def list_model_ids(self) -> list[str]:
        """Public model slugs Codex will serve this account, plus the image model."""
        auth = self._get_auth()
        models = [m for m in self._fetch_model_catalog(auth) if is_public_codex_model(m)]
        slugs = [m.slug for m in models]
        if self._config.image_model not in slugs:
            slugs.append(self._config.image_model)
        return slugs

    def resolve_model_info(self, model_slug: str) -> CodexModelInfo | None:
        auth = self._get_auth()
        try:
            for model in self._fetch_model_catalog(auth):
                if model.slug == model_slug:
                    return model
        except Exception:  # noqa: BLE001 - model defaults are best-effort
            return None
        return None

    # -- /responses ------------------------------------------------------------

    def normalize_responses_body(
        self, body: dict[str, Any], *, force_stream: bool = True
    ) -> tuple[dict[str, Any], CodexModelInfo | None]:
        """Port of `normalizeCodexResponsesBodyInternal` + `applyModelDefaults`."""
        normalized = dict(body)
        instructions = normalized.get("instructions")
        if not isinstance(instructions, str):
            instructions = self._config.instructions
        normalized["instructions"] = instructions

        input_value = normalized.get("input")
        if isinstance(input_value, str):
            normalized["input"] = [
                {"role": "user", "content": [{"type": "input_text", "text": input_value}]}
            ]

        normalized["store"] = False

        include = normalized.get("include")
        include_list = [v for v in include if isinstance(v, str)] if isinstance(include, list) else []
        if "reasoning.encrypted_content" not in include_list:
            include_list.append("reasoning.encrypted_content")
        normalized["include"] = include_list

        model_info = None
        model = normalized.get("model")
        if isinstance(model, str):
            model_info = self.resolve_model_info(model)
        self._apply_model_defaults(normalized, model_info)

        if force_stream:
            normalized["stream"] = True
        normalized.pop("max_output_tokens", None)
        return normalized, model_info

    @staticmethod
    def _apply_model_defaults(normalized: dict[str, Any], model_info: CodexModelInfo | None) -> None:
        if model_info is None:
            return

        reasoning = dict(normalized.get("reasoning") or {})
        if reasoning.get("effort") is None and model_info.default_reasoning_level is not None:
            reasoning["effort"] = model_info.default_reasoning_level
        if model_info.use_responses_lite:
            reasoning["context"] = "all_turns"
        if reasoning:
            normalized["reasoning"] = reasoning

        if model_info.support_verbosity and model_info.default_verbosity is not None:
            text = dict(normalized.get("text") or {})
            text.setdefault("verbosity", model_info.default_verbosity)
            normalized["text"] = text

        if not model_info.use_responses_lite:
            return

        input_items = list(normalized.get("input") or [])
        prefix: list[Any] = []
        tools = normalized.get("tools") or []
        has_additional_tools = any(
            isinstance(item, dict) and item.get("type") == "additional_tools"
            for item in input_items
        )
        if tools and not has_additional_tools:
            prefix.append({"type": "additional_tools", "role": "developer", "tools": tools})

        if isinstance(normalized.get("instructions"), str) and normalized["instructions"]:
            prefix.append(
                {
                    "role": "developer",
                    "content": [{"type": "input_text", "text": normalized["instructions"]}],
                }
            )

        normalized["input"] = prefix + input_items
        normalized["instructions"] = ""
        normalized["parallel_tool_calls"] = False
        normalized.pop("tools", None)

    def send_responses_request(
        self, body: dict[str, Any], *, stream: bool
    ) -> dict[str, Any] | Iterator[bytes]:
        """POST to `/responses`.

        Codex is always asked for a stream internally (`force_stream=True`);
        when the caller didn't want one (`stream=False`), the SSE reply is
        buffered here into one JSON object. When the caller does want a
        stream, the raw upstream SSE byte chunks are handed back for the
        caller to relay or re-translate.
        """
        normalized, model_info = self.normalize_responses_body(body, force_stream=True)
        payload = json.dumps(normalized).encode("utf-8")
        extra_headers = {"Content-Type": "application/json"}
        if model_info is not None and model_info.use_responses_lite:
            extra_headers["x-openai-internal-codex-responses-lite"] = "true"

        response = self._raw_request(
            "/responses", method="POST", headers=extra_headers, body=payload, stream=True
        )

        if isinstance(response, urllib.error.HTTPError):
            error_body = response.read()
            response.close()
            raise UpstreamError(
                f"Codex /responses request failed with HTTP {response.code}: "
                f"{error_body.decode('utf-8', errors='replace')}"
            )

        if stream:
            return self._iter_body(response)

        return collect_completed_response_from_sse(self._iter_body(response))

    # -- images ------------------------------------------------------------------

    def send_image_request(self, path: str, body: bytes) -> UpstreamResponse:
        result = self._raw_request(
            path, method="POST", headers={"Content-Type": "application/json"}, body=body
        )
        assert isinstance(result, UpstreamResponse)
        return result
