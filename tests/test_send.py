"""Tests of send_drafts.resend with a stubbed Telegram API (no network)."""
import os, sys, unittest
from unittest import mock

os.environ.update(TELEGRAM_BOT_TOKEN="test", ADMIN_CHAT_ID="1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import send_drafts

BOLD = [{"type": "bold", "offset": 0, "length": 5}]


class ResendTest(unittest.TestCase):
    def test_resend_moves_drafts_under_new_buttons(self):
        messages = {5: {"message_id": 5, "text": "Пост", "entities": BOLD},          # a draft
                    6: {"message_id": 6, "caption": "Фото", "caption_entities": BOLD},  # a draft with a photo
                    8: {"message_id": 8, "text": "Почему?"},                         # the bot's question
                    9: {"message_id": 9, "text": "Дайджест", "entities": BOLD}}      # 7: Nina's own message
        calls = []
        def fake(method, **p):
            calls.append((method, p))
            if method == "editMessageReplyMarkup":
                if p["message_id"] not in messages:
                    raise RuntimeError("message can't be edited")
                return messages[p["message_id"]]
            return {"message_id": 100}
        with mock.patch("send_drafts.call", fake), mock.patch("schedule.now_msk", lambda: "2026-10-03T19:40"):
            send_drafts.resend(5, 9, "2026-10-04")
        copies = [p for m, p in calls if m == "copyMessage"]
        self.assertEqual([p["message_id"] for p in copies], [5, 6, 9])
        rows = copies[0]["reply_markup"]["inline_keyboard"]
        self.assertEqual([b["text"] for b in rows[0]], ["сб, 03.10", "• вс, 04.10", "пн, 05.10"])
        self.assertEqual(rows[0][1]["callback_data"], "dt:2026-10-04")
        self.assertEqual(copies[-1]["reply_markup"]["inline_keyboard"][0][1]["callback_data"], "dg:2026-10-04")
        self.assertEqual([p["message_id"] for m, p in calls if m == "deleteMessage"], [5, 6, 9])
        self.assertIn("постов: 3", [p for m, p in calls if m == "sendMessage"][0]["text"])


if __name__ == "__main__":
    unittest.main()
