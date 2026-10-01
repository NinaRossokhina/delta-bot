"""AI tasks for the Delta bot, run by the "AI tasks" workflow.

Task kinds (JSON in the TASK env var):
  {"kind": "chat", "text": "пришли 2 новые новости"}  - Nina's free-form request
  {"kind": "edit", "msg": 123, "html": "...", "media": "...", "media_type": "photo",
   "day": "2026-10-02", "instructions": "1. короче 2. другой заголовок"}  - rewrite a draft
  {"kind": "daily"}  - the day's 5-7 posts, saved to drafts/<today>.json
  {"kind": "daily", "scheduled": true}  - the same from the morning schedule (empty TASK);
                                         skipped if today's drafts exist
New or rewritten posts are sent to Nina as drafts with the usual buttons. Daily posts are
committed by the workflow, which then runs send.yml for the new file.
"""
import glob
import html
import json
import os
import re
import urllib.request

import polza
from datetime import date, timedelta

from schedule import SLOTS, draft_buttons, now_msk
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
RUBRICS = ["news_of_the_day", "research", "good_news", "useful_find", "other_side", "humor", "digest"]
DAILY_SCHEMA = {
    "type": "object",
    "properties": {"posts": {"type": "array", "description": "5-7 posts for today, the evening digest last.", "items": {
        "type": "object",
        "properties": {
            "rubric": {"type": "string", "enum": RUBRICS, "description": "Rubric from the style guide, in its order."},
            "text": {"type": "string", "description": "Telegram HTML: <b>headline</b>, short paragraphs separated by \\n\\n, source as <a href='...'>word</a>. Under 1000 characters."},
            "image": {"type": "string", "description": "Direct .jpg/.png image URL for the post."},
            "source": {"type": "string", "description": "URL of the primary source the facts were checked against."},
        },
        "required": ["rubric", "text", "image", "source"],
        "additionalProperties": False,
    }}},
    "required": ["posts"],
    "additionalProperties": False,
}
EXTRA_TIMES = ["10:30", "13:30", "16:30", "19:30"]  # for days with more than 5 posts
DAILY_TASK = """Prepare today's posts for the channel: {count} fresh news stories from the last 24 hours, one post each, following the rubrics of the style guide (the evening positive digest last, it sums up the day's good news).
- Only positive stories: no alarming news, scandals, layoffs, wars or disasters.
- Check every fact against the primary source (fetch_page) and link the source as a hyperlinked word.
- Each post strictly follows the style guide, under 1000 characters, with a direct .jpg/.png image URL you saw on the source page (og:image is ideal).
- Do not repeat stories from the recent drafts listed above.
Return the posts as JSON in the given format (this time not through send_drafts: the workflow saves and sends them)."""


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


def recent_headlines(days=4):
    today = date.fromisoformat(now_msk()[:10])
    lines = []
    for path in sorted(glob.glob("drafts/*.json")):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        if not m or today - date.fromisoformat(m[1]) > timedelta(days=days):
            continue
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


def times_for(n):
    """Publish times for n posts: the usual slots (plus in-between ones on busy days), 21:00 last."""
    day = sorted(SLOTS[:-1] + EXTRA_TIMES[:max(0, n - len(SLOTS))])
    return day[:n - 1] + [SLOTS[-1]]


def draft_path(day):
    """drafts/<day>.json, or drafts/<day>-2.json etc. if that file is already there."""
    path, i = f"drafts/{day}.json", 1
    while os.path.exists(path):
        i += 1
        path = f"drafts/{day}-{i}.json"
    return path


def daily():
    """Find today's posts, save them to drafts/<today>.json and return the path."""
    tools = [t for t in TOOLS if t["function"]["name"] == "fetch_page"]
    messages = [{"role": "system", "content": system_prompt()},
                {"role": "user", "content": DAILY_TASK.format(count="as many as the style guide sets for today (5-7)")}]
    for _ in range(20):
        choice = polza.chat(messages, tools=tools, web=True,
                            response_format=polza.json_schema("daily_posts", DAILY_SCHEMA))
        message = choice.message
        if choice.finish_reason == "content_filter" or getattr(message, "refusal", None):
            raise RuntimeError("the model refused")
        messages.append(polza.assistant_message(message))
        if message.tool_calls:
            for tool_call in message.tool_calls:
                result, _ = use_tool(tool_call, {"kind": "daily"})
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result})
            continue
        posts = json.loads(polza.text(message))["posts"]
        digest = [p for p in posts if p["rubric"] == "digest"][-1:]  # always the last post of the day
        posts = [p for p in posts if p["rubric"] != "digest"][:7 - len(digest)] + digest
        if not posts:
            raise RuntimeError("no posts in the answer")
        day = now_msk()[:10]
        drafts = [{"text": p["text"], "media": p["image"] or None, "media_type": "photo" if p["image"] else "none",
                   "time": t} for p, t in zip(posts, times_for(len(posts)))]
        path = draft_path(day)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(drafts, f, ensure_ascii=False, indent=1)
            f.write("\n")
        return path
    raise RuntimeError("no answer after 20 steps")


FAILED_TODAY = "Сегодня черновики не собрались: {}"


def drafts_exist(day):
    """Daily drafts for this day are already saved (drafts/<day>.json or drafts/<day>-2.json etc.)."""
    return any(re.fullmatch(rf"{day}(-\d+)?\.json", name) for name in os.listdir("drafts")) if os.path.isdir("drafts") else False


def reason(e):
    """Why the daily run failed, in Russian, for Nina."""
    import openai
    if isinstance(e, openai.APIStatusError):
        return f"Polza AI ответила ошибкой {e.status_code} ({str(e)[:200]})"
    if isinstance(e, openai.APIConnectionError):
        return "нет связи с Polza AI"
    if isinstance(e, (json.JSONDecodeError, KeyError, TypeError)):
        return "нейросеть вернула ответ не в том формате"
    return str(e)[:300] or type(e).__name__


def main():
    task = json.loads(os.environ.get("TASK") or '{"kind": "daily", "scheduled": true}')
    if task["kind"] == "daily":
        day = now_msk()[:10]
        if task.get("scheduled") and drafts_exist(day):
            print(f"drafts for {day} already exist, nothing to do")
            return
        try:
            path = daily()
        except Exception as e:
            call("sendMessage", chat_id=ADMIN, text=FAILED_TODAY.format(reason(e)))
            raise
        print(f"saved {path}")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"file={path}\n")
        return
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
