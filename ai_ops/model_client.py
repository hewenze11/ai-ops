"""Credential-isolated OpenAI-compatible transport. No arbitrary destinations."""
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


class ModelFailure(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelFailure("MODEL_REDIRECT_REJECTED")


class OpenAICompatible:
    def __init__(self, base_url, key_file, timeout=60):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Model base URL must be a trusted HTTPS endpoint without embedded credentials")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.key_file = Path(key_file)
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def complete(self, request_body):
        # Secret is loaded only at the HTTP boundary. Not a model argument,
        # database field, log value, CLI argument, or ordinary execution env var.
        key = self.key_file.read_text().strip()
        if not key:
            raise ModelFailure("MODEL_CREDENTIAL_EMPTY")
        request = urllib.request.Request(self.url, method="POST", data=json.dumps(request_body).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "AI-Ops-Integration"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ModelFailure("MODEL_RESPONSE_TOO_LARGE")
            data = json.loads(raw)
        except urllib.error.HTTPError as e:
            raise ModelFailure("MODEL_HTTP_" + str(e.code)) from None
        except ModelFailure:
            raise
        except Exception:
            raise ModelFailure("MODEL_TRANSPORT_OR_JSON_ERROR") from None
        return normalize_response(data)


def normalize_response(data):
    try:
        choice = data["choices"][0]
        message = choice["message"]
        if message.get("role") != "assistant":
            raise ValueError("Wrong role")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("Non-text response")
        normalized = {"role": "assistant", "content": content}
        calls = message.get("tool_calls") or []
        if len(calls) > 1:
            raise ModelFailure("PARALLEL_TOOL_CALLS_NOT_SUPPORTED")
        if calls:
            call = calls[0]
            if call.get("type") != "function" or not isinstance(call.get("id"), str) or not call["id"] or len(call["id"]) > 200:
                raise ValueError("Invalid tool identity")
            function = call["function"]
            if not isinstance(function.get("name"), str) or not isinstance(function.get("arguments"), str):
                raise ValueError("Invalid function")
            normalized["tool_calls"] = [{"id": call["id"], "type": "function", "function": {"name": function["name"], "arguments": function["arguments"]}}]
        elif not content:
            raise ModelFailure("MODEL_EMPTY_RESPONSE")
        if choice.get("finish_reason") in ("length", "content_filter"):
            raise ModelFailure("MODEL_INCOMPLETE_RESPONSE")
        usage = {k: v for k, v in (data.get("usage") or {}).items() if k in ("prompt_tokens", "completion_tokens", "total_tokens") and isinstance(v, int)}
        # Do not request, expose, or fabricate provider hidden reasoning traces.
        return {"message": normalized, "usage": usage, "model": str(data.get("model", ""))}
    except ModelFailure:
        raise
    except Exception:
        raise ModelFailure("MODEL_INVALID_RESPONSE") from None
