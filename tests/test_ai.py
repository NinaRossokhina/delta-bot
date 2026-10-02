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
        self.assertEqual(self.calls[0][1]["text"], "Сегодня черновики не собрались: нейросеть вернула ответ не в том формате")

    def test_scheduled_skips_when_drafts_exist(self):
        os.mkdir("drafts")
        open("drafts/2026-10-02-extra.json", "w").write("[]")  # not daily drafts
        self.replies = [completion(json.dumps(self.daily_posts(5)))]
        os.environ["TASK"] = ""  # the schedule passes no task
        ai.main()
        self.assertTrue(os.path.exists("drafts/2026-10-02.json"))
        ai.main()  # second morning run: today's drafts are there
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(os.path.exists("drafts/2026-10-02-2.json"))

    def test_scheduled_polza_error_reason(self):
        def handler(request):
            return httpx.Response(402, json={"error": {"code": 402, "message": "Недостаточно средств"}})
        bad = OpenAI(base_url=polza.BASE_URL, api_key="test-key", max_retries=0,
                     http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        os.environ["TASK"] = ""
        with mock.patch("polza._client", bad), self.assertRaises(Exception):
            ai.main()
        text = self.calls[0][1]["text"]
        self.assertEqual(text, ai.FAILED_TODAY.format(ai.NO_MONEY))
        self.assertNotIn("test-key", text)

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

    def test_transcribe_sends_base64_json(self):
        self.replies = [{"text": " Пришли новость про роботов ", "language": "ru", "duration": 2.5}]
        self.assertEqual(polza.transcribe(b"OggS-voice"), "Пришли новость про роботов")
        r = self.requests[0]
        self.assertEqual(str(r.url), "https://polza.ai/api/v1/audio/transcriptions")
        self.assertEqual(r.headers["authorization"], "Bearer test-key")
        self.assertEqual(json.loads(r.content), {"model": "openai/whisper-large-v3-turbo",
                                                 "file": "data:audio/ogg;base64,T2dnUy12b2ljZQ==", "language": "ru"})

    def test_transcribe_converts_to_mp3_if_ogg_rejected(self):
        self.replies = [{"error": {"message": "unsupported format"}}, {"text": "ок"}]
        statuses = [400, 200]
        def handler(request):
            self.requests.append(request)
            return httpx.Response(statuses.pop(0), json=self.replies.pop(0))
        polza._client._client = httpx.Client(transport=httpx.MockTransport(handler))
        with mock.patch("polza.to_mp3", return_value=b"ID3") as conv:
            self.assertEqual(polza.transcribe(b"OggS"), "ок")
        conv.assert_called_once_with(b"OggS")
        self.assertEqual(json.loads(self.requests[1].content)["file"], "data:audio/mpeg;base64,SUQz")

    def voice_task(self, **extra):
        os.environ["TASK"] = json.dumps({"kind": "voice", "file_id": "F1", "mime": "audio/ogg", **extra})
        with mock.patch("ai.download", return_value=b"OggS") as dl:
            ai.main()
        dl.assert_called_once_with("F1")

    def texts(self): return [p["text"] for m, p in self.calls if m == "sendMessage"]

    VOICE_POST = {"post": "<b>Роботы научились готовить</b>\n\nТекст.", "image_prompt": "A robot cooking pasta, warm light."}

    def voice_post_setup(self):
        os.mkdir("style")
        open("style/my-voice.md", "w", encoding="utf-8").write("Пишу спокойно, без восклицаний.")
        open("style/examples.md", "w", encoding="utf-8").write("Появилась игра, в которой можно взорвать любой сайт")
        open("queue.json", "w").write('[{"msg": 1, "at": "2026-10-02T12:00"}]')
        sent = []
        def fake_send(d, day=None):
            sent.append((d, day)); return {"message_id": 77}
        for target, val in [("ai.send", fake_send), ("time.sleep", lambda s: None)]:
            pa = mock.patch(target, val); pa.start(); self.addCleanup(pa.stop)
        return sent

    def test_voice_makes_post_with_image(self):
        sent = self.voice_post_setup()
        self.replies = [{"text": "ну короче эээ роботы роботы научились готовить"},
                        completion(json.dumps(self.VOICE_POST, ensure_ascii=False)),
                        {"id": "m1", "object": "media.generation", "status": "pending"},
                        {"id": "m1", "status": "processing"},
                        {"id": "m1", "status": "completed", "output": {"url": "https://cdn.polza.ai/m1.png"}}]
        self.voice_task()
        self.assertEqual(sent, [({"text": self.VOICE_POST["post"], "media": "https://cdn.polza.ai/m1.png", "media_type": "photo",
                                  "time": "15:00", "suggested": True, "image": True}, "2026-10-02")])  # 12:00 is taken
        self.assertEqual(self.texts(), [])
        _, post, create, poll1, poll2 = self.requests
        system = json.loads(post.content)["messages"][0]["content"]
        for part in ["Пишу спокойно", "взорвать любой сайт", "Пиши коротко.", "скучно", "Invent nothing"]:
            self.assertIn(part, system)
        self.assertEqual(json.loads(post.content)["response_format"]["json_schema"]["schema"]["required"], ["post", "image_prompt"])
        self.assertEqual(str(create.url), "https://polza.ai/api/v1/media")
        self.assertEqual(json.loads(create.content), {"model": "google/gemini-3.1-flash-image-preview", "async": True, "input": {
            "prompt": "A robot cooking pasta, warm light, no text, no letters, no captions, no watermarks, no logos",
            "aspect_ratio": "4:3"}})
        self.assertEqual((poll1.method, str(poll1.url)), ("GET", "https://polza.ai/api/v1/media/m1"))
        self.assertEqual(json.load(open("images.json")), {"77": "A robot cooking pasta, warm light."})

    def test_voice_post_without_image_if_it_fails(self):
        sent = self.voice_post_setup()
        self.replies = [{"text": "роботы готовят"}, completion(json.dumps(self.VOICE_POST)),
                        {"id": "m1", "status": "failed", "error": {"message": "safety"}}]
        self.voice_task()
        self.assertEqual((sent[0][0]["media"], sent[0][0]["media_type"], sent[0][0]["image"]), (None, "none", True))
        self.assertIn("Другая картинка", self.texts()[0])

    def test_media_gives_up_after_3_minutes(self):
        self.replies = [{"id": "m1", "status": "pending"}] + [{"id": "m1", "status": "processing"}] * 100
        now, waits = [0], []
        def sleep(s): waits.append(s); now[0] += s
        with self.assertRaises(TimeoutError):
            polza.media(polza.IMAGE_MODEL, {"prompt": "x"}, sleep=sleep, clock=lambda: now[0])
        self.assertEqual(set(waits), {3})
        self.assertEqual(now[0], 180)
        self.assertEqual(ai.problem(TimeoutError()), ai.TIMEOUT)

    def test_media_completed_right_away(self):
        self.replies = [{"id": "m1", "status": "completed", "output": [{"url": "https://cdn/x.png"}]}]
        self.assertEqual(polza.media(polza.IMAGE_MODEL, {"prompt": "x"}), "https://cdn/x.png")

    def test_other_image_replaces_photo_in_place(self):
        open("images.json", "w").write('{"7": "A robot"}')
        self.replies = [{"id": "m2", "status": "completed", "output": {"url": "https://cdn/new.png"}}]
        buttons = {"inline_keyboard": [[{"text": "Другая картинка", "callback_data": "img"}]]}
        os.environ["TASK"] = json.dumps({"kind": "image", "msg": 7, "html": "<b>Пост</b>", "media": "https://cdn/old.png",
                                         "media_type": "photo", "day": "2026-10-02", "image": True, "buttons": buttons})
        ai.main()
        self.assertTrue(json.loads(self.requests[0].content)["input"]["prompt"].startswith("A robot, no text"))
        self.assertEqual(self.calls, [("editMessageMedia", {"chat_id": "1", "message_id": 7, "reply_markup": buttons, "media": {
            "type": "photo", "media": "https://cdn/new.png", "caption": "<b>Пост</b>", "parse_mode": "HTML"}})])

    def test_other_image_failure_restores_buttons(self):
        self.replies = [completion('{"image_prompt": "A robot"}'), {"id": "m2", "status": "failed"}]
        buttons = {"inline_keyboard": [[{"text": "Другая картинка", "callback_data": "img"}]]}
        os.environ["TASK"] = json.dumps({"kind": "image", "msg": 7, "html": "<b>Пост</b>", "media_type": "photo",
                                         "day": "2026-10-02", "buttons": buttons})
        with self.assertRaises(RuntimeError):
            ai.main()
        self.assertEqual(self.calls[-1], ("editMessageReplyMarkup", {"chat_id": "1", "message_id": 7, "reply_markup": buttons}))

    def test_voice_answer_to_edit_rewrites_draft(self):
        self.replies = [{"text": "сделай короче"},
                        completion(tool_calls=[("send_drafts", {"posts": [POST]})]), completion("Готово, сократила.")]
        self.voice_task(edit={"kind": "edit", "msg": 7, "html": "<b>Старый</b>", "media": None,
                              "media_type": None, "day": "2026-10-03"})
        self.assertEqual(self.texts(), ["Расшифровка:\nсделай короче", "Готово, сократила."])
        self.assertIn("Nina's notes:\nсделай короче", self.bodies()[1]["messages"][1]["content"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Новая версия ниже", str(self.calls))

    def test_voice_reason_goes_to_feedback(self):
        self.replies = [{"text": "слишком сложно"}]
        self.voice_task(reason="Термояд")
        self.assertEqual(open("feedback.md", encoding="utf-8").read().splitlines()[-1],
                         "- 2026-10-02 «Термояд»: слишком сложно")

    def test_voice_nothing_heard_restores_draft(self):
        self.replies = [{"text": ""}]
        self.voice_task(edit={"kind": "edit", "msg": 7, "html": "x", "day": "2026-10-03"})
        self.assertEqual(self.texts(), [ai.NOT_HEARD])
        self.assertIn("at:2026-10-03T09:00", str(self.calls[-1]))

    def test_no_money_and_timeout_messages(self):
        import openai
        resp = httpx.Response(402, request=httpx.Request("POST", "https://polza.ai/api/v1/x"))
        self.assertEqual(ai.problem(openai.APIStatusError("Payment required", response=resp, body=None)), ai.NO_MONEY)
        self.assertEqual(ai.problem(openai.APITimeoutError(request=resp.request)), ai.TIMEOUT)
        self.assertEqual(ai.problem(TimeoutError()), ai.TIMEOUT)
        self.assertIsNone(ai.problem(ValueError()))

    def test_voice_no_money_told_to_nina(self):
        def handler(request):
            return httpx.Response(402, json={"error": {"message": "Insufficient balance"}})
        polza._client._client = httpx.Client(transport=httpx.MockTransport(handler))
        polza._client.max_retries = 0
        with self.assertRaises(Exception):
            self.voice_task()
        self.assertEqual(self.texts(), [ai.NO_MONEY])


if __name__ == "__main__":
    unittest.main()
