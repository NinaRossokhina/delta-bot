"""Tests of ai.py and polza.py with a stubbed Polza API and Telegram (no network). Run: python -m unittest discover -s tests -v"""
import json, os, sys, tempfile, unittest
from unittest import mock

import httpx
from openai import OpenAI

os.environ.update(TELEGRAM_BOT_TOKEN="test", ADMIN_CHAT_ID="1", POLZA_API_KEY="test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai, costs, polza, tg

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

    def daily_posts(self, n=7):
        rubrics = ["news_of_the_day", "research", "useful_find", "news_of_the_day", "good_news", "humor", "research"][:n] + ["digest"]
        return {"posts": [{"rubric": r, "text": f"<b>Пост {i}</b>\n\nТекст <a href='https://src/{i}'>источник</a>",
                           "image": f"https://img/{i}.jpg", "source": f"https://src/{i}"} for i, r in enumerate(rubrics)]}

    def test_daily_saves_file_in_order_with_random_times(self):
        os.mkdir("drafts")
        self.replies = [completion(tool_calls=[("fetch_page", {"url": "https://src/1"})]),
                        completion(json.dumps(self.daily_posts(), ensure_ascii=False))]
        out = os.path.join(self.dir.name, "out")
        os.environ.update(TASK='{"kind": "daily"}', GITHUB_OUTPUT=out)
        self.addCleanup(os.environ.pop, "GITHUB_OUTPUT")
        times = ["09:20", "10:40", "12:05", "14:30", "16:10", "18:55", "21:35"]
        with mock.patch("ai.fetch_page", return_value={"title": "T"}), \
                mock.patch("ai.random_times", return_value=times) as rt:
            ai.main()
        rt.assert_called_once_with(7, "2026-10-02T10:00", start="07:00", end="21:00")
        self.assertEqual(open(out).read(), "file=drafts/2026-10-02.json\n")
        drafts = json.load(open("drafts/2026-10-02.json", encoding="utf-8"))
        self.assertEqual([d["time"] for d in drafts], times + ["22:00"])  # the model's order: most relevant first
        self.assertEqual([d["text"][:9] for d in drafts], [f"<b>Пост {i}" for i in range(8)])  # 7: the digest
        self.assertEqual(drafts[0], {"text": "<b>Пост 0</b>\n\nТекст <a href='https://src/0'>источник</a>",
                                     "media": "https://img/0.jpg", "media_type": "photo", "time": "09:20",
                                     "suggested": True})
        first = self.bodies()[0]
        self.assertEqual(first["response_format"]["type"], "json_schema")
        self.assertTrue(first["response_format"]["json_schema"]["strict"])
        self.assertEqual(first["response_format"]["json_schema"]["schema"], ai.DAILY_SCHEMA)
        self.assertEqual(first["plugins"], [{"id": "web", "max_results": 8}])
        self.assertEqual([t["function"]["name"] for t in first["tools"]], ["fetch_page"])
        task = first["messages"][1]["content"]
        self.assertIn("positive", task)
        self.assertIn("exactly 7", task)
        self.assertIn("artificial intelligence", task)
        self.assertIn("by relevance", task)
        self.assertIn("digest", task)
        self.assertEqual(self.sent, [])  # sending is send.yml's job
        self.assertEqual(self.calls, [])

    def test_daily_does_not_overwrite_and_caps_at_7(self):
        os.mkdir("drafts")
        open("drafts/2026-10-02.json", "w").write("[]")
        posts = self.daily_posts()["posts"]
        self.replies = [completion(json.dumps({"posts": [posts[-1]] + posts[:-1] * 2}))]  # the digest first
        path = ai.daily()
        self.assertEqual(path, "drafts/2026-10-02-2.json")
        drafts = json.load(open(path))
        self.assertEqual(len(drafts), 8)
        self.assertTrue(drafts[0]["text"].startswith("<b>Пост 0</b>"))
        self.assertTrue(drafts[6]["text"].startswith("<b>Пост 6</b>"))
        self.assertTrue(drafts[-1]["text"].startswith("<b>Пост 7</b>"))  # the digest, always last
        self.assertEqual(drafts[-1]["time"], "22:00")
        self.assertEqual([d["time"] for d in drafts], sorted(d["time"] for d in drafts))

    def test_morning_part_is_for_tomorrow(self):
        os.mkdir("drafts")
        self.replies = [completion(json.dumps(self.daily_posts()))]
        os.environ.update(TASK="", SCHEDULE=ai.EVENING_CRON)  # the 21:00 run
        self.addCleanup(os.environ.pop, "SCHEDULE")
        ai.main()
        drafts = json.load(open("drafts/2026-10-03-am.json"))
        self.assertEqual([d["text"][:9] for d in drafts], [f"<b>Пост {i}" for i in range(3)])  # no digest
        times = [d["time"] for d in drafts]
        self.assertTrue("07:00" <= times[0] and times[-1] <= "11:00" and times == sorted(times), times)
        task = self.bodies()[0]["messages"][1]["content"]
        self.assertIn("exactly 3", task)
        self.assertIn("tomorrow morning", task)
        self.assertIn("no digest", task)
        ai.main()  # the morning part for tomorrow is there: nothing to do
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(ai.drafts_exist("2026-10-03"))  # the day part is still to come

    def test_day_part_after_the_morning_one(self):
        os.mkdir("drafts")
        open("drafts/2026-10-02-am.json", "w").write("[]")  # sent last evening
        self.replies = [completion(json.dumps(self.daily_posts()))]
        os.environ["TASK"] = ""  # the 09:00 run
        ai.main()
        drafts = json.load(open("drafts/2026-10-02.json"))
        self.assertEqual(len(drafts), 5)  # 4 news and the digest
        self.assertTrue(drafts[-1]["text"].startswith("<b>Пост 7</b>"))
        self.assertEqual(drafts[-1]["time"], "22:00")
        times = [d["time"] for d in drafts[:-1]]
        self.assertTrue("12:00" <= times[0] and times[-1] <= "21:00" and times == sorted(times), times)
        self.assertIn("exactly 4", self.bodies()[0]["messages"][1]["content"])

    def test_daily_error_tells_nina(self):
        self.replies = [completion("не JSON")]
        os.environ["TASK"] = '{"kind": "daily"}'
        with self.assertRaises(Exception):
            ai.main()
        self.assertEqual(self.calls[0][1]["text"], "Сегодня черновики не собрались: нейросеть вернула ответ не в том формате")

    def test_scheduled_skips_when_drafts_exist(self):
        os.mkdir("drafts")
        open("drafts/2026-10-02-extra.json", "w").write("[]")  # not daily drafts
        self.replies = [completion(json.dumps(self.daily_posts()))]
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

    def test_random_times(self):
        import random
        from schedule import random_times
        mins = lambda ts: [int(t[:2]) * 60 + int(t[3:]) for t in ts]
        seen = set()
        for seed in range(300):
            got = random_times(7, "2026-10-02T05:00", random.Random(seed))
            m = mins(got)
            self.assertEqual(len(m), 7)
            self.assertTrue(7 * 60 <= m[0] and m[-1] <= 21 * 60, got)
            self.assertTrue(all(b - a >= 60 for a, b in zip(m, m[1:])), got)
            self.assertTrue(all(x % 5 == 0 for x in m), got)
            seen.add(tuple(got))
        self.assertGreater(len(seen), 250)  # really random
        late = mins(random_times(7, "2026-10-02T15:02", random.Random(1)))  # a run by hand in the afternoon
        self.assertGreaterEqual(late[0], 15 * 60 + 20)
        self.assertLessEqual(late[-1], 21 * 60)
        from schedule import digest_time
        self.assertEqual(digest_time("20:40"), "22:00")
        self.assertEqual(digest_time("22:15"), "22:20")  # a late run: still after the news

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
        self.assertEqual([json.loads(l) for l in open("images.jsonl")], [{"msg": 77, "prompt": "A robot cooking pasta, warm light."}])

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
        open("images.jsonl", "w").write('{"msg": 7, "prompt": "Old"}\n{"msg": 8, "prompt": "Other"}\n{"msg": 7, "prompt": "A robot"}\n')
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


    # --- review fixes: costs, long voice, failures --------------------------
    def test_cost_of_each_request_is_recorded(self):
        reply = completion("Просто ответ.")
        reply["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost_rub": 1.25, "cost": 1.25}
        self.replies = [reply]
        os.environ["TASK"] = json.dumps({"kind": "chat", "text": "привет"})
        with mock.patch("costs.now_msk", lambda: "2026-10-02T10:00"):
            ai.main()
        self.assertEqual([json.loads(l) for l in open("costs.jsonl")],
                         [{"t": "2026-10-02T10:00", "task": "chat", "model": polza.MODEL, "rub": 1.25}])

    def test_cost_from_transcription_and_media_and_missing_price(self):
        self.replies = [{"text": "ок", "usage": {"cost_rub": "0.4"}},
                        {"id": "m", "status": "completed", "cost": 3, "output": {"url": "https://x/p.png"}},
                        completion("без цены")]
        polza.transcribe(b"OggS")
        polza.media(polza.IMAGE_MODEL, {"prompt": "x"})
        polza.chat([{"role": "user", "content": "x"}])
        self.assertEqual([json.loads(l)["rub"] for l in open("costs.jsonl")], [0.4, 3.0, None])

    def test_cost_report_today_and_month(self):
        rows = [("2026-09-30T10:00", "daily", 50), ("2026-10-01T09:00", "daily", 40.5),
                ("2026-10-02T09:00", "voice", 2.25), ("2026-10-02T09:05", "image", 3), ("2026-10-02T09:06", "chat", None)]
        with open("costs.jsonl", "w") as f:
            for t, task, rub in rows:
                f.write(json.dumps({"t": t, "task": task, "model": "m", "rub": rub}) + "\n")
            f.write("broken line\n")
        text = costs.report("2026-10-02T12:00")
        self.assertIn("Сегодня, 02.10: 5,25 ₽, запросов: 3", text)
        self.assertIn("картинки: 3,00 ₽", text)
        self.assertIn("без цены в ответе Polza: 1", text)
        self.assertIn("Октябрь 2026: 45,75 ₽, запросов: 4", text)
        self.assertIn("черновики на день: 40,50 ₽", text)
        self.assertNotIn("50,00", text)

    def test_long_voice_is_cut_into_pieces(self):
        self.replies = [{"text": "первая часть"}, {"text": ""}, {"text": "третья"}]
        with mock.patch("polza.split_audio", return_value=[b"A", b"B", b"C"]) as split:
            self.assertEqual(polza.transcribe(b"OggS", duration=25 * 60), "первая часть третья")
        split.assert_called_once_with(b"OggS", 600)
        self.assertEqual([json.loads(r.content)["file"][:20] for r in self.requests], ["data:audio/mpeg;base"] * 3)

    def test_short_voice_is_sent_whole(self):
        self.replies = [{"text": "коротко"}]
        with mock.patch("polza.split_audio") as split:
            polza.transcribe(b"OggS", duration=9 * 60)
        split.assert_not_called()

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "needs ffmpeg")
    def test_split_audio_with_ffmpeg(self):
        import subprocess
        ogg = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=25",
                              "-c:a", "libopus", "-f", "ogg", "pipe:1"], capture_output=True, check=True).stdout
        parts = polza.split_audio(ogg, 10)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p) > 1000 for p in parts))

    def test_long_transcript_is_sent_in_parts(self):
        words = " ".join(["слово"] * 1500)  # ~9000 characters
        self.replies = [{"text": words}, completion(tool_calls=[("send_drafts", {"posts": [POST]})]), completion("Готово.")]
        self.voice_task(edit={"kind": "edit", "msg": 7, "html": "<b>Старый</b>", "day": "2026-10-02", "at": "2026-10-02T18:00"})
        parts = self.texts()[:-1]
        self.assertGreater(len(parts), 2)
        self.assertTrue(all(len(p) <= 4096 for p in parts))
        self.assertEqual(" ".join(parts).replace("Расшифровка:\n", ""), words)
        self.assertEqual((self.sent[0][0]["time"], self.sent[0][0]["suggested"]), ("18:00", True))  # keeps its time

    def test_long_reason_is_one_short_line(self):
        self.replies = [{"text": "очень\nдлинно " * 400}]
        self.voice_task(reason="Пост")
        line = open("feedback.md", encoding="utf-8").read().splitlines()[-1]
        self.assertLess(len(line), 1100)
        self.assertLess(len(self.texts()[0]), 400)

    def test_voice_post_uses_slot_from_poll(self):
        sent = self.voice_post_setup()
        self.replies = [{"text": "роботы"}, completion(json.dumps(self.VOICE_POST)),
                        {"id": "m1", "status": "completed", "output": {"url": "https://x/p.png"}}]
        self.voice_task(at="2026-10-02T21:00")
        self.assertEqual((sent[0][0]["time"], sent[0][1]), ("21:00", "2026-10-02"))

    def test_too_big_file(self):
        os.environ["TASK"] = json.dumps({"kind": "voice", "file_id": "F1"})
        with mock.patch("ai.download", side_effect=RuntimeError("getFile: Bad Request: file is too big")):
            with self.assertRaises(RuntimeError):
                ai.main()
        self.assertEqual(self.texts(), [tg.TOO_BIG])

    def test_deadline_tells_nina_and_restores_buttons_with_time(self):
        os.environ["TASK"] = json.dumps({"kind": "edit", "msg": 7, "html": "x", "day": "2026-10-02",
                                         "at": "2026-10-02T15:00", "instructions": "короче"})
        with mock.patch("ai.run", side_effect=ai.Deadline()), self.assertRaises(ai.Deadline):
            ai.main()
        self.assertIn("Не уложилась", self.texts()[0])
        self.assertIn("at:2026-10-02T15:00", str(self.calls[-1]))  # buttons back, at the post's own time

    def test_polza_down_and_busy_messages(self):
        for status, text in [(503, ai.POLZA_DOWN), (429, ai.POLZA_BUSY)]:
            self.calls.clear()
            polza._client._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={})))
            polza._client.max_retries = 0
            os.environ["TASK"] = json.dumps({"kind": "chat", "text": "x"})
            with self.assertRaises(Exception):
                ai.main()
            self.assertEqual(self.texts(), [text])
        polza._client._client = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("no"))))
        self.calls.clear()
        with self.assertRaises(Exception):
            ai.main()
        self.assertEqual(self.texts(), [ai.POLZA_DOWN])

    def test_telegram_down_while_reporting_does_not_hide_the_error(self):
        def down(method, **p):
            raise tg.Unavailable("Telegram: timeout")
        os.environ["TASK"] = json.dumps({"kind": "chat", "text": "x"})
        with mock.patch("ai.call", down), mock.patch("ai.run", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                ai.main()
        self.assertEqual(ai.problem(tg.Unavailable("x")), ai.TG_DOWN)
    def test_edited_voice_post_is_remembered(self):
        with mock.patch("ai.send", lambda d, day=None: {"message_id": 88}):
            self.replies = [completion(tool_calls=[("send_drafts", {"posts": [POST]})]), completion("Готово.")]
            os.environ["TASK"] = json.dumps({"kind": "edit", "msg": 7, "html": "<b>Старый</b>", "day": "2026-10-02",
                                             "image": True, "instructions": "короче"})
            ai.main()
            self.assertEqual(open("voice_edited.jsonl").read(), '{"msg": 88}\n')
            os.environ["TASK"] = json.dumps({"kind": "edit", "msg": 9, "html": "<b>Новость</b>", "day": "2026-10-02",
                                             "instructions": "короче"})
            self.replies = [completion(tool_calls=[("send_drafts", {"posts": [POST]})]), completion("Готово.")]
            ai.main()  # an ordinary news post is not a voice post
            self.assertEqual(open("voice_edited.jsonl").read(), '{"msg": 88}\n')


if __name__ == "__main__":
    unittest.main()
