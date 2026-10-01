"""Send each draft in a JSON file to the admin chat with Publish / Reject buttons.

Usage: python send_drafts.py drafts/2026-10-01.json

Draft file format: a list of objects
  {"text": "<b>Заголовок</b>\\n\\nТекст... <a href='...'>Источник</a>",
   "media": "https://.../image.jpg",      # optional
   "media_type": "photo" | "video"}       # optional, default photo
Text uses Telegram HTML formatting.
"""
import json
import os
import sys

from tg import call

ADMIN = os.environ["ADMIN_CHAT_ID"]
CAPTION_LIMIT = 1024
BUTTONS = {"inline_keyboard": [[
    {"text": "Опубликовать", "callback_data": "pub"},
    {"text": "Отклонить", "callback_data": "rej"},
]]}


def send(draft):
    text, media = draft["text"], draft.get("media")
    kind = draft.get("media_type", "photo")
    if media and len(text) <= CAPTION_LIMIT:
        method = "sendVideo" if kind == "video" else "sendPhoto"
        return call(method, chat_id=ADMIN, **{kind: media}, caption=text,
                    parse_mode="HTML", reply_markup=BUTTONS)
    # Text too long for a caption: send as a message with the media shown as a link preview.
    preview = {"url": media, "prefer_large_media": True, "show_above_text": True} if media else {"is_disabled": True}
    return call("sendMessage", chat_id=ADMIN, text=text, parse_mode="HTML",
                link_preview_options=preview, reply_markup=BUTTONS)


def main(path):
    drafts = json.load(open(path, encoding="utf-8"))
    for i, d in enumerate(drafts, 1):
        try:
            send(d)
            print(f"sent {i}/{len(drafts)}")
        except Exception as e:  # keep going so one bad image doesn't block the rest
            print(f"draft {i} failed: {e}; retrying without media")
            send({**d, "media": None})


if __name__ == "__main__":
    main(sys.argv[1])
