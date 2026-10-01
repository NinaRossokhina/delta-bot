"""Tests of ai.py and polza.py with a stubbed Polza API and Telegram (no network). Run: python -m unittest discover -s tests -v"""
import json, os, sys, tempfile, unittest
from unittest import mock

import httpx
from openai import OpenAI

os.environ.update(TELEGRAM_BOT_TOKEN="test", ADMIN_CHAT_ID="1", POLZA_API_KEY="test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai, polza

POST = {"text": "<b>Заголовок</b>\n\nТекст <a href='https://example.com'>Источник</a>",
        "media": "https://example.com/a.jpg", "media_type": "photo"}


def completion(content=None, tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                             for i, (name, args) in enumerate(tool_calls)]
        finish = "tool_calls"
    return {"id": "x", "object": "chat.completion", "created": 0, "model": polza.MODEL,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}]}


class AiTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd(); os.chdir(self.dir.name)
        open("style-guide.md", "w", encoding="utf-8").write("Пиши коротко.")
        open("feedback.md", "w", encoding="utf-8").write("- 2026-10-01 «Старое»: скучно\n")
        self.requests, self.replies, self.sent, self.calls = [], [], [], []

        def handler(request):
            self.requests.append(request)
            return httpx.Response(200, json=self.replies.pop(0))
        client = OpenAI(base_url=polza.BASE_URL, api_key=os.environ["POLZA_API_KEY"],
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        for target, val in [("polza._client", client), ("ai.send", lambda d, day=None: self.sent.append((d, day))),
                            ("ai.call", lambda m, **p: self.calls.append((m, p))),
                            ("ai.now_msk", lambda: "2026-10-02T10:00")]:
            pa = mock.patch(target, val); pa.start(); self.addCleanup(pa.stop)

    def tearDown(self):
        os.chdir(self.cwd); self.dir.cleanup()

    def bodies(self): return [json.loads(r.content) for r in self.requests]

    def test_chat_sends_drafts_and_answers(self):
        self.replies = [completion(tool_calls=[("send_drafts", {"posts": [POST, {**POST, "media": "", "media_type": "none"}]})]),
                        completion("Нашла две новости.")]
        os.environ["TASK"] = json.dumps({"kind": "chat", "text": "пришли 2 новые новости"})
        ai.main()
        self.assertEqual([d for d, _ in self.sent], [
            {"text": POST["text"], "media": POST["media"], "media_type": "photo"},
            {"text": POST["text"], "media": None, "media_type": "none"}])
        self.assertEqual(self.calls, [("sendMessage", {"chat_id": "1", "text": "Нашла две новости."})])
        first, second = self.bodies()
        r = self.requests[0]
        self.assertEqual(str(r.url), "https://polza.ai/api/v1/chat/completions")
        self.assertEqual(r.headers["authorization"], "Bearer test-key")
        self.assertEqual(first["model"], "anthropic/claude-sonnet-5.5")
        self.assertEqual(first["plugins"], [{"id": "web", "max_results": 8}])
        self.assertNotIn("plugins", second)  # no more searching after the drafts are sent
        system = first["messages"][0]
        self.assertEqual(system["role"], "system")
        for part in ("Пиши коротко.", "скучно", "2026-10-02"):
            self.assertIn(part, system["content"])
        self.assertEqual(first["messages"][1], {"role": "user", "content": "пришли 2 новые новости"})
        self.assertEqual({t["function"]["name"] for t in first["tools"]}, {"fetch_page", "send_drafts"})
        self.assertTrue(all(t["type"] == "function" and t["function"]["strict"] for t in first["tools"]))
        self.assertEqual(second["messages"][2]["tool_calls"][0]["function"]["name"], "send_drafts")
        self.assertEqual(second["messages"][3], {"role": "tool", "tool_call_id": "call_0", "content": "Sent 2 drafts."})

    def test_fetch_page_tool_result_goes_back(self):
        self.replies = [completion(tool_calls=[("fetch_page", {"url": "https://example.com/news"})]),
                        completion("Это не новость, просто ответ.")]
        page = {"title": "T", "og_image": "https://example.com/a.jpg", "published": "", "text": "Текст"}
        with mock.patch("ai.fetch_page", return_value=page) as fp:
            self.assertEqual(ai.run({"kind": "chat", "text": "что нового?"}), "Это не новость, просто ответ.")
        fp.assert_called_once_with("https://example.com/news")
        tool_msg = self.bodies()[1]["messages"][-1]
        self.assertEqual(json.loads(tool_msg["content"]), page)
        self.assertIn("plugins", self.bodies()[1])  # still researching
        self.assertEqual(self.sent, [])

    def test_edit_keeps_media_type_and_marks_old_draft(self):
        self.replies = [completion(tool_calls=[("send_drafts", {"posts": [{**POST, "media": "https://v.mp4", "media_type": "photo"}]})]),
                        completion("")]
        task = {"kind": "edit", "msg": 7, "html": "<b>Старый</b>", "media": "https://v.mp4", "media_type": "video",
                "day": "2026-10-03", "instructions": "короче"}
        os.environ["TASK"] = json.dumps(task)
        ai.main()
        self.assertEqual(self.sent, [({"text": POST["text"], "media": "https://v.mp4", "media_type": "video"}, "2026-10-03")])
        self.assertIn("короче", self.bodies()[0]["messages"][1]["content"])
        self.assertEqual(self.calls[0][0], "editMessageReplyMarkup")
        self.assertIn("Новая версия ниже", str(self.calls[0][1]["reply_markup"]))
        self.assertEqual(len(self.calls), 1)  # empty answer is not sent

    def test_failed_media_retried_without_it(self):
        self.replies = [completion(tool_calls=[("send_drafts", {"posts": [POST]})]), completion("Готово")]
        def flaky(d, day=None):
            if d["media"]:
                raise RuntimeError("bad image")
            self.sent.append((d, day))
        with mock.patch("ai.send", flaky):
            ai.run({"kind": "chat", "text": "пост"})
        self.assertEqual(self.sent[0][0]["media"], None)

    def test_refusal(self):
        self.replies = [completion("", finish="content_filter")]
        self.assertIn("сформулировать иначе", ai.run({"kind": "chat", "text": "x"}))

    def test_api_error_restores_buttons_and_hides_key(self):
        def handler(request):
            return httpx.Response(401, json={"error": {"code": 401, "message": "bad key"}})
        bad = OpenAI(base_url=polza.BASE_URL, api_key="test-key", max_retries=0,
                     http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        os.environ["TASK"] = json.dumps({"kind": "edit", "msg": 7, "html": "x", "day": "2026-10-03", "instructions": "y"})
        with mock.patch("polza._client", bad), self.assertRaises(Exception) as e:
            ai.main()
        self.assertNotIn("test-key", str(e.exception))
        self.assertEqual([m for m, _ in self.calls], ["sendMessage", "editMessageReplyMarkup"])
        self.assertIn("at:2026-10-03T09:00", str(self.calls[1][1]["reply_markup"]))

    def test_fetch_page_parses_html(self):
        page = ('<html><head><title>Новость &amp; факт</title><meta content="https://x.ru/i.jpg" property="og:image">'
                '<script>var a=1</script></head><body><p>Первый   абзац</p></body></html>').encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = page
        resp.headers.get_content_charset.return_value = "utf-8"
        with mock.patch("ai.urllib.request.urlopen", return_value=resp):
            got = ai.fetch_page("https://x.ru/news")
        self.assertEqual(got["title"], "Новость & факт")
        self.assertEqual(got["og_image"], "https://x.ru/i.jpg")
        self.assertIn("Первый абзац", got["text"])
        self.assertNotIn("var a", got["text"])
        self.assertIn("error", ai.fetch_page("file:///etc/passwd"))

    def daily_posts(self, n=5):
        rubrics = ["digest"] + ["news_of_the_day", "research", "good_news", "useful_find", "humor", "good_news"][:n - 1]
        return {"posts": [{"rubric": r, "text": f"<b>Пост {i}</b>\n\nТекст <a href='https://src/{i}'>источник</a>",
                           "image": f"https://img/{i}.jpg", "source": f"https://src/{i}"} for i, r in enumerate(rubrics)]}

    def test_daily_saves_file_with_times_digest_last(self):
        os.mkdir("drafts")
        self.replies = [completion(tool_calls=[("fetch_page", {"url": "https://src/1"})]),
                        completion(json.dumps(self.daily_posts(6), ensure_ascii=False))]
        out = os.path.join(self.dir.name, "out")
        os.environ.update(TASK='{"kind": "daily"}', GITHUB_OUTPUT=out)
        self.addCleanup(os.environ.pop, "GITHUB_OUTPUT")
        with mock.patch("ai.fetch_page", return_value={"title": "T"}):
            ai.main()
        self.assertEqual(open(out).read(), "file=drafts/2026-10-02.json\n")
        drafts = json.load(open("drafts/2026-10-02.json", encoding="utf-8"))
        self.assertEqual([d["time"] for d in drafts], ["09:00", "10:30", "12:00", "15:00", "18:00", "21:00"])
        self.assertTrue(drafts[-1]["text"].startswith("<b>Пост 0</b>"))  # the digest
        self.assertEqual(drafts[0], {"text": "<b>Пост 1</b>\n\nТекст <a href='https://src/1'>источник</a>",
                                     "media": "https://img/1.jpg", "media_type": "photo", "time": "09:00"})
        first = self.bodies()[0]
        self.assertEqual(first["response_format"]["type"], "json_schema")
        self.assertTrue(first["response_format"]["json_schema"]["strict"])
        self.assertEqual(first["response_format"]["json_schema"]["schema"], ai.DAILY_SCHEMA)
        self.assertEqual(first["plugins"], [{"id": "web", "max_results": 8}])
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["fetch_page"])
        self.assertIn("positive", first["messages"][1]["content"])
        self.assertEqual(self.sent, [])  # sending is send.yml's job
        self.assertEqual(self.calls, [])

    def test_daily_does_not_overwrite_and_caps_at_7(self):
        os.mkdir("drafts")
        open("drafts/2026-10-02.json", "w").write("[]")
        self.replies = [completion(json.dumps(self.daily_posts(7) | {"posts": self.daily_posts(7)["posts"] * 2}))]
        path = ai.daily()
        self.assertEqual(path, "drafts/2026-10-02-2.json")
        drafts = json.load(open(path))
        self.assertEqual(len(drafts), 7)
        self.assertEqual(drafts[-1]["time"], "21:00")
        self.assertTrue(drafts[-1]["text"].startswith("<b>Пост 0</b>"))

    def test_daily_error_tells_nina(self):
        self.replies = [completion("не JSON")]
        os.environ["TASK"] = '{"kind": "daily"}'
        with self.assertRaises(Exception):
            ai.main()
        self.assertIn("черновики на сегодня", self.calls[0][1]["text"])

    def test_recent_headlines_last_4_days_only(self):
        os.mkdir("drafts")
        for name, title in [("2026-09-27", "Старое"), ("2026-09-28", "Четыре дня назад"),
                            ("2026-10-02-extra", "Сегодня"), ("test", "Тест")]:
            json.dump([{"text": f"<b>{title}</b>\n\nтекст"}], open(f"drafts/{name}.json", "w", encoding="utf-8"))
        got = ai.recent_headlines()
        self.assertEqual(got, "- <b>Четыре дня назад</b>\n- <b>Сегодня</b>")

    def test_times_for(self):
        self.assertEqual(ai.times_for(5), ["09:00", "12:00", "15:00", "18:00", "21:00"])
        self.assertEqual(ai.times_for(7), ["09:00", "10:30", "12:00", "13:30", "15:00", "18:00", "21:00"])


if __name__ == "__main__":
    unittest.main()
