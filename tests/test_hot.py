"""Tests of hot.py (hot news) with stubbed sources, Polza and Telegram (no network). Run: python -m unittest discover -s tests -v"""
import json, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import httpx
from openai import OpenAI

os.environ.update(TELEGRAM_BOT_TOKEN="test", ADMIN_CHAT_ID="1", POLZA_API_KEY="test-key")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import costs, hot, polza
from schedule import next_free_slot

NOW = datetime.now(timezone.utc)
RFC = lambda d: d.strftime("%a, %d %b %Y %H:%M:%S +0000")
RSS = f"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Introducing GPT-6</title><link>https://openai.com/index/gpt-6/</link><guid>gpt-6</guid><pubDate>{RFC(NOW)}</pubDate></item>
<item><title>Old customer story</title><link>https://openai.com/index/old/</link><pubDate>{RFC(NOW - timedelta(days=3))}</pubDate></item>
</channel></rss>"""
ATOM = f"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Gemini 4 is here</title>
<link href="https://blog.google/gemini-4/"/><id>tag:g4</id><updated>{NOW.isoformat()}</updated></entry></feed>"""
ANTHROPIC = """<a href="/news/claude-opus-6" class="card"><h3 class="title">Introducing Claude Opus 6</h3></a>
<a href="/news/barclays-scales-claude"><span>x</span></a><a href="/news/claude-opus-6">again</a><a href="/careers">jobs</a>"""
HN = json.dumps({"hits": [
    {"objectID": "1", "title": "DeepSeek V5 released", "url": "https://deepseek.com/v5", "points": 900, "created_at_i": int(NOW.timestamp())},
    {"objectID": "2", "title": "A small AI side project", "url": "https://x.dev", "points": 40, "created_at_i": int(NOW.timestamp())},
    {"objectID": "3", "title": "My favourite bread recipe", "url": "https://bread.example", "points": 800, "created_at_i": int(NOW.timestamp())},
]})
PAGES = {"rss": RSS, "anthropic": ANTHROPIC, "hn": HN, "atom": ATOM}
SOURCES = [("OpenAI", "rss", "rss", False), ("Anthropic", "anthropic", "anthropic", False),
           ("Hacker News", "hn", "hn", True), ("Google AI", "rss", "atom", False)]


def fetch(url):
    return PAGES[url]


class ParseTest(unittest.TestCase):
    def test_rss_and_atom(self):
        rss = hot.parse_feed(RSS)
        self.assertEqual([(x["title"], x["url"], x["id"]) for x in rss],
                         [("Introducing GPT-6", "https://openai.com/index/gpt-6/", "gpt-6"),
                          ("Old customer story", "https://openai.com/index/old/", "https://openai.com/index/old/")])
        self.assertLess(abs((rss[0]["published"] - NOW).total_seconds()), 2)
        atom = hot.parse_feed(ATOM)
        self.assertEqual((atom[0]["title"], atom[0]["url"], atom[0]["id"]), ("Gemini 4 is here", "https://blog.google/gemini-4/", "tag:g4"))
        self.assertIsNotNone(atom[0]["published"])

    def test_anthropic_page(self):
        self.assertEqual([(x["title"], x["url"]) for x in hot.parse_anthropic(ANTHROPIC)],
                         [("Introducing Claude Opus 6", "https://www.anthropic.com/news/claude-opus-6"),
                          ("Barclays scales claude", "https://www.anthropic.com/news/barclays-scales-claude")])

    def test_hn_points_and_keywords(self):
        self.assertEqual([x["id"] for x in hot.parse_hn(HN)], ["hn:1", "hn:3"])
        items = hot.collect(SOURCES, fetch)
        titles = [x["title"] for x in items]
        self.assertIn("DeepSeek V5 released", titles)
        self.assertNotIn("My favourite bread recipe", titles)  # popular, but not about AI
        self.assertIn("Barclays scales claude", titles)  # lab blogs are not filtered: the cheap model decides
        self.assertIn("OpenAI:gpt-6", [x["id"] for x in items])

    def test_broken_source_is_skipped(self):
        def flaky(url):
            if url == "rss":
                raise OSError("down")
            return PAGES[url]
        self.assertNotIn("OpenAI", {x["source"] for x in hot.collect(SOURCES, flaky)})

    def test_dates(self):
        self.assertEqual(hot.when("Fri, 02 Oct 2026 10:00:00 GMT"), datetime(2026, 10, 2, 10, tzinfo=timezone.utc))
        self.assertEqual(hot.when("2026-10-02T10:00:00Z"), datetime(2026, 10, 2, 10, tzinfo=timezone.utc))
        self.assertIsNone(hot.when("вчера"))


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd(); os.chdir(self.dir.name)
        os.makedirs("drafts"); os.makedirs(hot.STATE)
        open("style-guide.md", "w", encoding="utf-8").write("Пиши коротко.")
        open("queue.json", "w").write("[]")
        self.sent, self.calls, self.requests, self.replies = [], [], [], []

        def handler(request):
            self.requests.append(request)
            return httpx.Response(200, json=self.replies.pop(0))
        client = OpenAI(base_url=polza.BASE_URL, api_key="test-key", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        for target, val in [("polza._client", client), ("hot.send", lambda d, day=None: self.sent.append((d, day))),
                            ("hot.call", lambda m, **p: self.calls.append((m, p))),
                            ("ai.call", lambda m, **p: self.calls.append((m, p))),
                            ("hot.now_msk", lambda: "2026-10-02T10:00"), ("ai.now_msk", lambda: "2026-10-02T10:00")]:
            pa = mock.patch(target, val); pa.start(); self.addCleanup(pa.stop)

    def tearDown(self):
        os.chdir(self.cwd); self.dir.cleanup()

    def remember_all_but(self, *ids):
        hot.save(hot.SEEN, {x["id"]: "2026-10-01T10:00" for x in hot.collect(SOURCES, fetch) if x["id"] not in ids})

    def test_first_run_only_remembers(self):
        with mock.patch("hot.rate") as rate:
            self.assertEqual(hot.check(SOURCES, fetch), [])
        rate.assert_not_called()
        self.assertIn("OpenAI:gpt-6", hot.load(hot.SEEN, {}))
        self.assertEqual(self.sent, [])

    def test_nothing_new_costs_nothing(self):
        self.remember_all_but()
        with mock.patch("hot.rate") as rate:
            hot.check(SOURCES, fetch)
        rate.assert_not_called()

    def test_hot_story_becomes_a_draft(self):
        self.remember_all_but("OpenAI:gpt-6", "Anthropic:https://www.anthropic.com/news/barclays-scales-claude")
        post = {"skip": False, "text": "<b>GPT-6</b>\n\nТекст", "image": "https://openai.com/g.png", "source": "https://openai.com/index/gpt-6/"}
        with mock.patch("hot.rate", return_value={0: (10, False), 1: (3, False)}) as rate, \
                mock.patch("hot.write_post", return_value=post) as write:
            paths = hot.check(SOURCES, fetch)
        self.assertEqual([x["title"] for x in rate.call_args[0][0]], ["Introducing GPT-6", "Barclays scales claude"])
        write.assert_called_once()
        self.assertEqual(write.call_args[0][0]["title"], "Introducing GPT-6")
        self.assertEqual(paths, ["drafts/2026-10-02-hot.json"])
        self.assertEqual(self.calls[0][1]["text"], hot.INTRO)
        self.assertEqual(self.sent, [({"text": post["text"], "media": post["image"], "media_type": "photo",
                                       "time": next_free_slot(set(), "2026-10-02T10:00")[11:], "suggested": True}, "2026-10-02")])
        self.assertEqual(json.load(open(paths[0], encoding="utf-8"))[0]["text"], post["text"])
        self.assertIn("OpenAI:gpt-6", hot.load(hot.SEEN, {}))
        with mock.patch("hot.rate") as rate:  # the next run does not see it as new
            self.assertEqual(hot.check(SOURCES, fetch), [])
        rate.assert_not_called()

    def test_covered_or_small_stories_are_not_written(self):
        self.remember_all_but("OpenAI:gpt-6", "Hacker News:hn:1")
        with mock.patch("hot.rate", return_value={0: (10, True), 1: (6, False)}), mock.patch("hot.write_post") as write:
            self.assertEqual(hot.check(SOURCES, fetch), [])
        write.assert_not_called()
        self.assertEqual(self.sent, [])

    def test_old_items_are_not_rated(self):
        self.remember_all_but("OpenAI:https://openai.com/index/old/")
        with mock.patch("hot.rate") as rate:
            hot.check(SOURCES, fetch)
        rate.assert_not_called()
        self.assertIn("OpenAI:https://openai.com/index/old/", hot.load(hot.SEEN, {}))

    def test_rating_failure_tries_again_next_time(self):
        self.remember_all_but("OpenAI:gpt-6")
        with mock.patch("hot.rate", side_effect=RuntimeError("Polza: empty answer")):
            hot.check(SOURCES, fetch)
        self.assertNotIn("OpenAI:gpt-6", hot.load(hot.SEEN, {}))
        self.assertEqual(self.calls, [])  # not worth bothering Nina every few minutes

    def test_writing_failure_is_reported_once(self):
        self.remember_all_but("OpenAI:gpt-6")
        with mock.patch("hot.rate", return_value={0: (9, False)}), mock.patch("hot.write_post", side_effect=RuntimeError("boom")):
            self.assertEqual(hot.check(SOURCES, fetch), [])
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Introducing GPT-6", self.calls[0][1]["text"])
        self.assertIn("OpenAI:gpt-6", hot.load(hot.SEEN, {}))

    def test_rate_uses_the_cheap_model(self):
        open("drafts/2026-10-01.json", "w", encoding="utf-8").write(json.dumps([{"text": "<b>Вчерашняя новость</b>\n\nТекст"}]))
        self.replies = [{"id": "x", "object": "chat.completion", "created": 0, "model": hot.CHEAP_MODEL,
                         "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant",
                                     "content": json.dumps({"items": [{"i": 0, "score": 9, "covered": False}, {"i": 7, "score": 9, "covered": False}]})}}]}]
        scores = hot.rate([{"source": "OpenAI", "title": "Introducing GPT-6", "url": "https://openai.com/index/gpt-6/"}])
        self.assertEqual(scores, {0: (9, False)})
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["model"], "anthropic/claude-haiku-4.5")
        self.assertNotIn("plugins", body)  # no web search: it is the cheap step
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertIn("Вчерашняя новость", body["messages"][0]["content"])
        self.assertIn("0. [OpenAI] Introducing GPT-6", body["messages"][0]["content"])

    def test_costs_are_moved_to_costs_jsonl_now_and_then(self):
        with open(hot.PENDING_COSTS, "w") as f:
            f.write(json.dumps({"t": "2026-10-02T08:00", "task": "hot", "model": hot.CHEAP_MODEL, "rub": 0.2}) + "\n")
        self.assertFalse(hot.flush_costs())  # 2 hours old: wait
        self.assertFalse(os.path.exists("costs.jsonl"))
        self.assertTrue(hot.flush_costs(force=True))  # a hot post is committed anyway
        self.assertIn('"rub": 0.2', open("costs.jsonl").read())
        self.assertFalse(os.path.exists(hot.PENDING_COSTS))
        self.assertEqual(costs.TASKS["hot"], "срочные новости")


if __name__ == "__main__":
    unittest.main()
