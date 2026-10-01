"""Shared helpers for Polza AI (https://polza.ai/docs), an OpenAI-compatible gateway to many models.

All AI calls of the bot go through here. The key comes only from the POLZA_API_KEY env var.
"""
import base64
import os

from openai import OpenAI

BASE_URL = "https://polza.ai/api/v1"
MODEL = "anthropic/claude-sonnet-5.5"
WEB = {"id": "web", "max_results": 8}  # web search plugin, works with any model

_client = None


def client():
    global _client
    if _client is None:
        _client = OpenAI(base_url=BASE_URL, api_key=os.environ["POLZA_API_KEY"], timeout=300, max_retries=2)
    return _client


def function_tool(name, description, parameters, strict=True):
    """A tool (function) definition in the Chat Completions format."""
    return {"type": "function",
            "function": {"name": name, "description": description, "parameters": parameters, "strict": strict}}


def json_schema(name, schema):
    """response_format for structured output: the reply is JSON that matches `schema`."""
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}


def chat(messages, tools=None, web=False, model=MODEL, max_tokens=16000, **params):
    """One Chat Completions request. Returns the first choice (.message, .finish_reason).

    web=True turns on the web search plugin; found sources come back in message.annotations.
    """
    if tools:
        params["tools"] = tools
    extra = {"plugins": [WEB]} if web else None
    response = client().chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens, extra_body=extra, **params)
    return response.choices[0]


def assistant_message(message):
    """The model's reply as a dict to append to `messages` for the next turn.

    Keeps tool_calls and reasoning_details (needed by Claude models to continue after tool calls).
    """
    data = message.model_dump(exclude_none=True)
    return {k: data[k] for k in ("role", "content", "tool_calls", "reasoning_details") if k in data}


def text(message):
    """Plain text of a reply (content can be a string, a list of parts or None)."""
    content = message.content
    if isinstance(content, list):
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return (content or "").strip()


TRANSCRIBE_MODEL = "openai/gpt-4o-transcribe"


def transcribe(audio, mime="audio/ogg", language="ru", model=TRANSCRIBE_MODEL):
    """Speech to text (POST /audio/transcriptions). `audio` is the file's bytes (mp3, wav, m4a, flac,
    ogg or webm, up to 25 MB); it goes as a base64 data URL in the JSON body, as the docs show."""
    file = f"data:{mime};base64,{base64.b64encode(audio).decode()}"
    result = client().post("/audio/transcriptions", cast_to=object,
                           body={"model": model, "file": file, "language": language})
    return (result.get("text") or "").strip()
