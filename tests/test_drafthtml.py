import unittest

from drafthtml import to_html


class ToHtmlTest(unittest.TestCase):
    def test_keeps_blockquote(self):
        text = "Промпт:\nПозвони мне\nКонец"
        message = {"text": text, "entities": [{"type": "blockquote", "offset": 8, "length": 11}]}
        self.assertEqual(to_html(message)[0], "Промпт:\n<blockquote>Позвони мне</blockquote>\nКонец")


if __name__ == "__main__":
    unittest.main()
