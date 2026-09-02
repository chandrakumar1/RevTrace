"""The hypothesis agent: one stateless call, no tools, validated on the way out.

The model is asked exactly one question — *which cell looks different from the
population, and in which direction* — and its answer is constrained to
`HypothesisProposal`. It is then checked against the real population before
anything downstream believes it.

Provider-neutral
----------------
`HypothesisProvider` is the whole interface, and one `OpenAICompatibleProvider`
serves every provider we use: OpenRouter and Featherless speak the same
OpenAI-shaped API, so only `base_url`, the model, and *how the schema is
enforced* differ.

    OpenRouter   free   nvidia/nemotron-3-super-120b-a12b:free   SchemaMode.JSON_SCHEMA
    Featherless  paid   (model chosen per config)                SchemaMode.PROMPT_ONLY

Featherless does not document `response_format`, so its path asks for JSON in
the prompt and relies on the same deterministic validation to fail closed.
**Neither path declares tools.** Not a filtered tool list, not an empty one —
no `tools` or `tool_choice` key is ever constructed, so the model has no
filesystem, shell, database, web or execution capability on any provider.

Free-only, and why it is a chain property rather than a policy
--------------------------------------------------------------
RevTrace calls no paid model. The dangerous shape is not a paid provider
existing — it is a *fallback* into one: a 429 on OpenRouter's free tier is
exactly the retryable condition `FallbackProvider` was built to survive, so a
chain of `[free, paid]` converts a rate limit into a bill without anyone
deciding to spend.

`free_only=True` closes that at construction rather than at call time, so a
chain that could bill cannot be built, let alone invoked. Freeness is an
**explicit declaration** on `ProviderConfig`, defaulting to False: a provider
that says nothing is treated as billable, which is the safe direction. The
`:free` suffix corroborates that declaration for OpenRouter and is checked, but
it is never the source of truth — it is a naming convention of one provider,
and a future free provider that does not use it would be declared free and
would need this check revisited rather than silently trusted.

`free_only_chain` is the only constructor here that builds a live chain, and it
constructs OpenRouter alone. Featherless is never instantiated in it.

What this agent cannot do, structurally rather than by policy
-------------------------------------------------------------
**Stateless.** Every call builds its own message list from the request. No
conversation is retained, so one proposal cannot contaminate the next.

**It sees aggregates only.** The payload is `HypothesisRequest.as_prompt_payload`
and nothing is added here. `CellStat` has no field for a `truth_*` column, a
`risk_id`, a customer, an order, an amount, or the intervention catalogue, so
none can leak by accident.

**It cannot reach the reporter.** `app/reporting/evaluation.py` is the sole
permitted reader of ground truth, and the Phase 3 import guard rejects any
`app/` module importing a module whose name contains `evaluation`. This module
therefore *cannot* import it even if someone tried; its evidence comes from
`app/causal` aggregates assembled by the caller.

Determinism, stated honestly
----------------------------
The model is **not** deterministic. Identical inputs may still yield different
proposals. What this module guarantees is narrower and is what the falsification
loop actually needs: **given a recorded response, everything downstream is
deterministic** — validation, identity minting, and evaluation.
`RecordedProposals` exists so tests exercise exactly that path with no network.

HTTP note: the SDK runs on `httpx2`, a different distribution from the `httpx`
FastAPI's TestClient uses. Their types are not interchangeable — never hand an
`httpx.Timeout` to this client. Timeouts here are plain floats, which both
accept, so the mistake is not available.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pydantic import SecretStr, ValidationError

from app.agents.contracts import (
    Claim,
    HypothesisError,
    HypothesisProposal,
    HypothesisRequest,
    ProviderInfo,
    ValidatedHypothesis,
)
from app.core.config import Settings

#: The only provider RevTrace calls. Free tier.
#:
#: This was `openai/gpt-oss-120b:free` until OpenRouter withdrew the whole
#: `gpt-oss` family from its free tier; a live request then returned HTTP 404
#: naming the paid slug as the replacement. Recorded because it is the concrete
#: case the free-only guard exists for: the provider offered a paid substitute
#: and nothing in the chain was able to accept it.
#:
#: Any replacement must be zero-priced in OpenRouter's catalog *and* advertise
#: `structured_outputs`, since the request path depends on strict schema
#: enforcement rather than on asking politely.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

#: Configured but **never** placed in a production chain: the same weights on a
#: paid endpoint. Kept so the free-only guard has something real to refuse and
#: so the shape of a paid config is documented rather than imagined.
FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"
FEATHERLESS_MODEL = "openai/gpt-oss-120b"

#: OpenRouter's naming convention for a zero-cost model variant. Used **only**
#: to corroborate an explicit `is_free=True`, never to infer one: a model
#: without it is not thereby paid, and a model with it is not thereby free.
FREE_MODEL_SUFFIX = ":free"

#: Small: the answer is five short fields. Well under any HTTP timeout, so the
#: non-streaming path is correct here.
MAX_TOKENS = 2_048

#: Seconds. A plain float, accepted by the SDK's own transport — deliberately
#: not an `httpx.Timeout`, which belongs to a different distribution.
TIMEOUT_SECONDS = 60.0

#: The name the JSON schema is registered under, for providers that echo it.
SCHEMA_NAME = "hypothesis_proposal"

#: Namespace for deriving a hypothesis id from its content. Keeps a replayed
#: response producing a replayed identity.
HYPOTHESIS_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "revtrace.hypothesis")

SYSTEM_PROMPT = """\
You are reading aggregate statistics from a randomised experiment about payment \
recovery. Each row is a cell — a group of units sharing observable payment \
features — with arm sizes, recovery counts, an uplift point estimate in integer \
basis points, and a 95% confidence interval.

Propose exactly one testable hypothesis about heterogeneity: name one cell that \
you believe differs from the population effect, and say in which direction.

Hard constraints:
- `cell_key` MUST be one of `allowed_cell_keys` exactly. Do not construct, \
combine, abbreviate or invent a key.
- `claim` MUST be one of `allowed_claims`.
- Cite only cell keys that appear in the data you were given.
- Do not propose a threshold, a monetary value, an action, an intervention, a \
policy change, or a feature that is not already present in the data.
- Your rationale must refer to the counts and intervals shown. Do not speculate \
about causes you cannot see in the data.

You are generating a hypothesis for a deterministic evaluator to test. You are \
not deciding anything, and nothing you propose will be executed.\
"""

#: Appended for providers with no schema-enforcement mechanism. The contract is
#: identical; only the enforcement differs, so the same validation catches the
#: same mistakes either way.
JSON_ONLY_INSTRUCTION = """\

Reply with a single JSON object and nothing else. No prose before or after it, \
no markdown fence. It must have exactly these five keys:

  "cell_key":       string, one of allowed_cell_keys
  "ladder_level":   string, "fine" or "coarse", matching that cell
  "claim":          string, one of allowed_claims
  "rationale":      string, one or two sentences
  "evidence_cited": array of strings, each one of allowed_cell_keys

Do not add any other key.\
"""


class SchemaMode(StrEnum):
    """How a provider is made to return the right shape.

    Deliberately not a capability flag: it records what the request actually
    does, so an audit entry can say how the output was constrained rather than
    assuming.
    """

    #: `response_format: {type: json_schema, strict: true}`. The provider
    #: enforces the schema; a malformed answer is impossible rather than caught.
    JSON_SCHEMA = "json_schema"
    #: The prompt asks for JSON and the deterministic validation catches
    #: anything else. Weaker enforcement, identical contract.
    PROMPT_ONLY = "prompt_only"


class ProviderError(RuntimeError):
    """A provider failed in a way that may be worth retrying elsewhere."""


def strict_schema(model: type[HypothesisProposal] = HypothesisProposal) -> dict[str, object]:
    """`HypothesisProposal`'s JSON Schema, made valid for strict mode.

    Pydantic omits a field with a default from `required`, but an OpenAI-style
    strict schema requires **every** property to be listed. `evidence_cited` has
    a `default_factory`, so the schema as emitted would either be rejected or —
    worse — silently accepted with strict enforcement dropped.

    This returns a *copy* with every property required. `HypothesisProposal`
    itself is untouched, and `validate()` still accepts an empty list, so
    requiring the model to emit `[]` costs nothing.
    """
    schema: dict[str, object] = json.loads(json.dumps(model.model_json_schema()))
    properties = schema.get("properties", {})
    schema["required"] = list(properties if isinstance(properties, dict) else {})
    schema["additionalProperties"] = False
    return schema


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Everything one provider needs. Runtime configuration, never schema."""

    name: str
    base_url: str
    model: str
    api_key: SecretStr
    schema_mode: SchemaMode
    extra_body: Mapping[str, object] = field(default_factory=dict)
    #: Whether calling this provider costs money. **Declared, never inferred**,
    #: and False by default so a config that forgets to say is treated as
    #: billable. Nothing derives it from the provider's name or a price list.
    is_free: bool = False

    def __post_init__(self) -> None:
        """Corroborate a free declaration against OpenRouter's naming convention.

        A config that claims to be free while naming a model without the `:free`
        suffix is far more likely to be a typo — a model string edited without
        the flag being revisited — than a genuinely free model under a different
        convention. Refusing loudly is the right failure: the alternative is a
        chain that believes it is free and is not.

        Deliberately one-directional. A config with `is_free=False` is never
        promoted to free because its model happens to end in `:free`; the
        declaration remains the authority in the direction that costs money.
        """
        if self.is_free and not self.model.endswith(FREE_MODEL_SUFFIX):
            raise HypothesisError(
                f"{self.name}: model {self.model!r} is declared free but does not end "
                f"in {FREE_MODEL_SUFFIX!r}. Either the model string is wrong or this "
                f"provider is not on a free tier; the declaration is not evidence."
            )

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            provider=self.name, model=self.model, schema_mode=self.schema_mode.value
        )


def openrouter_config(settings: Settings) -> ProviderConfig:
    """The free provider. Strict JSON schema, routing pinned to backends that honour it.

    `require_parameters` matters more than it looks: without it OpenRouter may
    route to a backend that ignores `response_format`, and an unenforced schema
    that looks enforced is worse than no schema at all.
    """
    return ProviderConfig(
        name="openrouter",
        base_url=OPENROUTER_BASE_URL,
        model=OPENROUTER_MODEL,
        api_key=settings.openrouter_api_key,
        schema_mode=SchemaMode.JSON_SCHEMA,
        extra_body={"provider": {"require_parameters": True}},
        is_free=True,
    )


def featherless_config(settings: Settings) -> ProviderConfig:
    """A **paid** provider. Never placed in a production chain.

    `is_free=False` is written out rather than left to the default: this is the
    config the free-only guard exists to refuse, and a reader should not have to
    check a dataclass default to know that calling it costs money.

    Featherless documents no `response_format`, so the prompt asks for JSON.
    """
    return ProviderConfig(
        name="featherless",
        base_url=FEATHERLESS_BASE_URL,
        model=FEATHERLESS_MODEL,
        api_key=settings.featherless_api_key,
        schema_mode=SchemaMode.PROMPT_ONLY,
        is_free=False,
    )


class HypothesisProvider(Protocol):
    """Where a proposal comes from.

    A Protocol so a live provider and a recorded fixture are interchangeable,
    and so tests never open a socket.
    """

    @property
    def info(self) -> ProviderInfo:
        """Which provider this is, for the audit trail."""
        ...

    def propose(self, request: HypothesisRequest) -> HypothesisProposal:
        """Return one structured proposal for this request."""
        ...


#: Fields a reasoning-capable endpoint may use for its thinking channel. Their
#: *presence* is reported; their contents never are — reasoning text is model
#: output about the payload and has no place in an exception message.
REASONING_FIELDS = ("reasoning", "reasoning_content", "reasoning_details")


#: How much of a provider's own error text is worth carrying into an exception.
#: The text is OpenRouter's status message, not model output — but a moderation
#: refusal can echo part of the request, so it is bounded rather than trusted.
ERROR_MESSAGE_LIMIT = 200

#: Token shapes that must never reach a log, even inside a provider's own error
#: text. An upstream error can quote the request that caused it, and a request
#: carries an `Authorization` header — so the text is scrubbed before it is
#: reported, not trusted because of where it came from.
_SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{4,}"),  # OpenAI/OpenRouter key prefix
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),  # any long opaque run
)


def _redact(text: str) -> str:
    """Remove key-shaped tokens from provider text. Conservative by design.

    Pattern-based scrubbing cannot be complete, which is why the reported text
    is also length-bounded and why nothing derived from the request payload is
    ever included. This removes the shapes a credential actually takes; it is
    the last line, not the only one.
    """
    for pattern in _SECRET_SHAPES:
        text = pattern.sub("[redacted]", text)
    return text


def _response_metadata(response: object) -> list[str]:
    """Envelope facts common to every failure. Metadata only, never content.

    Reads defensively throughout: this runs on paths that are already failing,
    and an exception raised while explaining an exception is worse than the
    original.
    """
    parts: list[str] = []
    parts.append(f"id={getattr(response, 'id', None)!r}")
    parts.append(f"model={getattr(response, 'model', None)!r}")
    parts.append(f"object={getattr(response, 'object', None)!r}")

    choices = getattr(response, "choices", None)
    parts.append("choices=absent" if choices is None else f"choices={len(choices)}")

    usage = getattr(response, "usage", None)
    if usage is None:
        parts.append("usage=unavailable")
    else:
        parts.append(f"prompt_tokens={getattr(usage, 'prompt_tokens', None)}")
        parts.append(f"completion_tokens={getattr(usage, 'completion_tokens', None)}")
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        if reasoning_tokens is not None:
            parts.append(f"reasoning_tokens={reasoning_tokens}")

    extra = getattr(response, "model_extra", None) or {}
    if extra:
        # Names only. A value could be anything the provider chose to send.
        parts.append(f"extra_keys={sorted(extra)}")

    metadata = getattr(response, "metadata", None)
    if isinstance(metadata, dict) and metadata:
        parts.append(f"metadata_keys={sorted(metadata)}")

    error = extra.get("error")
    if isinstance(error, dict):
        parts.append(f"error_code={error.get('code')!r}")
        parts.append(f"error_type={error.get('type')!r}")
        message = error.get("message")
        if isinstance(message, str):
            scrubbed = _redact(message)
            clipped = scrubbed[:ERROR_MESSAGE_LIMIT]
            suffix = "…" if len(scrubbed) > ERROR_MESSAGE_LIMIT else ""
            parts.append(f"error_message={clipped + suffix!r}")
    elif error is not None:
        parts.append(f"error_present={type(error).__name__}")

    return parts


#: How much of a response's *shape* is worth describing when it will not parse.
SHAPE_PREFIX_LIMIT = 40

#: A markdown fence is the single most common reason structured output fails to
#: parse, and it is invisible in a decoder's "line 1 column 1" message.
CODE_FENCE = "```"

_LETTERS = re.compile(r"[A-Za-z]")
_DIGITS = re.compile(r"[0-9]")


def _shape_prefix(content: str) -> str:
    """The start of a response with every letter and digit flattened.

    `{"cell_key": "insufficient_funds|upi"` becomes `{"aaaa_aaa": "aaaaaaaaaaaa`.
    Structure — braces, quotes, colons, fences, newlines — survives; anything
    that could carry a value does not. That is exactly the difference between
    diagnosing a malformed response and logging one.

    Punctuation and whitespace are preserved deliberately: a stray fence, a
    trailing comma or a leading newline is the finding, and normalising
    whitespace would erase it.
    """
    prefix = content[:SHAPE_PREFIX_LIMIT]
    return _DIGITS.sub("0", _LETTERS.sub("a", prefix))


def _parse_failure_diagnosis(content: str, response: object, provider: str) -> str:
    """Why content that arrived could not be read as the contract.

    A `JSONDecodeError` says where it gave up, never what it was looking at,
    and the obvious fix — quote the content — is the one thing this must not
    do: content is model output about the payload, and the payload is kept out
    of logs on purpose.

    So the *shape* is reported instead. A fence, a comment, a single quote or a
    truncation each leave a distinct signature in the flattened prefix, which is
    enough to tell them apart without reproducing a single character that
    carries meaning.
    """
    stripped = content.strip()
    parts = [
        f"provider={provider!r}",
        f"id={getattr(response, 'id', None)!r}",
    ]

    choices = getattr(response, "choices", None) or []
    first = choices[0] if choices else None
    parts.append(f"finish_reason={getattr(first, 'finish_reason', None)!r}")

    usage = getattr(response, "usage", None)
    if usage is None:
        parts.append("usage=unavailable")
    else:
        parts.append(f"prompt_tokens={getattr(usage, 'prompt_tokens', None)}")
        parts.append(f"completion_tokens={getattr(usage, 'completion_tokens', None)}")
        details = getattr(usage, "completion_tokens_details", None)
        parts.append(f"reasoning_tokens={getattr(details, 'reasoning_tokens', None)}")

    parts.append(f"content_length={len(content)}")
    parts.append(f"starts_with_fence={stripped.startswith(CODE_FENCE)}")
    parts.append(f"ends_with_fence={stripped.endswith(CODE_FENCE)}")
    parts.append(f"first_non_whitespace_char={(stripped[:1] or None)!r}")
    parts.append(f"last_non_whitespace_char={(stripped[-1:] or None)!r}")
    parts.append(f"shape_prefix={_shape_prefix(content)!r}")
    return ", ".join(parts)


def _body_error_code(response: object) -> int | None:
    """The status code carried inside a 200's error body, if there is one.

    OpenRouter reports some failures as a real HTTP status and others as HTTP
    200 with `{"error": {"code": ...}}`. The condition is the same either way,
    so the code is read back out here and routed by the same rule the `except`
    clauses apply — otherwise a bad request becomes retryable purely because of
    which channel the provider happened to choose.

    Returns None when there is no error body, or when its code is not an
    integer: a code that cannot be compared is not evidence, and guessing would
    be worse than falling through to the transient default.
    """
    extra = getattr(response, "model_extra", None) or {}
    error = extra.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    # bool is an int subclass and is never a status code.
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _is_client_error(code: int | None) -> bool:
    """Whether this code means *the request was wrong*, not *the service failed*.

    Mirrors the `except openai.APIStatusError` clause exactly: 4xx other than
    429 is a bad request, and asking again — here or elsewhere — would ask the
    same bad question. 429 is excluded because a rate limit is a statement about
    timing rather than about the request.
    """
    return code is not None and 400 <= code < 500 and code != 429


def _no_choices_diagnosis(response: object) -> str:
    """Why a 200 arrived with no choices at all.

    **This is reachable on a successful HTTP response**, and not as an edge
    case. The SDK parses response bodies with `construct_type`, which is lenient
    — a body with no `choices` key at all yields `ChatCompletion(choices=None)`
    rather than a validation error. OpenRouter returns exactly that shape when
    it answers HTTP 200 with an `{"error": {...}}` body, which is how it reports
    some upstream and quota failures. So `choices=absent` and `choices=0` are
    different events, and the count above distinguishes them.
    """
    return "response carried no choices, " + ", ".join(_response_metadata(response))


def _empty_content_diagnosis(response: object) -> str:
    """Why a response arrived with no content, in one line, from the response.

    The previous message was `empty response`, which is true and useless: it
    cannot distinguish a model that refused, one that stopped early, and one
    that spent its whole token budget thinking. That distinction is the entire
    question when a call fails, and it was being discarded at the one moment it
    mattered.

    Everything here is metadata — a stop reason, token counts, a completion id,
    and whether a reasoning field was *present*. No reasoning text, no message
    content, no credential, and nothing derived from the payload.

    Deliberately total: this runs on a path that is already failing, so a
    surprise here would replace a useful error with a confusing one. Every read
    is defensive and any unavailable field is reported as unknown rather than
    raising.
    """
    parts: list[str] = ["empty content"]

    choices = getattr(response, "choices", None) or []
    # Named `first` rather than `choice`: the isolation guard bans that
    # identifier because `random.choice` is a randomness source, and a guard
    # that has to special-case a safe use is a weaker guard.
    first = choices[0] if choices else None
    parts.append(f"finish_reason={getattr(first, 'finish_reason', None)!r}")

    message = getattr(first, "message", None)
    refusal = getattr(message, "refusal", None)
    parts.append(f"refused={bool(refusal)}")

    extra = getattr(message, "model_extra", None) or {}
    present = [name for name in REASONING_FIELDS if extra.get(name)]
    parts.append(f"reasoning_present={bool(present)}")
    if present:
        # The field *name* locates the channel for a reader; the value stays out.
        parts.append(f"reasoning_field={present[0]!r}")

    parts.extend(_response_metadata(response))
    return ", ".join(parts)


class OpenAICompatibleProvider:
    """One stateless, tool-free call to any OpenAI-compatible endpoint."""

    def __init__(self, config: ProviderConfig) -> None:
        key = config.api_key.get_secret_value()
        if not key:
            raise HypothesisError(
                f"{config.name.upper()}_API_KEY is not configured. The agent will "
                "not fall back to an ambient credential: a run that calls a paid "
                "API must name its own key."
            )
        from openai import OpenAI

        self._config = config
        self._client = OpenAI(api_key=key, base_url=config.base_url, timeout=TIMEOUT_SECONDS)

    @property
    def info(self) -> ProviderInfo:
        return self._config.info

    @property
    def is_free(self) -> bool:
        """Whether calling this provider costs money. Read from the config only.

        A property rather than a field on `ProviderInfo`: this is operational
        state about *how the call is billed*, not provenance about *what
        produced the answer*, and the audit record should not have to carry a
        safety flag to keep the safety guard working.
        """
        return self._config.is_free

    def _system_prompt(self) -> str:
        if self._config.schema_mode is SchemaMode.PROMPT_ONLY:
            return SYSTEM_PROMPT + JSON_ONLY_INSTRUCTION
        return SYSTEM_PROMPT

    def _request_kwargs(self, request: HypothesisRequest) -> dict[str, object]:
        """The request body. No `tools`, no `tool_choice`, on any path."""
        kwargs: dict[str, object] = {
            "model": self._config.model,
            "max_tokens": MAX_TOKENS,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(request.as_prompt_payload(), sort_keys=True),
                },
            ],
        }
        if self._config.schema_mode is SchemaMode.JSON_SCHEMA:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": SCHEMA_NAME,
                    "strict": True,
                    "schema": strict_schema(),
                },
            }
        if self._config.extra_body:
            kwargs["extra_body"] = dict(self._config.extra_body)
        return kwargs

    def propose(self, request: HypothesisRequest) -> HypothesisProposal:
        import openai

        try:
            # `create` is overloaded and mypy cannot match a **dict against it.
            # The dict is deliberate: `_request_kwargs` is the single place the
            # request body is built, so a test can assert its exact shape —
            # including that no `tools` key exists — without a network call.
            response = self._client.chat.completions.create(  # type: ignore[call-overload]
                **self._request_kwargs(request)
            )
        except (openai.APIConnectionError, openai.RateLimitError) as exc:
            # Transport trouble or rate limiting. Another provider may succeed.
            raise ProviderError(f"{self._config.name}: {type(exc).__name__}") from exc
        except openai.APIStatusError as exc:
            status = getattr(exc, "status_code", None)
            if status is not None and status >= 500:
                raise ProviderError(f"{self._config.name}: HTTP {status}") from exc
            # 4xx other than 429 is a bad request, not an outage. Asking a
            # different provider the same bad question would not help.
            raise

        if not response.choices:
            diagnosis = f"{self._config.name}: {_no_choices_diagnosis(response)}"
            if _is_client_error(_body_error_code(response)):
                # The same rule the 4xx branch above applies. A bad request
                # reported inside a 200 is still a bad request, and treating it
                # as transient would let a schema error look like an outage and
                # fall over to a provider that would reject it identically.
                raise HypothesisError(diagnosis)
            # 429, 5xx, and an absent error body are all conditions a later
            # attempt might survive, so they stay in the retryable class.
            raise ProviderError(diagnosis)

        first = response.choices[0]
        content = first.message.content
        if not content:
            raise ProviderError(f"{self._config.name}: {_empty_content_diagnosis(response)}")
        return self._parse(content, response)

    def _parse(self, content: str, response: object = None) -> HypothesisProposal:
        """Text to contract. A failure here is a finding, never a retry signal.

        `ValidationError` and `HypothesisError` both escape deliberately: a
        provider that returned the wrong shape has told us something, and
        quietly asking someone else would hide it.
        """
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            diagnosis = _parse_failure_diagnosis(content, response, self._config.name)
            raise HypothesisError(
                f"{self._config.name} returned content that is not JSON: {exc}; {diagnosis}"
            ) from exc
        try:
            return HypothesisProposal.model_validate(payload)
        except ValidationError as exc:
            raise HypothesisError(
                f"{self._config.name} returned JSON that does not match the contract: {exc}"
            ) from exc


class FallbackProvider:
    """Ordered providers. First success wins; failures are recorded.

    Falls over **only** on transport failure, 429 and 5xx — the conditions a
    different endpoint might survive. It deliberately does not fall over on a
    schema or validation failure: a provider that invented a cell key has
    produced a finding, and retrying elsewhere would bury it.

    That retry rule is precisely what makes `free_only` necessary. A 429 is the
    free tier's normal signal, so `[free, paid]` would turn a rate limit into a
    charge automatically. `free_only=True` refuses such a chain **at
    construction**: the unsafe object is never built, so there is no runtime
    path — no exception handler, no branch, no ordering — that can reach a paid
    provider from it.
    """

    def __init__(
        self,
        providers: Sequence[HypothesisProvider],
        *,
        free_only: bool = False,
    ) -> None:
        if not providers:
            raise HypothesisError("a fallback chain needs at least one provider")
        if free_only:
            self._require_free(providers)
        self._providers = tuple(providers)
        self._free_only = free_only
        self._answered: HypothesisProvider | None = None
        self.attempts: list[str] = []

    @staticmethod
    def _require_free(providers: Sequence[HypothesisProvider]) -> None:
        """Every provider must *declare* itself free. Silence counts as paid.

        `getattr(..., False)` rather than an attribute on the Protocol: a
        provider that has never heard of billing is refused rather than
        crashing, which is the same outcome by a gentler route, and adding
        `is_free` to `HypothesisProvider` would force every stub and future
        implementation to answer a question most of them have no stake in.
        """
        billable = [
            provider.info.provider
            for provider in providers
            if not getattr(provider, "is_free", False)
        ]
        if billable:
            raise HypothesisError(
                "free-only chain refused: "
                + ", ".join(billable)
                + " did not declare is_free=True. RevTrace calls no paid model, and a "
                "provider that does not say it is free is treated as billable."
            )

    @property
    def free_only(self) -> bool:
        return self._free_only

    @property
    def is_free(self) -> bool:
        """A chain is free only if every member is, so chains nest safely.

        Derived from the members rather than from `free_only`, because the flag
        records what was *enforced* at construction while this records what is
        *true* — a chain built without the flag but out of free providers is
        still free, and one containing a paid provider is not, whatever it was
        constructed with.
        """
        return all(getattr(provider, "is_free", False) for provider in self._providers)

    @property
    def info(self) -> ProviderInfo:
        if self._answered is None:
            return self._providers[0].info
        return self._answered.info

    def propose(self, request: HypothesisRequest) -> HypothesisProposal:
        failures: list[str] = []
        for provider in self._providers:
            try:
                proposal = provider.propose(request)
            except ProviderError as exc:
                self.attempts.append(f"{provider.info.provider}: {exc}")
                failures.append(str(exc))
                continue
            self._answered = provider
            self.attempts.append(f"{provider.info.provider}: ok")
            return proposal
        raise ProviderError(f"every provider failed: {'; '.join(failures)}")


class RecordedProposals:
    """Replays proposals in order. No network, no client, no key.

    Lives in the application package rather than the test tree so that a
    deliberate offline run — a demo, a dry run, a reproduction of a recorded
    session — uses the same code path a test does.
    """

    def __init__(
        self,
        proposals: Sequence[HypothesisProposal],
        *,
        info: ProviderInfo | None = None,
    ) -> None:
        if not proposals:
            raise HypothesisError("a recorded source needs at least one proposal")
        self._proposals = tuple(proposals)
        self._index = 0
        self._info = info or ProviderInfo(
            provider="recorded", model="recorded", schema_mode="recorded"
        )

    @property
    def info(self) -> ProviderInfo:
        return self._info

    @property
    def is_free(self) -> bool:
        """Always True: a replay has no client, no key and no socket.

        Not a claim about the model that originally produced these proposals —
        a claim about *this* object, which cannot issue a request and therefore
        cannot incur a charge however it is chained.
        """
        return True

    def propose(self, request: HypothesisRequest) -> HypothesisProposal:
        if self._index >= len(self._proposals):
            raise HypothesisError(
                f"recorded source exhausted after {len(self._proposals)} proposal(s)"
            )
        proposal = self._proposals[self._index]
        self._index += 1
        return proposal


def free_only_chain(settings: Settings) -> FallbackProvider:
    """The only live provider chain RevTrace builds. OpenRouter, alone.

    A one-element chain is not a degenerate `FallbackProvider` — it is the
    point. There is nothing to fall back *to*, so a 429 on the free tier
    surfaces as a `ProviderError` the caller must handle, which is the correct
    outcome: waiting or abandoning the call, never spending.

    Featherless is not constructed here. Not filtered out of a list, not
    skipped by a flag — never instantiated, so no misreading of the guard can
    resurrect it.
    """
    provider = OpenAICompatibleProvider(openrouter_config(settings))
    return FallbackProvider([provider], free_only=True)


# -- deterministic validation ---------------------------------------------


def hypothesis_id_for(experiment_id: uuid.UUID, cell_key: str, claim: Claim) -> uuid.UUID:
    """A content-derived identity, so a replay reproduces it.

    Derived rather than drawn: `uuid4` would make an otherwise deterministic
    replay produce a different audit record every time.
    """
    return uuid.uuid5(HYPOTHESIS_NAMESPACE, f"{experiment_id}:{cell_key}:{claim.value}")


def validate(
    request: HypothesisRequest,
    proposal: HypothesisProposal,
    provenance: ProviderInfo | None = None,
) -> ValidatedHypothesis:
    """Check a proposal against the real population. Pure.

    This is the boundary where a model's output stops being text and starts
    being data the system will act on, so every field is checked against
    something the caller supplied — never against something the model asserted.
    Identical on every provider: enforcement differs, this does not.
    """
    problems: list[str] = []

    if proposal.cell_key not in request.cell_keys:
        problems.append(
            f"cell_key {proposal.cell_key!r} is not present in the population; "
            f"the model may only name a key it was shown"
        )
    else:
        cell = request.cell(proposal.cell_key)
        if proposal.ladder_level != cell.ladder_level:
            problems.append(
                f"ladder_level {proposal.ladder_level!r} contradicts the population, "
                f"where {proposal.cell_key!r} was scored at {cell.ladder_level!r}"
            )

    if not isinstance(proposal.claim, Claim):
        problems.append(f"claim {proposal.claim!r} is not in the permitted vocabulary")

    if not proposal.rationale or not proposal.rationale.strip():
        problems.append("rationale must not be blank")

    invented = sorted(set(proposal.evidence_cited) - request.cell_keys)
    if invented:
        problems.append(f"evidence cites cell keys not in the population: {invented}")

    if problems:
        raise HypothesisError(f"refusing the proposal: {'; '.join(problems)}")

    return ValidatedHypothesis(
        hypothesis_id=hypothesis_id_for(request.experiment_id, proposal.cell_key, proposal.claim),
        experiment_id=request.experiment_id,
        cell_key=proposal.cell_key,
        ladder_level=proposal.ladder_level,
        claim=proposal.claim,
        rationale=proposal.rationale.strip(),
        evidence_cited=tuple(proposal.evidence_cited),
        provenance=provenance,
    )


def propose_hypothesis(
    provider: HypothesisProvider, request: HypothesisRequest
) -> ValidatedHypothesis:
    """Ask for a proposal and refuse it unless it checks out.

    Provenance is read *after* the call, so a fallback chain reports the
    provider that actually answered rather than the one first tried.
    """
    proposal = provider.propose(request)
    return validate(request, proposal, provider.info)


__all__ = [
    "FEATHERLESS_BASE_URL",
    "FEATHERLESS_MODEL",
    "FREE_MODEL_SUFFIX",
    "HYPOTHESIS_NAMESPACE",
    "JSON_ONLY_INSTRUCTION",
    "MAX_TOKENS",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_MODEL",
    "ERROR_MESSAGE_LIMIT",
    "REASONING_FIELDS",
    "SCHEMA_NAME",
    "SHAPE_PREFIX_LIMIT",
    "SYSTEM_PROMPT",
    "TIMEOUT_SECONDS",
    "FallbackProvider",
    "HypothesisProvider",
    "OpenAICompatibleProvider",
    "ProviderConfig",
    "ProviderError",
    "RecordedProposals",
    "SchemaMode",
    "featherless_config",
    "free_only_chain",
    "hypothesis_id_for",
    "openrouter_config",
    "propose_hypothesis",
    "strict_schema",
    "validate",
]
