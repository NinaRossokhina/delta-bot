"""AI tasks for the Delta bot, run by the "AI tasks" workflow.

Task kinds (JSON in the TASK env var):
  {"kind": "chat", "text": "пришли 2 новые новости"}  - Nina's free-form request
  {"kind": "edit", "msg": 123, "html": "...", "media": "...", "media_type": "photo",
   "day": "2026-10-02", "instructions": "1. короче 2. другой заголовок"}  - rewrite a draft
New or rewritten posts are sent to Nina as drafts with the usual buttons.
"""
import glob
import html
import json
import os
import re
import urllib.request

import polza
from schedule import draft_buttons, now_msk
from send_drafts import send
from tg import call

ADMIN = os.environ["ADMIN_CHAT_ID"]

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Telegram HTML: <b>headline</b>, short paragraphs separated by \\n\\n, <a href='...'>source</a>. Under 1000 characters."},
        "media": {"type": "string", "description": "Direct JPG/PNG image URL, or a page URL when media_type is link. Empty string for none."},
        "media_type": {"type": "string", "enum": ["photo", "video", "link", "none"]},
    },
    "required": ["text", "media", "media_type"],
    "additionalProperties": False,
}
TOOLS = [
    polza.function_tool(
        "fetch_page",
        "Open a web page: returns its title, og:image and main text. Use it to check facts in the primary "
        "source and to find an image for the post.",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False},
    ),
    polza.function_tool(
        "send_drafts",
        "Send finished posts to Nina as drafts for approval. Call it once with all posts.",
        {"type": "object", "properties": {"posts": {"type": "array", "items": POST_SCHEMA}},
         "required": ["posts"], "additionalProperties": False},
    ),
]
PAGE_LIMIT = 8000


def meta(page, prop):
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        if re.search(rf"""(?:property|name)\s*=\s*["']{re.escape(prop)}["']""", tag, re.I):
            m = re.search(r"""content\s*=\s*["']([^"']*)""", tag, re.I)
            if m:
                return html.unescape(m[1])
    return ""


def fetch_page(url):
    """The fetch_page tool: title, og:image and text of a page."""
    if not url.startswith(("http://", "https://")):
        return {"error": "only http(s) URLs"}
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; DeltaBot/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read(2_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace")
    except Exception as e:
        return {"error": str(e)[:200]}
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    body = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))).strip()
    return {"title": html.unescape(title[1].strip()) if title else "", "og_image": meta(page, "og:image"),
            "published": meta(page, "article:published_time"), "text": body[:PAGE_LIMIT]}


def read(path):
    try:
        return open(path, encoding="utf-8").read()
    except FileNotFoundError:
        return ""


def recent_headlines():
    lines = []
    for path in sorted(glob.glob("drafts/*.json"))[-4:]:
        for d in json.load(open(path, encoding="utf-8")):
            lines.append("- " + d["text"].split("\n")[0])
    return "\n".join(lines)


def system_prompt():
    return f"""You write posts for Delta (@delta24news), a Russian-language Telegram channel with positive AI and technology news for a broad audience. You talk to Nina, the channel's owner, through her Telegram bot. She writes in Russian; answer in Russian.

Channel style guide (follow it exactly):
{read("style-guide.md")}

Reasons Nina gave when she rejected posts (learn from them):
{read("feedback.md") or "(none yet)"}

Headlines of recent drafts (do not repeat these stories):
{recent_headlines() or "(none)"}

How to work:
- Research with web search (results come with each request) and fetch_page. Use fresh news (last 1-2 days unless Nina asks otherwise) and check key facts against the primary source.
- Deliver posts only through the send_drafts tool, all in one call. Text is Telegram HTML (<b>, <i>, <a href>), no emoji, under 1000 characters.
- Media: a direct JPG/PNG image URL that you saw on the source page (og:image is ideal; avoid .webp and .avif). If only the page itself has a good preview, use media_type "link" with the page URL. Otherwise media_type "none".
- After send_drafts, finish with one short sentence for Nina (for example, what you found). If her message is not a request for posts, just answer it briefly without calling send_drafts.
- Today is {now_msk()[:10]} (Moscow)."""


def send_posts(posts, task):
    ok = 0
    for post in posts:
        draft = {"text": post["text"], "media_type": post["media_type"],
                 "media": post["media"] if post["media_type"] != "none" and post["media"] else None}
        if task["kind"] == "edit" and draft["media"] == task.get("media"):
            draft["media_type"] = task.get("media_type") or draft["media_type"]
        try:
            send(draft, task.get("day"))
        except Exception as e:
            print(f"send failed: {e}; retrying without media")
            send({**draft, "media": None}, task.get("day"))
        ok += 1
    return ok


def use_tool(tool_call, task):
    """Run one tool call from the model; returns (result text, drafts sent)."""
    try:
        args = json.loads(tool_call.function.arguments or "{}")
    except json.JSONDecodeError:
        return "Error: arguments are not valid JSON.", 0
    name = tool_call.function.name
    if name == "fetch_page":
        return json.dumps(fetch_page(str(args.get("url", ""))), ensure_ascii=False), 0
    if name == "send_drafts":
        ok = send_posts(args.get("posts", []), task)
        return f"Sent {ok} drafts.", ok
    return f"Error: unknown tool {name}.", 0


def run(task):
    if task["kind"] == "edit":
        user = (f"Rewrite this draft according to Nina's notes and send the new version with send_drafts "
                f"(one post). Keep the current media unless the notes ask to change it; current media: "
                f"{task.get('media_type')} {task.get('media')}. If her message is already a complete "
                f"rewritten post, use it as is, only fixing the HTML.\n\nDraft:\n{task['html']}\n\n"
                f"Nina's notes:\n{task['instructions']}")
    else:
        user = task["text"]
    messages = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": user}]
    sent = 0
    for _ in range(12):
        # Web search runs on every request it is on, so turn it off once the drafts are sent.
        choice = polza.chat(messages, tools=TOOLS, web=not sent)
        message = choice.message
        if choice.finish_reason == "content_filter" or getattr(message, "refusal", None):
            return "Не получилось подготовить это, попробуй сформулировать иначе."
        messages.append(polza.assistant_message(message))
        if not message.tool_calls:
            return polza.text(message)
        for tool_call in message.tool_calls:
            result, ok = use_tool(tool_call, task)
            sent += ok
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result})
    return "Готово." if sent else "Не успел закончить, попробуй ещё раз."


def main():
    task = json.loads(os.environ["TASK"])
    try:
        answer = run(task)
    except Exception:
        call("sendMessage", chat_id=ADMIN, text="Что-то пошло не так, попробуй ещё раз чуть позже.")
        if task["kind"] == "edit":  # give the old draft its buttons back
            call("editMessageReplyMarkup", chat_id=ADMIN, message_id=task["msg"],
                 reply_markup=draft_buttons(f"{task['day']}T09:00"))
        raise
    if task["kind"] == "edit":
        call("editMessageReplyMarkup", chat_id=ADMIN, message_id=task["msg"],
             reply_markup={"inline_keyboard": [[{"text": "✏️ Новая версия ниже", "callback_data": "done"}]]})
    if answer:
        call("sendMessage", chat_id=ADMIN, text=answer)


if __name__ == "__main__":
    main()
