import logging
import os
from dotenv import load_dotenv

load_dotenv()

AI_PROVIDER = os.getenv("AI_PROVIDER", "groq")

# Best-effort token-usage capture for the monthly AI-token quota
# (services/token_quota_service.py). FastAPI/Starlette runs each request
# in its own async task on a single event loop thread and every call site
# here awaits the provider call synchronously before reading this value
# back out (see api/*.py: get_ai_response(...) followed immediately by
# record_ai_token_usage()) -- there's no `await` in between that could
# let another request's call interleave and overwrite it first. This is
# NOT safe if these functions were ever called from multiple OS threads
# concurrently for the same slot, so it's approximate metering, not
# billing-grade precision.
_LAST_USAGE_TOKENS = {"value": 0}


def get_last_usage_tokens() -> int:
    """Token count (prompt+completion) from the most recent
    get_ai_response()/get_vision_response() call. 0 if the provider's
    response didn't include usage data (e.g. DeepSeek error responses)."""
    return _LAST_USAGE_TOKENS["value"]


def set_last_usage_tokens(total: int) -> None:
    """2026-07-20 addition: lets a caller that makes SEVERAL sequential
    get_ai_response() calls for one logical "feature run" (e.g.
    services/agent_debate_service.py's run_debate(), which makes 4 calls
    -- 3 personas + 1 arbiter) overwrite this shared slot with the real
    summed total across all of them, right before returning. Without
    this, the site's usual pattern of "call get_ai_response(), then once
    record_ai_token_usage()" would only ever bill the LAST of the 4
    calls, silently dropping the other 3's real cost. Same shared-slot
    concurrency caveat as get_last_usage_tokens() applies here."""
    _LAST_USAGE_TOKENS["value"] = total


def get_ai_response(prompt: str, max_tokens: int = 1000, provider: str = None, reasoning_effort: str = None) -> str:
    """
    Universal AI router - switch provider via AI_PROVIDER env var, or pass
    `provider` explicitly to override it for one call (added 2026-07-18
    for features like services/agent_debate_service.py that deliberately
    always want DeepSeek regardless of the site's global default -- a
    multi-call feature like a debate should stay on the specifically
    confirmed-cheap provider, not silently ride whatever AI_PROVIDER
    happens to be set to).

    Supported:
        groq       → Groq (free, fast)
        deepseek   → DeepSeek V4 Flash, served via DeepInfra (cheap) --
                     needs DEEPINFRA_API_KEY, not a DeepSeek-issued key;
                     see _deepseek()'s docstring for why
        claude     → Anthropic Claude (best quality)
        openrouter → 2026-07-26 addition: one OpenAI-compatible endpoint
                     that fans out to many upstream providers/models
                     (GPT/Claude/Gemini/Llama/etc.) under a single
                     OPENROUTER_API_KEY -- see _openrouter()'s docstring.
                     Purely additive: existing groq/deepseek/claude
                     callers and the default AI_PROVIDER are untouched;
                     this is only used where a caller explicitly passes
                     provider="openrouter" or AI_PROVIDER=openrouter is
                     set in the environment.
        kimi       → 2026-07-30 addition: Moonshot AI's Kimi K2.6 (open-
                     weight, Modified MIT license), served via DeepInfra --
                     same DEEPINFRA_API_KEY already used by _deepseek(),
                     no new account/key needed. See _kimi()'s docstring.
                     Purely additive, same as openrouter above.

    Args:
        prompt: The prompt to send
        max_tokens: Max response tokens
        provider: optional override ("groq"/"deepseek"/"claude"/"kimi");
            defaults to the AI_PROVIDER env var when omitted
        reasoning_effort: "low" (default) or "high" -- Groq-only, ignored
            by every other provider. See _groq()'s 2026-07-30 docstring
            addition for why "high" also needs a bigger max_tokens from
            the caller, not just this flag.

    Returns:
        str: AI response text
    """
    provider = (provider or AI_PROVIDER).lower()
    _LAST_USAGE_TOKENS["value"] = 0

    if provider == "groq":
        return _groq(prompt, max_tokens, reasoning_effort=reasoning_effort or "low")
    elif provider == "deepseek":
        return _deepseek(prompt, max_tokens)
    elif provider == "claude":
        return _claude(prompt, max_tokens)
    elif provider == "openrouter":
        return _openrouter(prompt, max_tokens)
    elif provider == "kimi":
        return _kimi(prompt, max_tokens)
    else:
        raise ValueError(f"Unknown AI provider: {provider}. Use groq / deepseek / claude / openrouter / kimi")


def get_ai_response_with_escalation(
    prompt: str,
    max_tokens: int = 1000,
    provider: str = None,
    reasoning_effort: str = None,
    min_length: int = 15,
) -> str:
    """
    2026-07-30 addition: a "cascade" wrapper around get_ai_response() for
    the site's highest-visibility AI outputs (starting with api/chat.py's
    flagship assistant -- see that file's own 2026-07-23 comment where the
    user explicitly chose "keep the free Groq model, improve the prompt"
    over switching to the paid `claude` provider outright). This keeps
    that decision intact for the 90%+ of calls that come back fine, but
    adds a safety net for the ones that don't, instead of silently showing
    a generic error message to the user.

    Behavior:
      1. Call get_ai_response() with the given provider (Groq by default)
         exactly as before.
      2. If that raises OR returns something too short/degenerate to be a
         real answer (min_length is deliberately low -- this is only
         meant to catch outright failures like "" or a 2-character
         fragment, not to judge answer quality), and ANTHROPIC_API_KEY is
         configured, retry the SAME prompt against Claude once.
      3. If Claude isn't configured or also fails, return whatever the
         first attempt produced (even if empty) -- never raises, matching
         every other AI-facing function in this codebase's graceful-
         degradation convention. Callers keep their existing except-block
         fallback text as the final safety net, unchanged.

    This is opt-in: only call sites that explicitly switch to this
    function get the escalation behavior. get_ai_response() itself is
    completely unchanged for every other caller.
    """
    primary_answer = ""
    try:
        primary_answer = get_ai_response(
            prompt, max_tokens=max_tokens, provider=provider, reasoning_effort=reasoning_effort
        )
    except Exception as e:
        logging.getLogger(__name__).info("get_ai_response_with_escalation: primary call failed: %s", e)
        primary_answer = ""

    if len(primary_answer.strip()) >= min_length:
        return primary_answer

    if not os.getenv("ANTHROPIC_API_KEY"):
        # No escalation path configured -- return the (possibly empty)
        # primary result, same as if this wrapper didn't exist.
        return primary_answer

    try:
        escalated = _claude(prompt, max_tokens)
        if len(escalated.strip()) >= min_length:
            return escalated
    except Exception as e:
        logging.getLogger(__name__).info("get_ai_response_with_escalation: Claude escalation failed: %s", e)

    return primary_answer


VISION_PROVIDER = os.getenv("VISION_PROVIDER", "gemini")


def get_vision_response(prompt: str, image_base64: str, mime_type: str = "image/jpeg", max_tokens: int = 1000) -> str:
    """
    Analyze an image with a vision-capable model.

    Switch provider via VISION_PROVIDER env var:
        gemini → Google Gemini (recommended — much better at reading exact
                 numbers/axis labels off charts than Groq's preview models)
        groq   → Groq (fast/cheap, but weaker at precise number-reading)

    Args:
        prompt: The text instructions/question about the image
        image_base64: Base64-encoded image data (no data: prefix)
        mime_type: Image MIME type (image/jpeg, image/png, image/webp)
        max_tokens: Max response tokens

    Returns:
        str: AI response text
    """
    provider = VISION_PROVIDER.lower()
    _LAST_USAGE_TOKENS["value"] = 0

    if provider == "gemini":
        return _gemini_vision(prompt, image_base64, mime_type, max_tokens)
    elif provider == "groq":
        return _groq_vision(prompt, image_base64, mime_type, max_tokens)
    else:
        raise ValueError(f"Unknown VISION_PROVIDER: {provider}. Use gemini / groq")


def _gemini_vision(prompt: str, image_base64: str, mime_type: str, max_tokens: int) -> str:
    import requests

    api_key = os.getenv("GEMINI_API_KEY")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": image_base64}},
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": max_tokens,
            # Gemini 2.5 Flash uses hidden "thinking" tokens by default, which
            # count against maxOutputTokens and were eating the whole budget
            # before any visible JSON got written — hence truncated output.
            # Disable thinking so all tokens go to the actual answer.
            "thinkingConfig": {"thinkingBudget": 0},
            # Force strict JSON output — the API guarantees a syntactically
            # valid, complete JSON object instead of markdown-wrapped or
            # truncated free-form text.
            "responseMimeType": "application/json",
        },
    }
    res = requests.post(url, json=payload, timeout=60)
    res.raise_for_status()
    data = res.json()
    usage = data.get("usageMetadata", {}).get("totalTokenCount")
    if usage:
        _LAST_USAGE_TOKENS["value"] = usage
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _groq_vision(prompt: str, image_base64: str, mime_type: str, max_tokens: int) -> str:
    from groq import Groq
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    response = client.chat.completions.create(
        # Groq deprecated llama-4-scout/llama-3.2-vision in June 2026.
        # qwen/qwen3.6-27b is the current vision-capable model (preview tier).
        model="qwen/qwen3.6-27b",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                    },
                ],
            }
        ],
        temperature=0.3,
        max_tokens=max_tokens,
    )
    if getattr(response, "usage", None) and response.usage.total_tokens:
        _LAST_USAGE_TOKENS["value"] = response.usage.total_tokens
    return response.choices[0].message.content.strip()


def _groq(prompt: str, max_tokens: int, reasoning_effort: str = "low") -> str:
    """
    2026-07-20 fix: "AI文字解讀" (chart-analysis.html's commentary feature,
    which calls this via the AI_PROVIDER default) was silently returning
    empty text. Root cause: openai/gpt-oss-120b is a REASONING model --
    per Groq's own docs (console.groq.com/docs/reasoning), its internal
    chain-of-thought tokens count against the same max_completion_tokens
    budget as the visible answer, and Groq's community forum documents
    this model returning a fully empty message.content when that budget
    is too low (worse the lower it is) -- the model spends the whole
    budget "thinking" and never gets to write a visible answer. This
    codebase's chart-analysis.html commentary call used max_tokens=400,
    well under Groq's own recommended 1024+ default for this model.

    Fix: (1) reasoning_effort="low" by default -- per Groq's docs this
    specifically reduces how many tokens gpt-oss-120b spends on hidden
    reasoning, leaving more of the budget for the actual visible answer;
    (2) a token floor on top of whatever the caller asked for, since even
    "low" reasoning effort still needs headroom beyond a short answer's
    own length; (3) max_completion_tokens is the parameter name Groq's
    current docs use for reasoning-aware models (max_tokens still
    appeared to be accepted before, but wasn't reliably budgeting
    reasoning tokens against the callers' request).

    2026-07-30 addition: reasoning_effort is now a parameter (still
    defaults to "low", so every existing caller that doesn't pass it is
    byte-for-byte unaffected) so a specific high-value call site (e.g.
    api/chat.py's flagship AI assistant) can opt into "high" for better
    reasoning quality. Because "high" spends MORE hidden tokens on
    reasoning than "low" -- exactly the failure mode the empty-content
    bug above already documents -- the token floor scales up with it
    (1400 instead of 700) so there's still real headroom left for the
    visible answer after reasoning eats its share. Callers that want
    "high" should also pass a generous max_tokens themselves; this floor
    is a safety net, not a substitute for that.
    """
    from groq import Groq
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    floor = 1400 if reasoning_effort == "high" else 700
    response = client.chat.completions.create(
        # llama-3.1-8b-instant was deprecated by Groq on 2026-06-17.
        # openai/gpt-oss-120b is Groq's recommended replacement.
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_completion_tokens=max(max_tokens, floor),
        reasoning_effort=reasoning_effort,
    )
    if getattr(response, "usage", None) and response.usage.total_tokens:
        _LAST_USAGE_TOKENS["value"] = response.usage.total_tokens
    content = (response.choices[0].message.content or "").strip()
    if not content:
        # Defensive fallback: if the model still burned the whole budget
        # on reasoning (shouldn't happen with the above, but Groq's own
        # bug reports show it can still occur), fail loudly instead of
        # silently returning "" -- the caller's except-block then shows
        # a real error message instead of a blank result.
        raise ValueError("Groq gpt-oss-120b returned empty content (reasoning likely consumed the token budget)")
    return content


def _deepseek(prompt: str, max_tokens: int, _retry: int = 1) -> str:
    """
    2026-07-20: switched this provider's backing host from DeepSeek's own
    API (api.deepseek.com) to DeepInfra (api.deepinfra.com) -- the account
    only has a DeepInfra API key, not an official DeepSeek one, and
    DeepInfra hosts the same deepseek-ai/DeepSeek-V4-Flash model behind an
    OpenAI-compatible chat-completions endpoint, so this is a same-model
    swap of the transport, not a quality/capability downgrade. Auth is now
    DEEPINFRA_API_KEY (a DeepSeek-issued DEEPSEEK_API_KEY will NOT work
    against DeepInfra's endpoint -- the two are separate accounts/keys).

    DeepInfra also publishes no hard rate-limit/SLA guarantee for this
    model tier, so the same retry-once-after-backoff behavior as the
    previous direct-DeepSeek integration is kept (matches the "good
    citizen + graceful degradation" convention services/outbound_http.py
    uses for scraped sources).
    """
    import time
    import requests
    headers = {
        "Authorization": f"Bearer {os.getenv('DEEPINFRA_API_KEY')}",
        "Content-Type": "application/json"
    }
    payload = {
        # DeepInfra's model catalog id for this model (NOT the same string
        # DeepSeek's own API used -- see https://deepinfra.com/deepseek-ai/
        # DeepSeek-V4-Flash/api for the current reference). Same underlying
        # model as the previous "deepseek-v4-flash" on api.deepseek.com.
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3
    }
    res = requests.post("https://api.deepinfra.com/v1/openai/chat/completions",
                        json=payload, headers=headers, timeout=30)
    if res.status_code in (429, 503) and _retry > 0:
        time.sleep(3)
        return _deepseek(prompt, max_tokens, _retry=_retry - 1)
    data = res.json()
    usage = data.get("usage", {}).get("total_tokens")
    if usage:
        _LAST_USAGE_TOKENS["value"] = usage
    return data["choices"][0]["message"]["content"].strip()


def generate_background_image(prompt: str, size: str = "1024x1024", _retry: int = 1):
    """2026-09-06 addition (services/video_engine_service.py's Custom
    Video feature -- AJ asked for a relevant AI-generated background
    image instead of a plain theme color). Same DEEPINFRA_API_KEY already
    used by _deepseek()/_kimi() above -- DeepInfra's OpenAI-compatible
    images endpoint defaults to FLUX.1 [schnell], priced at roughly
    $0.0005 per 1024x1024 image at its default step count, so this adds
    a fraction of a cent per video, not a meaningful new cost line.

    Returns raw image bytes (decoded from the API's base64 response) on
    success, or None on ANY failure (missing key, network error, bad/
    missing response data) -- caller must treat a background image as a
    nice-to-have, same "real chart is a bonus, never a requirement"
    convention video_engine_service.py already applies to its candlestick
    chart slides. Never raises.
    """
    import base64
    import time
    import requests

    key = os.getenv("DEEPINFRA_API_KEY")
    if not key:
        return None
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {"prompt": prompt, "size": size, "n": 1, "response_format": "b64_json"}
    try:
        res = requests.post("https://api.deepinfra.com/v1/openai/images/generations",
                            json=payload, headers=headers, timeout=30)
        if res.status_code in (429, 503) and _retry > 0:
            time.sleep(3)
            return generate_background_image(prompt, size=size, _retry=_retry - 1)
        if res.status_code != 200:
            return None
        data = res.json()
        b64_json = data["data"][0]["b64_json"]
        return base64.b64decode(b64_json)
    except Exception:
        return None


def _openrouter(prompt: str, max_tokens: int, _retry: int = 1) -> str:
    """
    2026-07-26 addition: OpenRouter (https://openrouter.ai) -- a single
    OpenAI-compatible endpoint that fans out to many upstream providers/
    models (GPT/Claude/Gemini/Llama/etc.) under one OPENROUTER_API_KEY.
    Purely additive: this is only reached when a caller explicitly passes
    provider="openrouter" to get_ai_response(), or AI_PROVIDER=openrouter
    is set in the environment -- every existing groq/deepseek/claude call
    site and the default AI_PROVIDER are completely untouched.

    Model is configurable via OPENROUTER_MODEL (defaults to a reasonable
    general-purpose model below) -- change it per deployment without a
    code change if a better/cheaper model becomes available.

    Same retry-once-on-429/503 behavior as _deepseek() -- OpenRouter
    fans requests out to third-party upstreams, so a transient rate-limit
    from whichever provider it picked shouldn't hard-fail the caller.
    """
    import time
    import requests
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
        "Content-Type": "application/json",
        # OpenRouter's own docs recommend these two for attribution/
        # analytics on their dashboard -- optional, harmless if omitted.
        "HTTP-Referer": "https://www.xfinlab.com",
        "X-Title": "XFINLAB",
    }
    payload = {
        "model": os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    res = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        json=payload, headers=headers, timeout=30,
    )
    if res.status_code in (429, 503) and _retry > 0:
        time.sleep(3)
        return _openrouter(prompt, max_tokens, _retry=_retry - 1)
    data = res.json()
    usage = data.get("usage", {}).get("total_tokens")
    if usage:
        _LAST_USAGE_TOKENS["value"] = usage
    return data["choices"][0]["message"]["content"].strip()


def _kimi(prompt: str, max_tokens: int, _retry: int = 1) -> str:
    """
    2026-07-30 addition: Moonshot AI's Kimi K2.6 -- an open-weight
    (Modified MIT license) agentic/reasoning model, served via DeepInfra's
    OpenAI-compatible endpoint under the SAME DEEPINFRA_API_KEY already
    used by _deepseek() above. No new account or key needed to try this --
    it's literally the same transport, different `model` string.

    Model id is DeepInfra's catalog id (moonshotai/Kimi-K2.6), configurable
    via KIMI_MODEL if a newer/cheaper Kimi version becomes available later
    without needing a code change.

    Same retry-once-on-429/503 behavior as _deepseek()/_openrouter() --
    DeepInfra publishes no hard rate-limit/SLA guarantee for this tier.

    Purely additive: only reached when a caller explicitly passes
    provider="kimi" or AI_PROVIDER=kimi is set -- every existing
    groq/deepseek/claude/openrouter call site is untouched.
    """
    import time
    import requests
    headers = {
        "Authorization": f"Bearer {os.getenv('DEEPINFRA_API_KEY')}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": os.getenv("KIMI_MODEL", "moonshotai/Kimi-K2.6"),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    res = requests.post(
        "https://api.deepinfra.com/v1/openai/chat/completions",
        json=payload, headers=headers, timeout=30,
    )
    if res.status_code in (429, 503) and _retry > 0:
        time.sleep(3)
        return _kimi(prompt, max_tokens, _retry=_retry - 1)
    data = res.json()
    usage = data.get("usage", {}).get("total_tokens")
    if usage:
        _LAST_USAGE_TOKENS["value"] = usage
    return data["choices"][0]["message"]["content"].strip()


def _claude(prompt: str, max_tokens: int) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    if getattr(message, "usage", None):
        _LAST_USAGE_TOKENS["value"] = (message.usage.input_tokens or 0) + (message.usage.output_tokens or 0)
    return message.content[0].text.strip()
