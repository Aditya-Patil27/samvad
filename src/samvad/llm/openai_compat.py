# OWNER: P2 -- see docs/WORK.md. Do not edit if you are not P2.
"""OpenAI-compatible backends: Groq, NVIDIA NIM, and anything else that speaks
`POST /chat/completions`.

WHY ONE MODULE FOR SEVERAL PROVIDERS
Groq and NVIDIA NIM both expose the OpenAI chat-completions shape, so the only
things that differ are a base URL, an env var, and the model id. One module,
one set of boundary checks, one retry policy -- three copies of this file would
drift and only one of them would be the one under test.

WHY THIS EXISTS AT ALL
Measurement 5 needs an executor and a reviewer whose training priors are
UNCORRELATED. Locally that means two models resident at once, and a 6GB card
holding a 6GB model has no room for a second. A free-tier hosted model removes
the VRAM constraint entirely and is far faster than 19 tok/s on a laptop GPU.

This is not a departure from the architecture: config/peers.example.yaml
already assigns hosted API models to two of the four peers. Agents still
exchange signed messages over the LAN; where a given agent gets its tokens is
a separate axis from how the mesh talks.

KEYS COME FROM THE ENVIRONMENT, NEVER FROM A FILE IN THIS REPO.
Nothing here logs, echoes, or persists a key -- error messages name the
provider and the env var, never the value.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from samvad.llm.base import Completion, Usage, render_prompt


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible endpoint."""

    name: str
    base_url: str
    env_var: str
    signup: str


#: Free-tier providers. Add a row rather than a module.
PROVIDERS: dict[str, Provider] = {
    "groq": Provider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        env_var="GROQ_API_KEY",
        signup="https://console.groq.com/keys",
    ),
    "nvidia": Provider(
        name="nvidia",
        base_url="https://integrate.api.nvidia.com/v1",
        env_var="NVIDIA_API_KEY",
        signup="https://build.nvidia.com",
    ),
}

#: Hosted free tiers are rate limited by requests AND tokens per minute, and
#: 429 is the normal steady state at experiment volume rather than an error.
#: docs/WORK.md lists "429 responses retry with exponential backoff" as a P2
#: definition-of-done item; this is it.
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0

#: Connect fast, read patiently: a queued free-tier request can sit a while,
#: but a provider that is simply unreachable should not hold up the node.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)

#: Free tier. Reported as genuinely zero rather than left unset -- the cost
#: column in measurement 4 sums these, and a missing number is not a zero.
RATES_USD_PER_MTOK = (0.0, 0.0)


class OpenAICompatError(RuntimeError):
    """Base for every failure from a hosted OpenAI-compatible provider."""


class ProviderUnavailable(OpenAICompatError):
    """Unreachable, unauthenticated, out of quota, or still rate limited."""


class ProviderProtocolError(OpenAICompatError):
    """The provider answered with something that is not a completion."""


def split_model(model: str) -> tuple[Provider, str]:
    """'groq:llama-3.3-70b-versatile' -> (groq provider, model id).

    NVIDIA model ids contain a slash (`meta/llama-3.1-70b-instruct`), so the
    split is on the FIRST colon only -- splitting on every separator would
    mangle exactly the ids most likely to be used.
    """
    provider_name, _, model_id = model.partition(":")
    provider = PROVIDERS.get(provider_name.lower())
    if provider is None:
        raise OpenAICompatError(
            f"unknown provider {provider_name!r}; known: {sorted(PROVIDERS)}"
        )
    if not model_id:
        raise OpenAICompatError(
            f"{model!r} names a provider but no model -- use '{provider_name}:<model-id>'"
        )
    return provider, model_id


class OpenAICompatBackend:
    """LLMBackend over any OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        max_tokens: int = 2048,
    ) -> None:
        self.provider, self.model_name = split_model(model)
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._client = client
        self._owns_client = client is None

        key = api_key if api_key is not None else os.environ.get(self.provider.env_var, "")
        if not key:
            raise ProviderUnavailable(
                f"{self.provider.env_var} is not set. Get a free key at "
                f"{self.provider.signup} and put it in .env (gitignored) -- "
                f"never in a tracked file."
            )
        self._key = key

    @property
    def model_id(self) -> str:
        """Stamped onto every Cost, so results say which model produced them."""
        return f"{self.provider.name}:{self.model_name}"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def complete(self, prompt: Any) -> Completion:
        body = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": render_prompt(prompt)}],
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        payload = await self._post_with_retry("/chat/completions", body)
        return Completion(text=_text_of(payload), usage=_usage(payload, self.model_id))

    async def _post_with_retry(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Retry 429 and 5xx with exponential backoff; never retry a 4xx.

        A 401 does not become valid by being sent again, and neither does a
        model id the provider does not have -- retrying those just turns a
        clear failure into a slow one. 429 is different: on a free tier it is
        the expected signal to slow down, and the provider usually says how
        long to wait in `Retry-After`.
        """
        url = f"{self.provider.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._key}"}
        last: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._get_client().post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                last = ProviderUnavailable(f"{self.provider.name} timed out: {exc}")
            except httpx.HTTPError as exc:
                last = ProviderUnavailable(f"{self.provider.name} unreachable: {exc}")
            else:
                if response.status_code == httpx.codes.OK:
                    return _parse_json(response, self.provider.name)

                if response.status_code == httpx.codes.UNAUTHORIZED:
                    raise ProviderUnavailable(
                        f"{self.provider.name} rejected the key in "
                        f"{self.provider.env_var} (401). If it was ever pasted "
                        f"anywhere shared, rotate it at {self.provider.signup}."
                    )
                if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
                    last = ProviderUnavailable(f"{self.provider.name} rate limited (429)")
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
                if response.status_code < httpx.codes.INTERNAL_SERVER_ERROR:
                    raise ProviderUnavailable(
                        f"{self.provider.name} refused: {response.status_code} "
                        f"{_snippet(response.text)}"
                    )
                last = ProviderUnavailable(
                    f"{self.provider.name} server error {response.status_code}"
                )

            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(min(BACKOFF_BASE_SECONDS * 2**attempt, MAX_BACKOFF_SECONDS))

        raise ProviderUnavailable(f"{self.provider.name} failed after {MAX_ATTEMPTS}: {last}")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honour Retry-After when the provider sends one; back off otherwise.

    Guessing when the provider has told you exactly how long to wait is how a
    free tier turns into a ban.
    """
    header = response.headers.get("retry-after", "")
    try:
        return min(float(header), MAX_BACKOFF_SECONDS)
    except ValueError:
        return min(BACKOFF_BASE_SECONDS * 2**attempt, MAX_BACKOFF_SECONDS)


def _parse_json(response: httpx.Response, provider: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderProtocolError(
            f"{provider} returned non-JSON: {_snippet(response.text)}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderProtocolError(f"{provider} returned {type(payload).__name__}, not an object")
    return payload


def _text_of(payload: dict[str, Any]) -> str:
    """Validated at the boundary: a hosted provider's JSON is untrusted input.

    A truncated or reshaped response must fail here with something readable,
    not three frames deep in the agent loop as an AttributeError on None.
    """
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderProtocolError(f"no choices in response: {list(payload)}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ProviderProtocolError("first choice has no message object")
    content = message.get("content")
    if content is None:
        raise ProviderProtocolError("message has no content field")
    return str(content)


def _usage(payload: dict[str, Any], model_id: str) -> Usage:
    """Map OpenAI usage onto ours.

    `cached_tokens` is optional and most free tiers never send it, so the
    default is 0 -- reported honestly rather than inferred. A guessed cache
    number would corrupt measurement 4, which exists to measure exactly that.
    """
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    return Usage(
        input=_count(usage, "prompt_tokens"),
        output=_count(usage, "completion_tokens"),
        cache_read=_count(details, "cached_tokens"),
        model=model_id,
    )


def _count(source: dict[str, Any], field: str) -> int:
    value = source.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        if value in (None, 0):
            return 0
        raise ProviderProtocolError(f"{field} is {value!r}, expected a non-negative integer")
    return value


def _snippet(text: str, limit: int = 200) -> str:
    return text[:limit].replace("\n", " ")
