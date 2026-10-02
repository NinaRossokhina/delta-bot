"""Tests of tg.py: retries when Telegram is down, no token in errors (no network)."""
import io, json, os, sys, unittest, urllib.error
from unittest import mock

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tg


def http_error(code, body):
    return urllib.error.HTTPError(tg.API + "x", code, "err", {}, io.BytesIO(json.dumps(body).encode()))


class Answer(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


class TgTest(unittest.TestCase):
    def setUp(self):
        for target, val in [("tg.TOKEN", "123:SECRET"), ("tg.API", "https://api.telegram.org/bot123:SECRET/")]:
            pa = mock.patch(target, val); pa.start(); self.addCleanup(pa.stop)

    def run_with(self, answers):
        calls, waits = [], []
        def urlopen(req, timeout):
            calls.append(req.full_url)
            a = answers.pop(0)
            if isinstance(a, BaseException): raise a
            return Answer(json.dumps(a).encode())
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep", waits.append):
            try:
                return tg.call("sendMessage", chat_id=1, text="x"), calls, waits
            except Exception as e:
                return e, calls, waits

    def test_ok(self):
        res, calls, _ = self.run_with([{"ok": True, "result": {"message_id": 5}}])
        self.assertEqual(res, {"message_id": 5})

    def test_502_retried_then_ok(self):
        res, calls, waits = self.run_with([http_error(502, {}), {"ok": True, "result": 1}])
        self.assertEqual((res, len(calls), waits), (1, 2, [2]))

    def test_429_waits_retry_after(self):
        res, calls, waits = self.run_with([http_error(429, {"ok": False, "parameters": {"retry_after": 7}}), {"ok": True, "result": 1}])
        self.assertEqual((res, waits), (1, [7]))

    def test_down_after_3_attempts_without_token(self):
        err = urllib.error.URLError(OSError(f"cannot reach {tg.API}"))
        res, calls, _ = self.run_with([err, err, err])
        self.assertIsInstance(res, tg.Unavailable)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("SECRET", str(res))

    def test_timeout_is_not_repeated(self):
        res, calls, _ = self.run_with([urllib.error.URLError(TimeoutError("timed out"))])
        self.assertIsInstance(res, tg.Unavailable)
        self.assertEqual(len(calls), 1)  # the message may have gone out: no second copy

    def test_telegram_refusal_is_runtime_error(self):
        res, calls, _ = self.run_with([http_error(400, {"ok": False, "description": "Bad Request: message is too long"})])
        self.assertIsInstance(res, RuntimeError)
        self.assertNotIsInstance(res, tg.Unavailable)
        self.assertEqual(len(calls), 1)

    def test_not_json_answer(self):
        def urlopen(req, timeout): return Answer(b"<html>Bad Gateway</html>")
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertRaises(tg.Unavailable, tg.call, "getMe")


if __name__ == "__main__":
    unittest.main()
