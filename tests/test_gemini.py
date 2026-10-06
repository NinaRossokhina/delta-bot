"""Tests of the free Gemini path (gemini.py and its use in ai.py) with stubbed APIs (no network)."""
import io, json, os, sys, tempfile, unittest
from unittest import mock

import httpx
from openai import OpenAI

os.environ.update(TELEGRAM_BOT_TOKEN="test", ADMIN_CHAT_ID="1", POLZA_API_KEY="test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai, costs, gemini, polza

from test_ai import completion


class GeminiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd(); os.chdir(self.dir.name)
        os.mkdir("drafts")
        open("style-guide.md", "w", encoding="utf-8").write("Пиши коротко.")
        self.g_requests, self.g_replies, self.p_requests, self.p_replies, self.calls = [], [], [], [], []

        def stub(requests, replies):
            def handler(request):
                requests.append(request)
                status, body = replies.pop(0)
                return httpx.Response(status, json=body)
            return httpx.Client(transport=httpx.MockTransport(handler))
        g = OpenAI(base_url=gemini.BASE_URL + "/openai/", api_key="g-key", max_retries=0,
                   http_client=stub(self.g_requests, self.g_replies))
        p = OpenAI(base_url=polza.BASE_URL, api_key="test-key", http_client=stub(self.p_requests, self.p_replies))
        for target, val in [("gemini._client", g), ("polza._client", p), ("gemini._off", False),
                            ("ai.call", lambda m, **kw: self.calls.append((m, kw))),
                            ("ai.now_msk", lambda: "2026-10-02T10:00"),
                            ("ai.fetch_page", lambda url: {"title": "T", "og_image": ""})]:
            pa = mock.patch(target, val); pa.start(); self.addCleanup(pa.stop)
        pa = mock.patch.dict(os.environ, GEMINI_API_KEY="g-key"); pa.start(); self.addCleanup(pa.stop)

    def tearDown(self):
        os.chdir(self.cwd); self.dir.cleanup()

    def posts(self):
        rubrics = ["news_of_the_day"] * 7 + ["digest"]
        return {"posts": [{"rubric": r, "text": f"<b>Пост {i}</b>", "image": f"https://img/{i}.jpg",
                           "source": f"https://src/{i}"} for i, r in enumerate(rubrics)]}

    def test_daily_goes_to_gemini_with_its_web_search(self):
        found = {"answer": "Новости", "sources": [{"title": "a", "url": "https://a/1"}]}
        self.g_replies += [(200, completion(tool_calls=[("web_search", {"query": "AI news"})])),
                           (200, completion("```json\n" + json.dumps(self.posts()) + "\n```"))]
        with mock.patch("gemini.search", return_value=found) as search:
            path = ai.free_first(ai.daily, "next")
        search.assert_called_once_with("AI news", today="2026-10-02")
        self.assertEqual(len(json.load(open(path))), 8)
        self.assertEqual(self.p_requests, [])  # nothing paid
        first, second = [json.loads(r.content) for r in self.g_requests]
        self.assertEqual(str(self.g_requests[0].url), gemini.BASE_URL + "/openai/chat/completions")
        self.assertEqual(first["model"], gemini.MODEL)
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["fetch_page", "web_search"])
        self.assertTrue(all("strict" not in t["function"] for t in first["tools"]))
        self.assertNotIn("plugins", first)
        self.assertNotIn("response_format", first)  # with tools the schema is a rule in the system message
        self.assertIn("answer with JSON only", first["messages"][0]["content"])
        self.assertEqual(json.loads(second["messages"][-1]["content"]), found)
        self.assertEqual([(r["model"], r["rub"]) for r in costs.load()], [(f"google/{gemini.MODEL}", 0)] * 2)
        self.assertEqual(self.calls, [])

    def test_gemini_failure_falls_back_to_polza_and_tells_nina(self):
        self.g_replies.append((403, {"error": {"message": "User location is not supported"}}))
        self.p_replies.append((200, completion(json.dumps(self.posts()))))
        path = ai.free_first(ai.daily, "next")
        self.assertEqual(len(json.load(open(path))), 8)
        self.assertFalse(gemini.on())
        body = json.loads(self.p_requests[0].content)
        self.assertEqual(body["model"], polza.MODEL)
        self.assertEqual(body["plugins"], [{"id": "web", "max_results": 8}])
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Gemini не сработал", self.calls[0][1]["text"])
        self.assertIn("через Polza", self.calls[0][1]["text"])

    def test_rate_limit_waits_then_gives_up(self):
        waits = []
        self.g_replies += [(429, {"error": {"message": "quota"}})] * 5
        with self.assertRaises(gemini.Failed):
            gemini.chat([{"role": "user", "content": "hi"}], sleep=waits.append)
        self.assertEqual(waits, [*gemini.RATE_LIMIT_WAITS, 0])

    def test_busy_model_waits_then_uses_the_spare_one(self):
        waits = []
        busy = (503, {"error": {"code": 503, "message": "This model is currently experiencing high demand."}})
        self.g_replies += [busy] * 3 + [busy, (200, completion("ok"))]
        choice = gemini.chat([{"role": "user", "content": "hi"}], sleep=waits.append)
        self.assertEqual(choice.message.content, "ok")
        self.assertEqual(waits, [*gemini.RATE_LIMIT_WAITS, 0])
        models = [json.loads(r.content)["model"] for r in self.g_requests]
        self.assertEqual(models, [gemini.MODEL] * 3 + [gemini.SPARE_MODEL] * 2)
        self.assertEqual(costs.load()[-1]["model"], f"google/{gemini.SPARE_MODEL}")

    def test_request_does_not_resend_drafts_through_polza(self):
        self.g_replies += [(200, completion(tool_calls=[("send_drafts", {"posts": [
                               {"text": "<b>A</b>", "media": "", "media_type": "none", "day": "2026-10-02"}]})])),
                           (400, {"error": {"message": "boom"}})]
        with mock.patch("ai.send", return_value={"message_id": 1}) as send:
            self.assertEqual(ai.free_first(ai.run, {"kind": "chat", "text": "пришли новость"}), "Готово.")
        send.assert_called_once()
        self.assertEqual(self.p_requests, [])

    def test_search_returns_answer_and_real_source_urls(self):
        reply = {"candidates": [{"content": {"parts": [{"text": "OpenAI выпустила модель."}]},
                                 "groundingMetadata": {"groundingChunks": [
                                     {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/X",
                                              "title": "openai.com"}},
                                     {"web": {"uri": "https://example.com/b", "title": "example.com"}}]}}]}
        sent = []

        def urlopen(request, timeout=None):
            sent.append(request)
            if "generateContent" in request.full_url:
                return io.BytesIO(json.dumps(reply).encode())
            r = mock.MagicMock()
            r.__enter__.return_value.geturl.return_value = "https://openai.com/news/x"
            return r
        with mock.patch("urllib.request.urlopen", urlopen):
            found = gemini.search("AI news", today="2026-10-02")
        self.assertEqual(found, {"answer": "OpenAI выпустила модель.", "sources": [
            {"title": "openai.com", "url": "https://openai.com/news/x"},
            {"title": "example.com", "url": "https://example.com/b"}]})
        api = sent[0]
        self.assertEqual(api.full_url, f"{gemini.BASE_URL}/models/{gemini.MODEL}:generateContent")
        self.assertEqual(api.get_header("X-goog-api-key"), "g-key")
        self.assertEqual(json.loads(api.data)["tools"], [{"google_search": {}}])

    def test_web_search_falls_back_to_free_news_feeds(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        items = [{"source": "TechCrunch AI", "title": "Old", "url": "https://t/old", "id": "1", "published": now - timedelta(days=5)},
                 {"source": "OpenAI", "title": "New model", "url": "https://o/new", "id": "2", "published": now - timedelta(hours=1)},
                 {"source": "Anthropic", "title": "Claude news", "url": "https://a/n", "id": "3", "published": None}]
        with mock.patch.dict(ai._google, on=True, news=None), \
                mock.patch("gemini.search", side_effect=gemini.Failed("HTTP 429 quota")) as search, \
                mock.patch("hot.collect", return_value=items) as collect:
            first = ai.web_search("AI news")
            second = ai.web_search("more AI news")
        search.assert_called_once()  # Google search is not tried again in this run
        collect.assert_called_once()  # the feeds are read once
        self.assertEqual(first, second)
        self.assertIn("not available", first["answer"])
        self.assertEqual([x["title"] for x in first["sources"]], ["Claude news", "New model"])
        sources = collect.call_args[0][0]
        self.assertIn("TechCrunch AI", [x[0] for x in sources])

    def test_search_quota_fails_at_once(self):
        import urllib.error
        waits = []
        error = urllib.error.HTTPError("u", 429, "quota", {}, io.BytesIO(b'{"error": {"status": "RESOURCE_EXHAUSTED"}}'))
        with mock.patch("urllib.request.urlopen", side_effect=error), self.assertRaises(gemini.Failed):
            gemini.search("AI", sleep=waits.append)
        self.assertEqual(waits, [])  # a daily quota: no point in waiting

    def test_json_text_accepts_fences(self):
        self.assertEqual(gemini.json_text('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(gemini.json_text('{"a": 1}'), {"a": 1})

    def test_off_without_key(self):
        with mock.patch.dict(os.environ, GEMINI_API_KEY=""):
            self.assertFalse(gemini.on())


if __name__ == "__main__":
    unittest.main()
