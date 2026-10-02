"""Tests of stats.py on a saved-style t.me/s page (no network). Run: python -m unittest discover -s tests -v"""
import json, os, sys, tempfile, unittest
from unittest import mock

os.environ.update(TELEGRAM_BOT_TOKEN="test", ADMIN_CHAT_ID="1", CHANNEL_ID="@delta24news")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stats


def message(mid, when, text, views, reactions=""):
    box = f'<div class="tgme_widget_message_reactions js-message_reactions">{reactions}</div>' if reactions else ""
    return f'''<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="delta24news/{mid}" data-view="x">
<div class="tgme_widget_message_bubble"><div class="tgme_widget_message_text js-message_text" dir="auto"><b>{text}</b><br/><br/>Текст &amp; ещё <a href="https://example.com">источник</a></div>
{box}<div class="tgme_widget_message_footer compact js-message_footer"><div class="tgme_widget_message_info short js-message_info">
<span class="tgme_widget_message_views">{views}</span><span class="copyonly"> views</span><span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" href="https://t.me/delta24news/{mid}"><time datetime="{when}" class="time">12:00</time></a></span>
</div></div></div></div></div>'''


LIKE = '<span class="tgme_reaction"><i class="emoji" style="background-image:url(\'//telegram.org/img/emoji/40/F09F918D.png\')"><b>👍</b></i>{}</span>'
STAR = '<span class="tgme_reaction tgme_reaction_paid"><i class="emoji tgme_reaction_paid_emoji"></i>{}</span>'


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd(); os.chdir(self.dir.name)
        pa = mock.patch("stats.now_msk", lambda: "2026-10-05T07:30"); pa.start(); self.addCleanup(pa.stop)

    def tearDown(self):
        os.chdir(self.cwd); self.dir.cleanup()

    def test_numbers(self):
        self.assertEqual([stats.number(t) for t in ["845", "1.2K", "15K", "1,5M", "", "?"]],
                         [845, 1200, 15000, 1500000, 0, 0])

    def test_parse(self):
        page = message(10, "2026-10-01T06:00:00+00:00", "Робот &laquo;Ф&raquo;", "1.2K", LIKE.format(12) + STAR.format(3))
        self.assertEqual(stats.parse(page), [{"id": 10, "date": "2026-10-01T09:00", "title": "Робот «Ф»",
                                              "views": 1200, "reactions": 15}])

    def test_week_pages_back_until_a_week_ago(self):
        pages = {
            "https://t.me/s/delta24news": message(20, "2026-10-04T10:00:00+00:00", "Новое", "300")
                                          + message(21, "2026-10-04T15:00:00+00:00", "Новейшее", "90", LIKE.format(40)),
            "https://t.me/s/delta24news?before=20": message(18, "2026-09-27T10:00:00+00:00", "Старое", "5K")
                                                   + message(19, "2026-09-29T10:00:00+00:00", "Неделя", "2K", LIKE.format(7)),
        }
        posts = stats.week_posts("delta24news", get=pages.__getitem__)
        self.assertEqual([p["id"] for p in posts], [19, 20, 21])
        text = stats.report(posts, "delta24news")
        self.assertIn("постов 3, просмотров 2390, реакций 47", text)
        views = text.split("<b>Больше всего просмотров</b>\n")[1]
        self.assertTrue(views.startswith('1. <a href="https://t.me/delta24news/19">Неделя</a> — 2000 просм., 7 реакц.'))
        reactions = text.split("<b>Больше всего реакций</b>\n")[1]
        self.assertTrue(reactions.startswith('1. <a href="https://t.me/delta24news/21">Новейшее</a> — 40 реакц.'))

    def test_changed_page_is_an_error(self):
        with self.assertRaises(RuntimeError):
            stats.week_posts("delta24news", get=lambda url: "<html>nothing</html>")

    def test_no_reactions_note(self):
        posts = [{"id": 1, "date": "2026-10-04T10:00", "title": "A", "views": 5, "reactions": 0}]
        self.assertIn("Реакций на странице канала не видно", stats.report(posts, "delta24news"))

    def test_for_prompt_uses_recent_weeks(self):
        rows = [{"week": "2026-08-01", "id": 1, "title": "Давнее", "views": 9999, "reactions": 0}]
        rows += [{"week": "2026-10-05", "id": i, "title": f"Пост {i}", "views": i * 100, "reactions": i} for i in range(2, 16)]
        rows += [{"week": "2026-09-28", "id": 15, "title": "Пост 15", "views": 1, "reactions": 0}]
        with open(stats.STATS, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        text = stats.for_prompt()
        self.assertNotIn("Давнее", text)
        best, worst = text.split("\nLeast read:\n")
        self.assertTrue(best.startswith("Most read:\n- Пост 15 (1500 views, 15 reactions)"))
        self.assertEqual(worst.splitlines()[-1], "- Пост 2 (200 views, 2 reactions)")

    def test_for_prompt_empty_without_stats(self):
        self.assertEqual(stats.for_prompt(), "")


if __name__ == "__main__":
    unittest.main()
