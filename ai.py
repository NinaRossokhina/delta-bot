"""AI tasks for the Delta bot, run by the "AI tasks" workflow.

Task kinds (JSON in the TASK env var):
  {"kind": "chat", "text": "пришли 2 новые новости"}  - Nina's free-form request
  {"kind": "edit", "msg": 123, "html": "...", "media": "...", "media_type": "photo",
   "day": "2026-10-02", "instructions": "1. короче 2. другой заголовок"}  - rewrite a draft
New or rewritten posts are sent to Nina as drafts with the usual buttons.
"""
import glob
import json
import os

import anthropic

from schedule import draft_buttons, now_msk
from send_drafts import send
from tg import call

ADMIN = os.environ["ADMIN_CHAT_ID"]
MODEL = "claude-opus-5-5"

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
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
    {
        "name": "send_drafts",
        "description": "Send finished posts to Nina as drafts for approval. Call it once with all posts.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"posts": {"type": "array", "items": POST_SCHEMA}},
            "required": ["posts"],
            "additionalProperties": False,
        },
    },
]


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
- Research with web_search / web_fetch. Use fresh news (last 1-2 days unless Nina asks otherwise) and check key facts against the primary source.
- Deliver posts only through the send_drafts tool, all in one call. Text is Telegram HTML (<b>, <i>, <a href>), no emoji, under 1000 characters.
- Media: a direct JPG/PNG image URL that you saw on the source page (og:image is ideal; avoid .webp and .avif). If only the page itself has a good preview, use media_type "link" with the page URL. Otherwise media_type "none".
- After send_drafts, finish with one short sentence for Nina (for example, what you found). If her message is not a request for posts, just answer it briefly without calling send_drafts.
- Today is {now_msk()[:10]} (Moscow)."""


def run(task):
    client = anthropic.Anthropic()
    if task["kind"] == "edit":
        user = (f"Rewrite this draft according to Nina's notes and send the new version with send_drafts "
                f"(one post). Keep the current media unless the notes ask to change it; current media: "
                f"{task.get('media_type')} {task.get('media')}. If her message is already a complete "
                f"rewritten post, use it as is, only fixing the HTML.\n\nDraft:\n{task['html']}\n\n"
                f"Nina's notes:\n{task['instructions']}")
    else:
        user = task["text"]
    messages = [{"role": "user", "content": user}]
    sent = 0
    for _ in range(12):
        with client.beta.messages.stream(
            model=MODEL, max_tokens=32000, system=system_prompt(), tools=TOOLS, messages=messages,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason == "refusal":
            return "Не получилось подготовить это, попробуй сформулировать иначе."
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            return "".join(b.text for b in response.content if b.type == "text").strip()
        results = []
        for block in tool_uses:
            posts = block.input.get("posts", []) if isinstance(block.input, dict) else []
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
            sent += ok
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"Sent {ok} drafts."})
        messages.append({"role": "user", "content": results})
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
