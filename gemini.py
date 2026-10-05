"""Free Google Gemini (the free tier of the Gemini API, https://ai.google.dev/gemini-api/docs).

When the GEMINI_API_KEY secret is set, daily drafts, Nina's requests and hot news go here instead
of Polza (ai.ask, ai.free_first); if Gemini fails, the task is done again through Polza and Nina is
told why. Voice posts, transcription and pictures stay on Polza.

Chat steps use Gemini's OpenAI-compatible endpoint (tools and JSON answers work as with Polza).
Google Search is not available there, so web search is the web_search tool: our code makes a separate
native generateContent request with the google_search tool and returns the answer with its sources.
Free requests are written to costs.jsonl at 0 rubles, so /cost shows how many went to Gemini.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import openai
from openai import OpenAI

import costs

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# The alias always points to the current Flash model (free tier); the GEMINI_MODEL repository
# variable replaces it without a code change.
MODEL = os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"
SPARE_MODEL = "gemini-flash-lite-latest"  # also free; used when MODEL stays busy (503 "high demand")
RATE_LIMIT_WAITS = (20, 40, 60)  # seconds to wait after "too many requests" or "busy" before giving up
UA = {"User-Agent": "Mozilla/5.0 (compatible; DeltaBot/1.0)"}

WEB_SEARCH = {"type": "function", "function": {
    "name": "web_search",
    "description": "Search the web with Google. Returns a short answer and its sources (title, url). "
                   "Ask for what you need in one query, e.g. «AI news of the last 24 hours: new models and research».",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                   "required": ["query"], "additionalProperties": False}}}


class Failed(Exception):
    """Gemini did not work (no access, quota, bad answer): the caller does the task through Polza."""


_client = None
_off = False


def on():
    """Gemini is used: the key is set and it has not failed in this run."""
    return bool(os.environ.get("GEMINI_API_KEY")) and not _off


def off():
    global _off
    _off = True


def client():
    global _client
    if _client is None:
        _client = OpenAI(base_url=BASE_URL + "/openai/", api_key=os.environ["GEMINI_API_KEY"], timeout=300,
                         max_retries=2)
    return _client


def _plain_tools(tools):
    """Function tools without "strict" (an OpenAI field Gemini does not need)."""
    return [{**t, "function": {k: v for k, v in t["function"].items() if k != "strict"}} for t in tools]


def _with_json_hint(messages, response_format):
    """Tools and a JSON schema in one request: the schema goes into the system message as a rule."""
    schema = json.dumps(response_format["json_schema"]["schema"], ensure_ascii=False)
    hint = f"\n\nWhen you are done (no more tool calls), answer with JSON only, matching this JSON schema:\n{schema}"
    first = messages[0]
    if first.get("role") == "system":
        return [{**first, "content": first["content"] + hint}, *messages[1:]]
    return [{"role": "system", "content": hint.strip()}, *messages]


def chat(messages, tools=None, response_format=None, max_tokens=16000, sleep=time.sleep):
    """One Chat Completions request to Gemini, like polza.chat. Returns the first choice."""
    params = {}
    if tools:
        params["tools"] = _plain_tools(tools)
        if response_format:
            messages = _with_json_hint(messages, response_format)
    elif response_format:
        params["response_format"] = response_format
    attempts = [(MODEL, w) for w in RATE_LIMIT_WAITS] + [(SPARE_MODEL, 0), (SPARE_MODEL, None)]
    for model, wait in attempts:
        try:
            response = client().chat.completions.create(model=model, messages=messages, max_tokens=max_tokens, **params)
            break
        except (openai.RateLimitError, openai.InternalServerError) as e:  # quota or "high demand": wait, then the spare model
            if wait is None:
                raise Failed(f"Gemini перегружен или кончился лимит: {str(e)[:150]}") from e
            sleep(wait)
        except openai.APIError as e:
            raise Failed(f"{type(e).__name__}: {str(e)[:200]}") from e
    costs.record(f"google/{model}", {"cost": 0})
    if not response.choices:
        raise Failed("пустой ответ")
    return response.choices[0]


def final_url(url):
    """Google gives sources as redirect links; the post needs the real page address."""
    if "grounding-api-redirect" not in url:
        return url
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
            return r.geturl()
    except Exception:
        return url


def _generate(body, sleep):
    request = urllib.request.Request(f"{BASE_URL}/models/{MODEL}:generateContent", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    for wait in (*RATE_LIMIT_WAITS, None):
        try:
            with urllib.request.urlopen(request, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 503) or wait is None:
                raise Failed(f"поиск Google: HTTP {e.code} {e.read()[:200]!r}") from e
            sleep(wait)
        except OSError as e:
            raise Failed(f"поиск Google: {e!r}"[:200]) from e


def search(query, today="", sleep=time.sleep):
    """The web_search tool: Gemini with Google Search answers `query`; returns {"answer", "sources"}."""
    prompt = (f"{query}\n\nToday is {today}. For each story give its date, the key facts and numbers, and "
              f"name the source.") if today else query
    data = _generate({"contents": [{"role": "user", "parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]},
                     sleep)
    costs.record(f"google/{MODEL}", {"cost": 0})
    candidate = (data.get("candidates") or [{}])[0]
    answer = "".join(p.get("text", "") for p in (candidate.get("content") or {}).get("parts", [])).strip()
    chunks = [c.get("web") or {} for c in (candidate.get("groundingMetadata") or {}).get("groundingChunks", [])]
    chunks = [c for c in chunks if c.get("uri")][:10]
    with ThreadPoolExecutor(8) as pool:
        urls = list(pool.map(final_url, [c["uri"] for c in chunks]))
    sources = [{"title": c.get("title", ""), "url": u} for c, u in zip(chunks, urls)]
    if not answer and not sources:
        raise Failed("поиск Google ничего не вернул")
    return {"answer": answer, "sources": sources}


def json_text(text):
    """JSON from a reply that may be wrapped in ```json fences."""
    m = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.S)
    return json.loads(m[1] if m else text)
