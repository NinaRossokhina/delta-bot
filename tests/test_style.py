"""Tests of style/build_style.py and style/analyze_style.py with a stubbed Polza API (no network)."""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import httpx
from openai import OpenAI

os.environ.setdefault("POLZA_API_KEY", "test-key")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "style")]
import analyze_style, build_style, costs, polza

EXPORT = {"name": "Канал", "type": "public_channel", "messages": [
    {"id": 1, "type": "service", "date": "2026-01-01T10:00:00", "action": "create_channel", "text": ""},
    {"id": 2, "type": "message", "date": "2026-01-02T10:00:00", "text": "Простой пост.\n\nВторой абзац."},
    {"id": 3, "type": "message", "date": "2026-01-03T10:00:00",
     "text": [{"type": "bold", "text": "Заголовок"}, "\n\nТекст и ", {"type": "text_link", "text": "ссылка",
              "href": "https://example.com"}, "."]},
    {"id": 4, "type": "message", "date": "2026-01-04T10:00:00", "forwarded_from": "Другой канал", "text": "Репост"},
    {"id": 5, "type": "message", "date": "2026-01-05T10:00:00", "photo": "photos/a.jpg", "text": ""},
    {"id": 6, "type": "message", "date": "2026-01-06T10:00:00", "text": ["  ", {"type": "plain", "text": " "}]},
]}


class StyleTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)
        pa = mock.patch("costs.FILE", str(self.path / "costs.jsonl")); pa.start(); self.addCleanup(pa.stop)

    def tearDown(self):
        self.dir.cleanup()

    def test_posts_keep_own_text_posts_only(self):
        self.assertEqual(build_style.posts(EXPORT), [
            (2, "2026-01-02", "Простой пост.\n\nВторой абзац."),
            (3, "2026-01-03", "Заголовок\n\nТекст и ссылка.")])

    def test_posts_txt_round_trip(self):
        build_style.write(build_style.posts(EXPORT), self.path / "posts.txt")
        self.assertEqual(build_style.read(self.path / "posts.txt"), build_style.posts(EXPORT))

    def test_sample_fits_limit(self):
        items = [(i, "", "x" * 100) for i in range(100)]
        self.assertEqual(analyze_style.sample(items, 20_000), items)
        self.assertLessEqual(sum(len(t) for *_, t in analyze_style.sample(items, 2_500)), 2_500)

    def test_analyze_writes_voice_and_verbatim_examples(self):
        reply = {"voice": "# Мой голос\n\n## Тон\nЛёгкий.", "examples": [
            {"id": 3, "why": "заголовок и ссылка"}, {"id": 99, "why": "нет такого"}]}
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": polza.MODEL,
                                             "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                                 "role": "assistant", "content": json.dumps(reply, ensure_ascii=False)}}]})
        polza._client = OpenAI(base_url=polza.BASE_URL, api_key="test-key",
                               http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(setattr, polza, "_client", None)
        build_style.write(build_style.posts(EXPORT), self.path / "posts.txt")
        analyze_style.HERE = self.path
        self.addCleanup(setattr, analyze_style, "HERE", build_style.HERE)
        analyze_style.main()
        self.assertEqual(requests[0]["model"], polza.MODEL)
        self.assertEqual(requests[0]["response_format"]["type"], "json_schema")
        self.assertIn("[пост 3 · 2026-01-03]\nЗаголовок", requests[0]["messages"][1]["content"])
        self.assertEqual((self.path / "my-voice.md").read_text(encoding="utf-8"), "# Мой голос\n\n## Тон\nЛёгкий.\n")
        examples = (self.path / "examples.md").read_text(encoding="utf-8")
        self.assertIn("## 1. Пост 3 · 2026-01-03\n\n_заголовок и ссылка_\n\nЗаголовок\n\nТекст и ссылка.", examples)
        self.assertNotIn("99", examples)

    def test_learned_examples_survive_regeneration(self):
        old = ("# Образцы\n\n## 1. Пост 3 · 2026-10-01\n\nСтарый\n\n"
               "## 2. Голосовой пост после правок · 2026-10-02\n\n_Финальная версия._\n\nРоботы готовят\n")
        new = "# Образцы\n\n## 1. Пост 5 · 2026-10-01\n\nА\n\n## 2. Пост 6 · 2026-10-01\n\nБ\n"
        out = analyze_style.keep_learned(new, old)
        self.assertTrue(out.endswith("## 3. Голосовой пост после правок · 2026-10-02\n\n_Финальная версия._\n\nРоботы готовят\n"), out)
        self.assertNotIn("Старый", out)


if __name__ == "__main__":
    unittest.main()
