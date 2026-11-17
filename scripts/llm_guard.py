"""Back off from the Anthropic API while the usage limit (or credit) is used up, and run simple calls on a Claude
subscription through the Claude Code CLI when one is installed.

One marker file (data/state/api_blocked.json) says "blocked". A guarded call raises ApiBlocked at once, without a
request, until the marker's skip_until; then one real call checks again (every RECHECK_MINUTES). A successful call
removes the marker. ApiBlocked subclasses anthropic.BadRequestError, so code that already catches the SDK's errors
behaves as before, just without the wasted call.

    import llm_guard
    client = llm_guard.guarded(anthropic.Anthropic())
    client.messages.create(...)            # or .parse / .stream / .count_tokens
    if llm_guard.blocked(): skip the run   # optional early exit for loops

Subscription backend: on a machine with the Claude Code CLI, a simple messages.create (one user message of text,
optional system prompt, optional json_schema output format, no tools/images/streaming) runs as a locked-down
`claude -p` on your Claude subscription instead of the API key. The reply comes back shaped like an API response
(content[0].text, stop_reason, usage). Anything else still goes to the API. A subscription limit raises ApiBlocked,
so jobs stop and retry later; nothing falls back to paid usage unless LLM_API_FALLBACK=1. Choose the backend with
[llm] backend = "auto" | "api" | "subscription" in config.toml, or LLM_BACKEND in the environment.

    client = llm_guard.client()            # same calls; the SDK client is built only if one needs the API
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx

import ce_config as C

MARKER = C.STATE / "api_blocked.json"
RECHECK_MINUTES = 30
_PATTERNS = ("reached your specified API usage limits", "credit balance is too low")


class ApiBlocked(anthropic.BadRequestError):
    """The API is blocked by the usage limit or credit; no request was sent."""

    def __init__(self, message: str):
        req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        super().__init__(message, response=httpx.Response(400, request=req), body=None)


def _read() -> dict | None:
    try:
        return json.loads(MARKER.read_text())
    except (OSError, ValueError):
        return None


def blocked(simple: bool = False) -> dict | None:
    """The marker while calls should be skipped, else None (also None when it is time to re-check).
    simple=True: the caller only makes calls the subscription backend takes (see subscription_ok),
    so on that backend the API limit does not stop it; a subscription limit this process already
    hit does (see _sub_stopped), else None."""
    if simple and backend() == "subscription":
        return _sub_stopped()
    return _api_blocked()


def _api_blocked() -> dict | None:
    m = _read()
    if m and time.time() < m.get("skip_until", 0):
        return m
    return None


def is_limit_error(exc: BaseException) -> bool:
    text = str(exc)
    return any(p in text for p in _PATTERNS)


def note(exc: BaseException) -> bool:
    """Record a usage-limit/credit error. Returns True if it was one."""
    if not is_limit_error(exc):
        return False
    text = str(exc)
    regain = ""
    m = re.search(r"regain access on (\d{4}-\d{2}-\d{2}) at (\d{2}:\d{2}) UTC", text)
    if m:
        regain = f"{m.group(1)}T{m.group(2)}:00+00:00"
    prev = _read() or {}
    data = {"since": prev.get("since") or time.time(), "last_error": time.time(), "regain": regain or prev.get("regain", ""),
            "skip_until": time.time() + RECHECK_MINUTES * 60, "message": text[:300]}
    try:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        tmp = MARKER.with_name(MARKER.stem + ".tmp.json")  # *.json: git-ignored like the marker
        tmp.write_text(json.dumps(data))
        os.replace(tmp, MARKER)
    except OSError:
        pass
    return True


def clear() -> None:
    try:
        MARKER.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _blocked_error(m: dict) -> ApiBlocked:
    until = time.strftime("%H:%M", time.localtime(m.get("skip_until", time.time())))
    return ApiBlocked(f"You have reached your specified API usage limits (skipped without a request; "
                      f"next check {until}). {m.get('message', '')[:160]}")


def call(fn, *args, **kwargs):
    """Run one API call under the guard."""
    m = _api_blocked()
    if m:
        raise _blocked_error(m)
    try:
        result = fn(*args, **kwargs)
    except anthropic.APIStatusError as exc:
        if note(exc):
            raise _blocked_error(_read() or {}) from exc
        raise
    if MARKER.exists():
        clear()
    if getattr(result, "usage", None) is not None and getattr(result, "model", None):
        log_usage("api", result.model, result.usage)
    return result


def _route(kw: dict, real: bool, api_create):
    """One messages.create given as keyword arguments. A simple call bound for the real API runs on
    the subscription on a machine with the CLI; anything else, and a subscription stop when
    LLM_API_FALLBACK=1, goes to the guarded API. api_create() returns the SDK's create, so a lazy
    client is only built when the API is really used."""
    if real and backend() == "subscription" and subscription_ok(kw):
        try:
            return subscription_create(kw)
        except ApiBlocked:
            if os.environ.get("LLM_API_FALLBACK") != "1":
                raise
    return call(api_create(), **kw)


class _Messages:
    def __init__(self, messages):
        self._m = messages
        # Only a call bound for the real API moves to the subscription; a test's fake client keeps its fake.
        self._real = type(messages).__module__.startswith("anthropic.")

    def create(self, *a, **kw):
        if a:
            return call(self._m.create, *a, **kw)
        return _route(kw, self._real, lambda: self._m.create)

    def parse(self, *a, **kw):
        return call(self._m.parse, *a, **kw)

    def count_tokens(self, *a, **kw):
        return call(self._m.count_tokens, *a, **kw)

    def stream(self, *a, **kw):
        m = _api_blocked()
        if m:
            raise _blocked_error(m)
        return self._m.stream(*a, **kw)  # errors surface inside the context manager; callers note() them

    def __getattr__(self, name):
        return getattr(self._m, name)


class _Guarded:
    """A thin proxy over an anthropic client: .messages (and .beta.messages) calls are guarded."""

    def __init__(self, client):
        self._client = client
        self.messages = _Messages(client.messages)

    def __getattr__(self, name):
        if name == "beta":
            beta = self._client.beta
            proxy = type("_Beta", (), {})()
            proxy.messages = _Messages(beta.messages)
            proxy.__dict__.update({k: getattr(beta, k) for k in ("models",) if hasattr(beta, k)})
            return proxy
        return getattr(self._client, name)


def guarded(client):
    return _Guarded(client)


def _sdk_is_real() -> bool:
    # A test that patches anthropic.Anthropic with a fake keeps its fake (as _Messages._real does).
    return getattr(anthropic.Anthropic, "__module__", "").startswith("anthropic")


class _LazyMessages:
    def __init__(self, owner, beta: bool):
        self._owner, self._beta = owner, beta

    def _sdk(self):
        sdk = self._owner._build()
        return sdk.beta.messages if self._beta else sdk.messages

    def create(self, *a, **kw):
        if a:
            return call(self._sdk().create, *a, **kw)
        return _route(kw, _sdk_is_real(), lambda: self._sdk().create)

    def __getattr__(self, name):  # parse / stream / count_tokens: always the guarded API
        return getattr(_Messages(self._sdk()), name)


class _Lazy:
    """client(): .messages (and .beta.messages) like guarded(anthropic.Anthropic(**kwargs)), but the SDK
    client is constructed only when a call goes to the API."""

    def __init__(self, kwargs: dict):
        self._kwargs, self._sdk = kwargs, None
        self.messages = _LazyMessages(self, beta=False)

    def _build(self):
        if self._sdk is None:
            self._sdk = anthropic.Anthropic(**self._kwargs)
        return self._sdk

    @property
    def beta(self):
        proxy = type("_Beta", (), {})()
        proxy.messages = _LazyMessages(self, beta=True)
        return proxy

    def __getattr__(self, name):
        return getattr(self._build(), name)


def client(**kwargs):
    """A guarded Anthropic client (see the module docstring): a simple call on
    the subscription backend never builds anthropic.Anthropic(**kwargs), so it needs no API key."""
    return _Lazy(kwargs)


# ---- subscription backend -------------------------------------------------------

CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or C.claude_bin()
SUBSCRIPTION_TIMEOUT_S = 900
# Stop-and-retry-later errors: the subscription's limit, or the CLI itself unusable (too old for the
# model, logged out). Neither is the input's fault, so jobs must not mark their item as failed.
_SUB_LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|quota|try again later|overloaded|"
                           r"does not support this model|claude update|not logged in|/login|"
                           r"authenticat|invalid api key|oauth", re.I)
# One stop per process (see the module docstring), per CLI path so a test's fake CLI never sees another's.
SUB_RECHECK_MINUTES = 30
_SUB_STOPS: dict = {}


def _sub_stopped() -> dict | None:
    """The subscription stop this process hit (skip_until, message), while it lasts, else None."""
    stop = _SUB_STOPS.get(CLAUDE_BIN)
    if stop and time.time() < stop["skip_until"]:
        return stop
    return None


def _sub_blocked(message: str) -> ApiBlocked:
    """Remember the stop for this process and return the ApiBlocked to raise."""
    _SUB_STOPS[CLAUDE_BIN] = {"skip_until": time.time() + SUB_RECHECK_MINUTES * 60, "message": message[:300]}
    return ApiBlocked(message)


def backend() -> str:
    forced = (os.environ.get("LLM_BACKEND") or C.get("llm", "backend") or "").lower()
    if forced in ("api", "subscription"):
        return forced
    return "subscription" if os.path.exists(CLAUDE_BIN) else "api"  # a machine with Claude Code uses it


def newest(model: str | None) -> str | None:
    return model  # the model named in the call, as configured


def _text(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and all(
            isinstance(b, dict) and b.get("type") == "text" for b in content):
        return "\n\n".join(b.get("text", "") for b in content)
    return None


def subscription_ok(kw: dict) -> bool:
    """Only what `claude -p` reproduces exactly: one user text message, text system prompt."""
    if kw.get("tools") or kw.get("tool_choice") or kw.get("stream"):
        return False
    msgs = kw.get("messages") or []
    if len(msgs) != 1 or msgs[0].get("role") != "user" or _text(msgs[0].get("content")) is None:
        return False
    return kw.get("system") is None or _text(kw.get("system")) is not None


def subscription_create(kw: dict):
    """One messages.create as a locked-down `claude -p` (no tools, MCP, settings, hooks or saved
    session, empty working dir). Returns an object shaped like an API Message. After a stop in this
    process (limit, unusable CLI, timeout) it raises ApiBlocked without starting the CLI."""
    stop = _sub_stopped()
    if stop:
        raise ApiBlocked(f"{stop['message']} (skipped without a call: this run already hit it)")
    model = newest(kw.get("model"))
    oc = kw.get("output_config") or {}
    fmt = oc.get("format") or {}
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
           "--setting-sources", "", "--no-session-persistence", "--disable-slash-commands"]
    if model:
        cmd += ["--model", model]
    if oc.get("effort"):
        cmd += ["--effort", oc["effort"]]
    system = _text(kw.get("system")) if kw.get("system") is not None else None
    if system:
        cmd += ["--system-prompt", system]
    if fmt.get("type") == "json_schema":
        cmd += ["--json-schema", json.dumps(fmt["schema"])]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # subscription, not API billing
    with tempfile.TemporaryDirectory(prefix="llm_sub_") as cwd:
        try:
            proc = subprocess.run(cmd, input=_text(kw["messages"][0]["content"]), cwd=cwd, env=env,
                                  capture_output=True, text=True, timeout=SUBSCRIPTION_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            log_usage("subscription", model, {}, ok=False, note="timeout")
            raise _sub_blocked(f"Claude subscription call timed out after {SUBSCRIPTION_TIMEOUT_S}s") from e
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        detail = (proc.stderr or proc.stdout or "")[-300:]
        log_usage("subscription", model, {}, ok=False, note=detail[-120:])
        if _SUB_LIMIT_RE.search(detail):
            raise _sub_blocked(f"Claude subscription unavailable: {detail}")
        raise RuntimeError(f"claude -p exit {proc.returncode}: {detail!r}")
    log_cli_usage(out, model)
    if out.get("is_error") or out.get("subtype") != "success":
        detail = str(out.get("result") or out.get("subtype"))[:300]
        if _SUB_LIMIT_RE.search(detail):
            raise _sub_blocked(f"Claude subscription unavailable: {detail}")
        raise RuntimeError(f"claude -p {out.get('subtype')}: {detail!r}")
    structured = out.get("structured_output")
    text = json.dumps(structured, ensure_ascii=False) if fmt and structured is not None else (out.get("result") or "")
    u = out.get("usage") or {}
    usage = SimpleNamespace(input_tokens=int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                            + int(u.get("cache_creation_input_tokens") or 0),
                            output_tokens=int(u.get("output_tokens") or 0))
    return SimpleNamespace(id=out.get("session_id"), type="message", role="assistant", model=model,
                           content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                           stop_details=None, usage=usage, backend="subscription")


# ---- usage log ----
# One line per AI call through this module, on every machine: which job, which backend, which
# model, tokens, and for subscription calls the API-equivalent price the CLI reports.
# No prompt or reply text is stored. One file per machine, so two machines never count each other's calls.
USAGE_LOG = C.STATE / f"ai_usage.{socket.gethostname().split('.')[0]}.jsonl"


def _u(usage, key: str) -> int:
    v = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
    return int(v or 0)


def log_usage(backend: str, model: str | None, usage, cost_usd: float | None = None,
              ms: int | None = None, ok: bool = True, note: str = "") -> None:
    rec = {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "host": socket.gethostname().split(".")[0],
           "job": os.environ.get("CE_JOB") or Path(sys.argv[0] or "?").stem,
           "backend": backend, "model": model, "in": _u(usage, "input_tokens"),
           "cache_read": _u(usage, "cache_read_input_tokens"), "cache_write": _u(usage, "cache_creation_input_tokens"),
           "out": _u(usage, "output_tokens"), "ok": ok}
    if cost_usd is not None:
        rec["cost_usd"] = round(float(cost_usd), 5)
    if ms is not None:
        rec["ms"] = int(ms)
    if note:
        rec["note"] = note[:160]
    if getattr(sys.modules.get("__main__"), "__package__", None) == "unittest":
        return  # `python -m unittest`: fakes, not real usage
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE_LOG, "a") as f:  # one short line per write: appends do not interleave
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass  # the log is a report, never a reason to fail a job


def log_cli_usage(out: dict, model: str | None) -> None:
    """A `claude -p --output-format json` result."""
    ok = not out.get("is_error") and out.get("subtype") == "success"
    log_usage("subscription", model, out.get("usage") or {}, cost_usd=out.get("total_cost_usd"),
              ms=out.get("duration_ms"), ok=ok, note="" if ok else str(out.get("result") or out.get("subtype"))[:120])
