"""Minimal Telegram Bot API client (stdlib only).

Telegram being down or overloaded (no connection, 5xx, 429) is retried a few times and then
raised as Unavailable; a refusal from Telegram (bad request, message not found) is RuntimeError.
The bot token never appears in error messages.
"""
import json
import os
import socket
import time
import urllib.error
import urllib.request

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}/"
ATTEMPTS, MAX_WAIT = 3, 30
FILE_LIMIT = 20 * 1024 * 1024  # bots can download files up to 20 MB
TOO_BIG = "Файл больше 20 МБ, Telegram не даёт ботам скачивать такие. Пришли запись покороче или сожми её."


class Unavailable(Exception):
    """Telegram did not answer (network, 5xx, too many requests). Worth trying again later."""


def redact(text):
    return str(text).replace(TOKEN, "***") if TOKEN else str(text)


def _request(url, data=None, timeout=60):
    """Bytes of the answer. Retries what is safe to retry: no connection, 5xx and 429 (the request
    was not carried out). A timeout after the request went out is not retried, so nothing is sent twice."""
    for attempt in range(1, ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code != 429 and e.code < 500:
                return body  # Telegram's own error ({"ok": false, "description": ...})
            wait = 2 ** attempt
            if e.code == 429:
                try:
                    wait = json.loads(body)["parameters"]["retry_after"]
                except Exception:
                    pass
            problem = f"HTTP {e.code}"
        except (socket.timeout, TimeoutError) as e:
            raise Unavailable(f"Telegram: timeout ({redact(e)})") from None
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise Unavailable(f"Telegram: timeout ({redact(reason)})") from None
            wait, problem = 2 ** attempt, redact(reason)
        if attempt == ATTEMPTS or wait > MAX_WAIT:
            raise Unavailable(f"Telegram: {problem}") from None
        time.sleep(wait)


def call(method, **params):
    data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
    body = _request(API + method, data)
    try:
        res = json.loads(body)
    except ValueError:
        raise Unavailable(f"{method}: Telegram answered not with JSON") from None
    if not res.get("ok"):
        raise RuntimeError(f"{method}: {redact(res.get('description'))}")
    return res["result"]


def download(file_id):
    """Bytes of a file sent to the bot (voice message etc., up to 20 MB)."""
    path = call("getFile", file_id=file_id)["file_path"]
    return _request(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=120)
