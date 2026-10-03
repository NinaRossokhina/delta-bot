"""Tests of poll.py logic with a stubbed Telegram API (no network). Run: python -m unittest discover -s tests -v"""
import os, sys, tempfile, unittest
from unittest import mock

os.environ.update(TELEGRAM_BOT_TOKEN="test", ADMIN_CHAT_ID="1", CHANNEL_ID="@delta24news")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import poll, tg
from schedule import draft_buttons

ADMIN = 1


def draft(mid=10, at="2026-10-02T12:00", queued=False):
    kb = draft_buttons(at)["inline_keyboard"]
    return {"message_id": mid, "chat": {"id": ADMIN}, "text": "Заголовок\n\nТекст", "reply_markup": {"inline_keyboard": kb}}


class PollTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd(); os.chdir(self.dir.name)
        self.calls = []
        self.now = "2026-10-02T10:00"
        def fake(method, **p):
            self.calls.append((method, p))
            return {"message_id": 99}
        for target, val in [("poll.call", fake), ("poll.now_msk", lambda: self.now), ("schedule.now_msk", lambda: self.now)]:
            pa = mock.patch(target, val); pa.start(); self.addCleanup(pa.stop)
        poll.AI_ENABLED = False
        self.bot = poll.Bot()

    def tearDown(self):
        os.chdir(self.cwd); self.dir.cleanup()

    def methods(self): return [m for m, _ in self.calls]
    def button(self, mid, data, msg=None):
        self.bot.handle({"callback_query": {"id": "q", "from": {"id": ADMIN}, "data": data, "message": msg or draft(mid)}})
    def reply(self, text, target, uid=ADMIN):
        self.bot.handle({"message": {"message_id": 50, "chat": {"id": uid}, "from": {"id": uid}, "text": text, "reply_to_message": target}})

    def test_time_button_queues_and_answers(self):
        self.button(10, "at:2026-10-02T15:00")
        self.assertEqual(self.bot.queue, [{"msg": 10, "at": "2026-10-02T15:00"}])
        ans = [p for m, p in self.calls if m == "answerCallbackQuery"][0]
        self.assertEqual(ans["text"], "Выйдет 02.10 в 15:00")

    def test_now_publishes_via_copyMessage(self):
        self.button(10, "pub")
        m, p = next(c for c in self.calls if c[0] == "copyMessage")
        self.assertEqual((p["chat_id"], p["from_chat_id"], p["message_id"]), ("@delta24news", ADMIN, 10))

    def test_reject_removes_from_queue(self):
        self.button(10, "at:2026-10-02T15:00"); self.button(10, "rej")
        self.assertEqual(self.bot.queue, [])
        self.assertNotIn("copyMessage", self.methods())

    def test_reply_time_changes_time_of_open_draft(self):
        self.reply("15:30", draft(10))  # an exact time: the post is queued for it
        self.assertEqual(self.bot.queue, [{"msg": 10, "at": "2026-10-02T15:30"}])
        edit = [p for m, p in self.calls if m == "editMessageReplyMarkup"][-1]
        self.assertEqual(edit["reply_markup"]["inline_keyboard"][0][0]["text"], "⏰ 02.10 в 15:30")

    def test_reply_time_moves_queued_post(self):
        self.button(10, "at:2026-10-02T15:00")
        queued = draft(10); queued["reply_markup"] = poll.scheduled_buttons("2026-10-02T15:00")
        queued["reply_markup"]["inline_keyboard"][0].append({"text": "x", "callback_data": "pub"})
        self.reply("16:45", queued)
        self.assertEqual(self.bot.queue, [{"msg": 10, "at": "2026-10-02T16:45"}])

    def test_reply_text_replaces_text(self):
        with mock.patch.object(poll.Bot, "replace_text") as rt:
            self.reply("Новый текст поста", draft(10))
            rt.assert_called_once()

    def test_publish_due_only_when_time_came(self):
        self.bot.queue = [{"msg": 1, "at": "2026-10-02T09:55"}, {"msg": 2, "at": "2026-10-02T10:00"}, {"msg": 3, "at": "2026-10-02T10:01"}]
        self.bot.publish_due()
        published = [p["message_id"] for m, p in self.calls if m == "copyMessage"]
        self.assertEqual(published, [1, 2])
        self.assertEqual(self.bot.queue, [{"msg": 3, "at": "2026-10-02T10:01"}])

    def test_failed_publish_does_not_crash_or_loop(self):
        def boom(method, **p):
            if method == "copyMessage": raise RuntimeError("nope")
            return {"message_id": 99}
        with mock.patch("poll.call", boom):
            self.bot.queue = [{"msg": 1, "at": "2026-10-02T09:00"}]
            self.bot.publish_due()
        self.assertEqual(self.bot.queue, [])

    def test_other_users_ignored(self):
        self.bot.handle({"callback_query": {"id": "q", "from": {"id": 777}, "data": "pub", "message": draft(10)}})
        self.reply("12:00", draft(10), uid=777)
        self.assertNotIn("copyMessage", self.methods())
        self.assertEqual(self.bot.queue, [])

    def test_state_saved_sorted(self):
        self.button(1, "at:2026-10-02T18:00"); self.button(2, "at:2026-10-02T11:00"); self.bot.save()
        self.assertEqual([i["msg"] for i in poll.load("queue.json", [])], [2, 1])

    # --- dialog: «Изменить», «Отклонить», free requests, voice -------------
    def message(self, **m):
        self.bot.handle({"message": {"message_id": 50, "chat": {"id": ADMIN}, "from": {"id": ADMIN}, **m}})

    def ai_on(self):
        poll.AI_ENABLED = True
        self.tasks = []
        pa = mock.patch("poll.run_ai", self.tasks.append); pa.start(); self.addCleanup(pa.stop)

    def asked(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def test_edit_asks_and_answer_goes_to_ai(self):
        self.ai_on()
        self.button(10, "edit")
        self.assertEqual(self.asked(), ["Что поправить? Ответь текстом или голосовым."])
        self.assertEqual(self.bot.pending["99"]["kind"], "edit")
        self.bot.save()
        self.assertIn("99", poll.load("pending.json", {}))  # survives until the next run
        self.bot = poll.Bot(); self.ai_on()
        self.message(text="короче и другой заголовок", reply_to_message={"message_id": 99})
        task = self.tasks[0]
        self.assertEqual((task["kind"], task["msg"], task["instructions"], task["day"]),
                         ("edit", 10, "короче и другой заголовок", "2026-10-02"))
        self.assertNotIn("asked", task)
        self.assertEqual(self.bot.pending, {})

    def test_reject_asks_why_and_saves_dated_reason(self):
        self.button(10, "rej")
        self.assertEqual(self.asked(), ["Почему? Учту в следующих постах. Можно текстом или голосовым."])
        self.message(text="скучная тема")  # answered without tapping "reply"
        self.assertEqual(open("feedback.md", encoding="utf-8").read(), "- 2026-10-02 «Заголовок»: скучная тема\n")
        self.assertEqual(self.bot.pending, {})

    def test_free_text_request_goes_to_ai(self):
        self.ai_on()
        self.message(text="пришли 2 новости про роботов")
        self.assertEqual(self.tasks, [{"kind": "chat", "text": "пришли 2 новости про роботов"}])

    def voice(self, **extra):
        self.message(voice={"file_id": "F1", "mime_type": "audio/ogg", "duration": 3}, **extra)

    def test_voice_request_starts_voice_task(self):
        self.ai_on()
        self.voice()
        self.assertEqual(self.tasks, [{"kind": "voice", "file_id": "F1", "mime": "audio/ogg", "duration": 3,
                                       "at": "2026-10-02T11:00"}])
        self.assertEqual(self.asked(), ["Получила, работаю над постом"])

    def test_audio_file_too(self):
        self.ai_on()
        self.message(audio={"file_id": "A1", "mime_type": "audio/mpeg"})
        self.assertEqual(self.tasks, [{"kind": "voice", "file_id": "A1", "mime": "audio/mpeg", "duration": 0,
                                       "at": "2026-10-02T11:00"}])

    def test_voice_answer_to_edit_is_the_edit(self):
        self.ai_on()
        self.button(10, "edit")
        self.voice(reply_to_message={"message_id": 99})
        task = self.tasks[0]
        self.assertEqual((task["kind"], task["edit"]["kind"], task["edit"]["msg"], task["edit"]["day"]),
                         ("voice", "edit", 10, "2026-10-02"))
        self.assertNotIn("asked", task["edit"])
        self.assertEqual(self.bot.pending, {})
        self.assertIn("Переписываю", str([p for m, p in self.calls if m == "editMessageReplyMarkup"][-1]))

    def test_voice_reply_to_draft_is_an_edit(self):
        self.ai_on()
        self.voice(reply_to_message=draft(10))
        self.assertEqual(self.tasks[0]["edit"]["msg"], 10)

    def test_voice_answer_to_why_is_the_reason(self):
        self.ai_on()
        self.button(10, "rej")
        self.voice()
        self.assertEqual(self.tasks[0]["reason"], "Заголовок")

    def test_voice_without_ai_keeps_question(self):
        self.button(10, "edit")
        self.voice()
        self.assertEqual(self.asked()[-1], poll.NO_AI)
        self.assertIn("99", self.bot.pending)

    # --- posts from voice messages: «Другая картинка» -----------------------
    def image_draft(self, mid=10):
        d = draft(mid); d["reply_markup"] = poll.draft_buttons("2026-10-02T15:00", image=True)
        d["photo"] = [{"file_id": "P"}]; d["caption"] = d.pop("text")
        return d

    def test_other_image_starts_image_task(self):
        self.ai_on()
        d = self.image_draft()
        self.button(10, "img", d)
        task = self.tasks[0]
        self.assertEqual((task["kind"], task["msg"], task["media_type"], task["image"], task["buttons"]),
                         ("image", 10, "photo", True, d["reply_markup"]))
        self.assertIn("Рисую", str([p for m, p in self.calls if m == "editMessageReplyMarkup"][-1]))

    def test_image_button_survives_schedule_and_cancel(self):
        d = self.image_draft()
        self.button(10, "at:2026-10-02T15:00", d)
        markup = [p for m, p in self.calls if m == "editMessageReplyMarkup"][-1]["reply_markup"]
        self.assertIn("ui:2026-10-02T15:00", str(markup))
        self.button(10, "ui:2026-10-02T15:00", {**d, "reply_markup": markup})
        markup = [p for m, p in self.calls if m == "editMessageReplyMarkup"][-1]["reply_markup"]
        self.assertEqual(markup["inline_keyboard"][-1], [{"text": "Другая картинка", "callback_data": "img"}])
        self.assertEqual(self.bot.queue, [])

    def test_next_free_slot_and_marked_button(self):
        from schedule import next_free_slot
        self.assertEqual(next_free_slot(set(), "2026-10-02T10:00"), "2026-10-02T11:00")
        # 11:30 and 12:20 are queued: 11:00-13:00 are less than an hour from them
        self.assertEqual(next_free_slot({"2026-10-02T11:30", "2026-10-02T12:20"}, "2026-10-02T10:00"), "2026-10-02T14:00")
        self.assertEqual(next_free_slot(set(), "2026-10-02T22:00"), "2026-10-03T07:00")
        rows = poll.draft_buttons("2026-10-03T18:00", mark=True)["inline_keyboard"]
        self.assertEqual([[b["text"] for b in row] for row in rows],
                         [["пт, 02.10", "• сб, 03.10", "вс, 04.10"], ["Изменить", "Сейчас, 02.10", "Отклонить"]])
        self.assertEqual([b["callback_data"] for b in rows[0]], ["dt:2026-10-02", "dt:2026-10-03", "dt:2026-10-04"])
        self.assertEqual([b["callback_data"] for b in rows[1]], ["edit", "pub", "rej"])

    def test_past_time_is_refused_not_published(self):
        self.button(10, "at:2026-10-02T09:00")  # it is 10:00: tapping 09:00 must not publish the post now
        self.assertEqual(self.bot.queue, [])
        ans = [p for m, p in self.calls if m == "answerCallbackQuery"][0]
        self.assertTrue(ans["show_alert"])
        self.assertIn("02.10 в 09:00 уже прошло", ans["text"])
        self.bot.publish_due()
        self.assertNotIn("copyMessage", self.methods())
        self.reply("9:30", draft(11))
        self.assertIn("02.10 в 09:30 уже прошло", self.asked()[-1])
        self.assertEqual(self.bot.queue, [])

    def test_reply_with_date_or_tomorrow(self):
        self.reply("завтра 9:00", draft(11))
        self.reply("04.10 15:30", draft(12))
        self.reply("32.10 15:30", draft(13))  # not a date: an edit request (AI is off: the new text)
        self.assertEqual([i["at"] for i in self.bot.queue if i["msg"] in (11, 12)], ["2026-10-03T09:00", "2026-10-04T15:30"])
        self.assertNotIn(13, [i["msg"] for i in self.bot.queue])

    def test_tapping_the_date_explains_and_queues_nothing(self):
        # drafts sent before the date buttons: a date label over the time buttons
        d = draft(10); d["reply_markup"] = {"inline_keyboard": [[{"text": "📅 сб, 03.10", "callback_data": "dl"}],
                                                                [{"text": "• 07:15", "callback_data": "at:2026-10-03T07:15"}]]}
        for data in ("dl", "done"):  # drafts sent before this fix have "done" on the date
            self.calls.clear()
            self.button(10, data, d)
            ans = [p for m, p in self.calls if m == "answerCallbackQuery"][0]
            self.assertTrue(ans["show_alert"])
            self.assertIn("Это только дата: 03.10", ans["text"])
            self.assertIn("«• 07:15»", ans["text"])
        self.assertEqual(self.bot.queue, [])

    def test_date_button_schedules_at_a_random_time_that_day(self):
        d = draft(10, "2026-10-03T09:25"); d["reply_markup"] = poll.draft_buttons("2026-10-03T09:25", mark=True)
        self.button(10, "dt:2026-10-03", d)
        [item] = self.bot.queue
        self.assertEqual(item["msg"], 10)
        self.assertTrue("2026-10-03T07:00" <= item["at"] <= "2026-10-03T21:00", item["at"])
        ans = [p for m, p in self.calls if m == "answerCallbackQuery"][0]
        self.assertEqual(ans["text"], f"Выйдет 03.10 в {item['at'][11:]}")
        done = [p for m, p in self.calls if m == "editMessageReplyMarkup"][-1]["reply_markup"]
        self.assertEqual(done["inline_keyboard"][0][0]["text"], f"⏰ 03.10 в {item['at'][11:]}")
        self.calls.clear()
        self.button(11, "dt:2026-10-01")  # yesterday
        ans = [p for m, p in self.calls if m == "answerCallbackQuery"][0]
        self.assertEqual(ans["text"], "Этот день уже прошёл.")
        self.assertEqual(len(self.bot.queue), 1)

    def test_eight_taps_spread_over_the_day(self):
        for mid in range(20, 28):
            self.button(mid, "dt:2026-10-03")
        times = sorted(i["at"][11:] for i in self.bot.queue)
        self.assertEqual(len(times), 8)
        mins = [int(t[:2]) * 60 + int(t[3:]) for t in times]
        self.assertTrue(all(b - a >= 60 for a, b in zip(mins, mins[1:])), times)
        self.assertTrue(times[0] >= "07:00" and times[-1] <= "22:00", times)
        self.calls.clear()
        for mid in range(40, 60):  # the day fills up: then the bot says so
            self.button(mid, "dt:2026-10-03")
        self.assertIn("На 03.10 свободного времени не осталось", [p for m, p in self.calls if m == "answerCallbackQuery"][-1]["text"])
        self.assertLessEqual(len(self.bot.queue), 16)

    def test_today_date_button_picks_a_later_time(self):
        self.button(10, "dt:2026-10-02")  # it is 10:00
        self.assertGreaterEqual(self.bot.queue[0]["at"], "2026-10-02T10:15")

    def test_cancel_brings_back_the_date_buttons(self):
        d = draft(10); d["reply_markup"] = poll.draft_buttons("2026-10-02T22:00", mark=True, digest=True)
        self.button(10, "dg:2026-10-02", d)
        mark = [p for m, p in self.calls if m == "editMessageReplyMarkup"][-1]["reply_markup"]
        cancel = mark["inline_keyboard"][0][1]["callback_data"]
        self.assertTrue(cancel.startswith("ug:2026-10-02T22:"))
        self.button(10, cancel, {**d, "reply_markup": mark})
        self.assertEqual(self.bot.queue, [])
        rows = [p for m, p in self.calls if m == "editMessageReplyMarkup"][-1]["reply_markup"]["inline_keyboard"]
        self.assertEqual([b["callback_data"] for b in rows[0]], ["dg:2026-10-02", "dg:2026-10-03", "dg:2026-10-04"])
        self.assertEqual(rows[0][0]["text"], "• пт, 02.10")

    def test_time_less_than_an_hour_from_another_post_is_refused(self):
        self.bot.queue = [{"msg": 1, "at": "2026-10-02T15:00"}]
        self.button(10, "at:2026-10-02T15:40")
        self.assertEqual(self.bot.queue, [{"msg": 1, "at": "2026-10-02T15:00"}])  # not queued
        ans = [p for m, p in self.calls if m == "answerCallbackQuery"][0]
        self.assertTrue(ans["show_alert"])
        self.assertIn("В 15:00 уже стоит пост", ans["text"])
        self.assertNotIn("editMessageReplyMarkup", self.methods())  # the draft keeps its buttons
        self.button(10, "at:2026-10-02T16:00")  # exactly an hour later is fine
        self.assertIn({"msg": 10, "at": "2026-10-02T16:00"}, self.bot.queue)
        self.button(10, "at:2026-10-02T16:30")  # moving the same post: only the others count
        self.assertIn({"msg": 10, "at": "2026-10-02T16:30"}, self.bot.queue)
        self.calls.clear()
        self.reply("15:20", draft(11))  # a reply with a time too
        self.assertEqual(self.asked(), ["В 15:00 уже стоит пост, а между постами нужен хотя бы час. Выбери другое время."])
        self.assertFalse(any(i["msg"] == 11 for i in self.bot.queue))


    # --- review fixes -------------------------------------------------------
    def test_cost_command(self):
        open("costs.jsonl", "w").write('{"t": "2026-10-02T09:00", "task": "voice", "model": "m", "rub": 2.5}\n')
        with mock.patch("costs.now_msk", lambda: self.now):
            self.message(text="/cost")
        self.assertIn("Сегодня, 02.10: 2,50 ₽", self.asked()[0])
        self.assertIn("Октябрь 2026: 2,50 ₽", self.asked()[0])

    def test_cost_command_does_not_answer_open_question(self):
        self.button(10, "rej")
        self.message(text="/cost@DeltaRossBot")
        self.assertIn("99", self.bot.pending)  # «Почему?» is still waiting
        self.assertFalse(os.path.exists("feedback.md"))

    def test_voices_in_a_row_get_different_times(self):
        self.ai_on()
        self.bot.queue = [{"msg": 1, "at": "2026-10-02T12:00"}]
        self.voice(); self.voice(); self.voice()
        self.assertEqual([t["at"] for t in self.tasks], ["2026-10-02T11:00", "2026-10-02T13:00", "2026-10-02T14:00"])
        self.bot.save()
        self.assertEqual(poll.Bot().slots, ["2026-10-02T11:00", "2026-10-02T13:00", "2026-10-02T14:00"])

    def test_long_voice_warns_and_too_big_is_refused(self):
        self.ai_on()
        self.message(voice={"file_id": "F", "duration": 14 * 60 + 5, "file_size": 3_000_000})
        self.assertIn("длинное (14 мин)", self.asked()[-1])
        self.message(voice={"file_id": "G", "duration": 3600, "file_size": 25_000_000})
        self.assertEqual(self.asked()[-1], tg.TOO_BIG)
        self.assertEqual(len(self.tasks), 1)

    def test_edit_of_queued_post_takes_it_out_of_queue(self):
        self.ai_on()
        self.bot.queue = [{"msg": 10, "at": "2026-10-02T15:00"}]
        d = draft(10); d["reply_markup"] = poll.scheduled_buttons("2026-10-02T15:00")
        self.reply("сделай короче", d)
        self.assertEqual(self.bot.queue, [])
        self.assertEqual(self.tasks[0]["at"], "2026-10-02T15:00")

    def test_telegram_down_keeps_due_post_in_queue(self):
        def down(method, **p):
            raise tg.Unavailable("Telegram: HTTP 502")
        with mock.patch("poll.call", down):
            self.bot.queue = [{"msg": 1, "at": "2026-10-02T09:00"}]
            self.bot.publish_due()
        self.assertEqual(self.bot.queue, [{"msg": 1, "at": "2026-10-02T09:00"}])

    def test_published_but_label_failed_is_not_published_again(self):
        def flaky(method, **p):
            if method == "editMessageReplyMarkup": raise tg.Unavailable("timeout")
            self.calls.append((method, p)); return {}
        with mock.patch("poll.call", flaky):
            self.bot.queue = [{"msg": 1, "at": "2026-10-02T09:00"}]
            self.bot.publish_due()
        self.assertEqual(self.bot.queue, [])
        self.assertEqual(self.methods().count("copyMessage"), 1)

    def test_telegram_down_mid_run_confirms_only_handled_updates(self):
        self.ai_on()
        updates = [{"update_id": 5, "callback_query": {"id": "a", "from": {"id": ADMIN}, "data": "at:2026-10-02T15:00", "message": draft(10)}},
                   {"update_id": 6, "callback_query": {"id": "b", "from": {"id": ADMIN}, "data": "at:2026-10-02T18:00", "message": draft(11)}},
                   {"update_id": 7, "message": {"message_id": 3, "chat": {"id": ADMIN}, "text": "пост про роботов"}}]
        def fake(method, **p):
            if method == "getUpdates": return updates
            if method == "editMessageReplyMarkup" and p["message_id"] == 11: raise tg.Unavailable("Telegram: HTTP 502")
            self.calls.append((method, p)); return {"message_id": 99}
        with mock.patch("poll.call", fake):
            poll.main()
        self.assertEqual(open(".confirm_offset").read(), "6")  # 6 and 7 come again next run
        self.assertEqual(poll.load("queue.json", []), [{"msg": 10, "at": "2026-10-02T15:00"}])  # 11 not half-done
        self.assertEqual(self.tasks, [])

    def test_task_started_then_telegram_fails_is_not_repeated(self):
        self.ai_on()
        updates = [{"update_id": 8, "message": {"message_id": 3, "chat": {"id": ADMIN}, "voice": {"file_id": "F"}}}]
        def fake(method, **p):
            if method == "getUpdates": return updates
            raise tg.Unavailable("Telegram: timeout")
        with mock.patch("poll.call", fake):
            poll.main()
        self.assertEqual(len(self.tasks), 1)
        self.assertEqual(open(".confirm_offset").read(), "9")
    # --- learning: an edited voice post goes to style/examples.md when published ----
    def edited_post(self, mid=10):
        open("voice_edited.jsonl", "w").write(f'{{"msg": {mid}}}\n')
        os.mkdir("style"); open("style/examples.md", "w", encoding="utf-8").write("# Образцы постов\n\n## 1. Пост 3\n\nТекст\n")
        self.bot = poll.Bot()
        d = self.image_draft(mid)
        d["caption"] = "Роботы научились готовить\n\nТеперь & навсегда."
        d["caption_entities"] = [{"type": "bold", "offset": 0, "length": 25}]
        return d

    def test_edited_voice_post_published_now_goes_to_examples(self):
        self.button(10, "pub", self.edited_post())
        text = open("style/examples.md", encoding="utf-8").read()
        self.assertTrue(text.endswith("## 2. Голосовой пост после правок · 2026-10-02\n\n"
                                      "_Финальная версия, которую Нина опубликовала после правок._\n\n"
                                      "Роботы научились готовить\n\nТеперь & навсегда.\n"), text)

    def test_edited_voice_post_published_on_time_goes_to_examples(self):
        self.button(10, "at:2026-10-02T12:00", self.edited_post())
        self.bot.save(); self.bot = poll.Bot()  # next run
        self.now = "2026-10-02T12:00"
        self.bot.publish_due()
        self.assertIn("Роботы научились готовить\n\nТеперь & навсегда.", open("style/examples.md", encoding="utf-8").read())

    def test_unedited_post_does_not_go_to_examples(self):
        d = self.edited_post(mid=10)
        d["message_id"] = 11
        self.button(11, "pub", d)
        self.button(11, "at:2026-10-02T09:00", d); self.bot.publish_due()
        self.assertEqual(open("style/examples.md", encoding="utf-8").read().count("## "), 1)


if __name__ == "__main__":
    unittest.main()
