"""Tests of poll.py logic with a stubbed Telegram API (no network). Run: python -m unittest discover -s tests -v"""
import os, sys, tempfile, unittest
from unittest import mock

os.environ.update(TELEGRAM_BOT_TOKEN="test", ADMIN_CHAT_ID="1", CHANNEL_ID="@delta24news")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import poll
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
        for target, val in [("poll.call", fake), ("poll.now_msk", lambda: self.now)]:
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
        self.assertEqual(ans["text"], "Выйдет в 15:00")

    def test_now_publishes_via_copyMessage(self):
        self.button(10, "pub")
        m, p = next(c for c in self.calls if c[0] == "copyMessage")
        self.assertEqual((p["chat_id"], p["from_chat_id"], p["message_id"]), ("@delta24news", ADMIN, 10))

    def test_reject_removes_from_queue(self):
        self.button(10, "at:2026-10-02T15:00"); self.button(10, "rej")
        self.assertEqual(self.bot.queue, [])
        self.assertNotIn("copyMessage", self.methods())

    def test_reply_time_changes_time_of_open_draft(self):
        self.reply("15:30", draft(10))
        edit = [p for m, p in self.calls if m == "editMessageReplyMarkup"][-1]
        self.assertIn("at:2026-10-02T15:30", str(edit["reply_markup"]))

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
        self.button(1, "at:2026-10-02T18:00"); self.button(2, "at:2026-10-02T09:00"); self.bot.save()
        self.assertEqual([i["msg"] for i in poll.load("queue.json", [])], [2, 1])


if __name__ == "__main__":
    unittest.main()
