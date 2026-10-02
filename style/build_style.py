"""Posts of the channel from a Telegram Desktop export (style/result.json) as plain text (style/posts.txt).

Keeps only the channel's own text posts: drops service messages, forwards (reposts) and posts without text.
Run: python style/build_style.py [result.json] [posts.txt]
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEPARATOR = "\n\n=====\n\n"


def plain_text(text):
    """The `text` field is a string or a list of strings and formatted pieces ({"type": "bold", "text": ...})."""
    if isinstance(text, str):
        return text
    return "".join(part if isinstance(part, str) else part.get("text", "") for part in text or [])


def posts(export):
    """[(id, date, text)] of the channel's own non-empty text posts, oldest first."""
    result = []
    for m in export.get("messages", []):
        if m.get("type") != "message" or m.get("forwarded_from") or m.get("saved_from"):
            continue
        text = plain_text(m.get("text")).strip()
        if text:
            result.append((m.get("id"), m.get("date", "")[:10], text))
    return result


def header(post_id, date):
    return f"[пост {post_id} · {date}]"


def write(items, path):
    path.write_text(SEPARATOR.join(f"{header(i, d)}\n{t}" for i, d, t in items) + "\n", encoding="utf-8")


def read(path):
    """[(id, date, text)] back from posts.txt."""
    items = []
    for chunk in path.read_text(encoding="utf-8").split(SEPARATOR):
        head, _, text = chunk.strip().partition("\n")
        post_id, _, date = head.strip("[]").removeprefix("пост ").partition(" · ")
        items.append((int(post_id), date, text.strip()))
    return items


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "result.json"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "posts.txt"
    if not src.exists():
        sys.exit(f"Нет файла {src}: положите туда выгрузку канала из Telegram Desktop (формат JSON)")
    export = json.loads(src.read_text(encoding="utf-8"))
    items = posts(export)
    write(items, dst)
    print(f"{len(export.get('messages', []))} сообщений в выгрузке, {len(items)} постов сохранено в {dst}")


if __name__ == "__main__":
    main()
