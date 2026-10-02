"""Compare models on Nina's voice posts: the same transcripts go through write_voice_post's prompt
with each model; price (usage.cost_rub), tokens, time and the posts go to a JSON file.

Usage: python compare_models.py VOICES_DIR OUT.json [model ...]
Each audio file in VOICES_DIR is transcribed once (polza.transcribe), as the bot does; a ready
transcript can be given instead as a .txt file. No pictures, nothing is sent to Telegram.
"""
import json
import mimetypes
import os
import subprocess
import sys
import time

import openai

import ai
import costs
import polza

MODELS = ["anthropic/claude-sonnet-5.5", "anthropic/claude-haiku-4.5"]
MIME = {".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4", ".wav": "audio/wav", ".webm": "audio/webm", ".flac": "audio/flac"}
JSON_HINT = ('\n\nAnswer with JSON only: {"post": "<the post in Telegram HTML>", '
             '"image_prompt": "<English picture description>"}')


def duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=60).stdout
        return float(out.strip())
    except Exception:
        return None


def transcript(path):
    """(text, rubles) for an audio file or a .txt transcript."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".txt":
        with open(path, encoding="utf-8") as f:
            return f.read().strip(), 0.0
    with open(path, "rb") as f:
        audio = f.read()
    mime = MIME.get(ext) or mimetypes.guess_type(path)[0] or "audio/ogg"
    before = len(costs.load())
    text = polza.transcribe(audio, mime, duration=duration(path))
    prices = [r.get("rub") for r in costs.load()[before:]]
    return text, (None if None in prices else sum(prices))


def parse(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def write_post(model, text):
    """write_voice_post with a given model, keeping the whole answer for its price and usage."""
    messages = [{"role": "system", "content": ai.voice_system_prompt()},
                {"role": "user", "content": f"Transcript of the voice message:\n{text}"}]
    fmt = polza.json_schema("voice_post", ai.VOICE_POST_SCHEMA)
    note = None
    started = time.monotonic()
    try:
        response = polza.client().chat.completions.create(model=model, messages=messages, max_tokens=4000,
                                                          response_format=fmt)
    except (openai.BadRequestError, openai.UnprocessableEntityError) as e:  # no structured output for the model
        note = f"без json_schema: {str(e)[:200]}"
        messages[0]["content"] += JSON_HINT
        response = polza.client().chat.completions.create(model=model, messages=messages, max_tokens=4000)
    seconds = round(time.monotonic() - started, 1)
    usage = response.usage.model_dump() if response.usage else None
    row = {"model": model, "rub": costs.price(response), "usage": usage, "seconds": seconds, "note": note,
           "finish_reason": response.choices[0].finish_reason if response.choices else None}
    try:
        row.update(parse(polza.text(response.choices[0].message)))
        row["chars"] = len(row.get("post", ""))
    except Exception as e:
        row["error"] = f"{e!r}"[:300]
        row["raw"] = polza.text(response.choices[0].message) if response.choices else None
    return row


def main(src, out, models):
    results = []
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if not os.path.isfile(path):
            continue
        item = {"file": name}
        try:
            item["transcript"], item["transcribe_rub"] = transcript(path)
        except Exception as e:
            item["error"] = f"transcribe: {e!r}"[:300]
            results.append(item)
            continue
        item["posts"] = []
        for model in models:
            try:
                item["posts"].append(write_post(model, item["transcript"]))
            except Exception as e:
                item["posts"].append({"model": model, "error": f"{e!r}"[:300]})
            print(f"{name}: {model} done")  # no content in the logs: the repository is public
        results.append(item)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3:] or MODELS)
