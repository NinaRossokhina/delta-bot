"""Shared helpers for Polza AI (https://polza.ai/docs), an OpenAI-compatible gateway to many models.

All AI calls of the bot go through here. The key comes only from the POLZA_API_KEY env var.
"""
import base64
import os
import subprocess

import openai
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



TRANSCRIBE_MODEL = "openai/whisper-large-v3-turbo"


def transcribe(audio, mime="audio/ogg", language="ru", model=TRANSCRIBE_MODEL):
    """Speech to text (POST /audio/transcriptions). `audio` is the file's bytes (mp3, wav, m4a, flac,
    ogg or webm, up to 25 MB); it goes as a base64 data URL in the JSON body, as the docs show.
    If Polza rejects the format (Telegram voices are ogg/opus), it is converted to mp3 with ffmpeg."""
    try:
        return _transcribe(audio, mime, language, model)
    except (openai.BadRequestError, openai.UnprocessableEntityError):
        if mime == "audio/mpeg":
            raise
        return _transcribe(to_mp3(audio), "audio/mpeg", language, model)


def _transcribe(audio, mime, language, model):
    file = f"data:{mime};base64,{base64.b64encode(audio).decode()}"
    result = client().post("/audio/transcriptions", cast_to=object,
                           body={"model": model, "file": file, "language": language})
    return (result.get("text") or "").strip()


def to_mp3(audio):
    """Convert any audio ffmpeg understands to mp3 (ffmpeg is installed by the ai.yml workflow)."""
    return subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-f", "mp3", "pipe:1"],
                          input=audio, capture_output=True, check=True, timeout=120).stdout


IMAGE_MODEL = "google/gemini-3.1-flash-image-preview"
MEDIA_POLL_SECONDS, MEDIA_MAX_SECONDS = 3, 180


def media(model, input, poll=MEDIA_POLL_SECONDS, limit=MEDIA_MAX_SECONDS, sleep=None, clock=None):
    """Generate an image (or other media) with POST /media and wait for it with GET /media/{id}
    every `poll` seconds, at most `limit` seconds. Returns the result URL (output.url).
    Raises TimeoutError if it is not ready in time, RuntimeError if the generation failed."""
    import time
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    job = client().post("/media", cast_to=object, body={"model": model, "input": input, "async": True})
    started = clock()
    while True:
        status = job.get("status")
        if status == "completed":
            url = _output_url(job.get("output"))
            if not url:
                raise RuntimeError("media: completed without output.url")
            return url
        if status in ("failed", "cancelled"):
            error = job.get("error") or {}
            raise RuntimeError(f"media {status}: {error.get('message', error) if isinstance(error, dict) else error}")
        if clock() - started >= limit:
            raise TimeoutError(f"media {job.get('id')} not ready after {limit} s")
        sleep(poll)
        job = client().get(f"/media/{job['id']}", cast_to=object)


def _output_url(output):
    """output.url; also accepts a list of outputs (first one) or a bare URL string."""
    if isinstance(output, list):
        output = output[0] if output else None
    if isinstance(output, dict):
        return output.get("url")
    return output if isinstance(output, str) else None
