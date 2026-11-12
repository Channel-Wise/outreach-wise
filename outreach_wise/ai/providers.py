"""Model providers. Both return JSON that matches a given schema.

- AnthropicProvider: the Claude API with the user's own API key (`pip install outreach-wise[ai]`).
- ClaudeCodeProvider: the `claude` CLI on this machine in headless mode (local development only;
  it uses whatever account that CLI is signed in to).
"""

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

# $ per million tokens (input, output). Cache writes cost 1.25x input, cache reads 0.1x.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
MODELS = list(PRICES)
EFFORTS = ("low", "medium", "high")
# Server-side refusal fallback (routes a declined request to another model in the same call).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
TIMEOUT_SECONDS = 240


class ProviderError(Exception):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Result:
    data: dict
    model: str
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0


def cost(model: str, usage: dict) -> float:
    price_in, price_out = PRICES.get(model, PRICES["claude-opus-5-5"])
    tokens_in = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0) * 1.25
        + usage.get("cache_read_input_tokens", 0) * 0.1
    )
    return round((tokens_in * price_in + usage.get("output_tokens", 0) * price_out) / 1_000_000, 6)


def anthropic_installed() -> bool:
    try:
        import anthropic  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def claude_cli() -> str | None:
    return shutil.which("claude")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, effort: str = "medium", client=None):
        if not api_key and client is None:
            raise ProviderError("Add an Anthropic API key in Settings → AI personalization.")
        self.api_key, self.model, self.effort, self._client = api_key, model, effort, client

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic  # noqa: PLC0415
            except ImportError:
                raise ProviderError(
                    "The Anthropic SDK isn't installed. Install with: pip install 'outreach-wise[ai]'"
                ) from None
            self._client = anthropic.Anthropic(api_key=self.api_key, max_retries=2, timeout=TIMEOUT_SECONDS)
        return self._client

    def generate(self, system: str, user: str | list, schema: dict, max_tokens: int = 16000) -> Result:
        import anthropic  # noqa: PLC0415

        client = self._get_client()
        extra = {}
        if self.model in FALLBACK_MODELS:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                # The playbook instructions are the same for every lead: cache them.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                **extra,
            )
        except anthropic.AuthenticationError:
            raise ProviderError("The Anthropic API key was rejected.") from None
        except anthropic.PermissionDeniedError as err:
            raise ProviderError(f"Anthropic API: permission denied ({err.message})") from None
        except anthropic.BadRequestError as err:
            raise ProviderError(f"Anthropic API rejected the request: {err.message}") from None
        except anthropic.RateLimitError:
            raise ProviderError("Anthropic API rate limit; will retry later.", retryable=True) from None
        except anthropic.APIStatusError as err:
            raise ProviderError(
                f"Anthropic API error {err.status_code}", retryable=err.status_code >= 500
            ) from None
        except anthropic.APIConnectionError:
            raise ProviderError("Anthropic API unreachable; will retry later.", retryable=True) from None

        usage = {
            key: getattr(response.usage, key, 0) or 0
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        }
        model = getattr(response, "model", self.model) or self.model
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ProviderError(f"The model declined this lead ({category or 'refusal'}).")
        if response.stop_reason == "max_tokens":
            raise ProviderError("The answer was cut off (max_tokens).", retryable=True)
        text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
        try:
            data = json.loads(text)
        except ValueError:
            raise ProviderError("The model didn't return valid JSON.", retryable=True) from None
        return Result(data=data, model=model, usage=usage, cost_usd=cost(model, usage))


class ClaudeCodeProvider:
    name = "claude_code"

    def __init__(self, model: str, effort: str = "medium", binary: str | None = None):
        self.binary = binary or claude_cli()
        if not self.binary:
            raise ProviderError("The `claude` command isn't installed on this machine.")
        self.model, self.effort = model, effort

    def generate(self, system: str, user: str | list, schema: dict, max_tokens: int = 16000) -> Result:
        if not isinstance(user, str):
            raise ProviderError("Claude Code accepts text only here; paste the guide as text.")
        with tempfile.TemporaryDirectory(prefix="outreach-wise-") as tmp:
            command = [
                self.binary,
                "-p",
                "--output-format", "json",
                "--json-schema", json.dumps(schema),
                "--system-prompt", system,
                "--model", self.model,
                "--effort", self.effort,
                "--tools", "",
                "--no-session-persistence",
                "--strict-mcp-config",
            ]  # fmt: skip
            try:
                run = subprocess.run(
                    command, input=user, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=tmp
                )
            except subprocess.TimeoutExpired:
                raise ProviderError("Claude Code timed out.", retryable=True) from None
            except OSError as err:
                raise ProviderError(f"Couldn't run Claude Code: {err}") from None
        try:
            output = json.loads(run.stdout)
        except ValueError:
            message = (run.stderr or run.stdout).strip()[:300]
            raise ProviderError(f"Claude Code failed: {message or f'exit code {run.returncode}'}") from None
        if output.get("is_error"):
            raise ProviderError(f"Claude Code: {str(output.get('result', 'error'))[:300]}")
        data = output.get("structured_output")
        if data is None:
            try:
                data = json.loads(output.get("result", ""))
            except ValueError:
                raise ProviderError("Claude Code didn't return valid JSON.", retryable=True) from None
        if not isinstance(data, dict):
            raise ProviderError("Claude Code returned an unexpected answer.", retryable=True)
        usage = output.get("usage") or {}
        usage = {
            k: usage.get(k, 0)
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        }
        return Result(
            data=data, model=self.model, usage=usage, cost_usd=float(output.get("total_cost_usd") or 0)
        )


def for_settings(conn, cfg, settings: dict):
    """The provider configured in Settings. Raises ProviderError when it can't be used."""
    from . import keys  # noqa: PLC0415

    model = settings["ai_model"] if settings["ai_model"] in PRICES else MODELS[0]
    effort = settings["ai_effort"] if settings["ai_effort"] in EFFORTS else "medium"
    if settings["ai_provider"] == "claude_code":
        return ClaudeCodeProvider(model, effort)
    return AnthropicProvider(keys.get(conn, cfg, "ai_api_key"), model, effort)
