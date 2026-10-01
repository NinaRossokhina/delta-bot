"""Send each draft in a JSON file to the admin chat with Publish / Reject buttons.

Usage: python send_drafts.py drafts/2026-10-01.json

Draft file format: a list of objects
  {"text": "<b>Заголовок</b>\\n\\nТекст... <a href='...'>Источник</a>",
   "media": "https://.../image.jpg",      # optional
   "media_type": "photo" | "video" | "link",  # optional, default photo;
                                               # "link" = text post with a link preview of media
   "time": "12:00"}                            # optional, Moscow time; the date comes from the file name
Text uses Telegram HTML formatting.
"""
import json
import os
import re
import sys

from schedule import draft_buttons, now_msk
from tg import call

ADMIN = os.environ["ADMIN_CHAT_ID"]
CAPTION_LIMIT = 1024
BUTTONS = {"inline_keyboard": [[
    {"text": "Опубликовать", "callback_data": "pub"},
    {"text": "Отклонить", "callback_data": "rej"},
]]}
HELP = ("Черновики на {day}, постов: {n}. Нажми «Опубликовать в …», и пост сам выйдет в это время. "
        "Чтобы поменять время, ответь на черновик сообщением вида 15:30. "
        "Чтобы исправить текст, скопируй черновик, поправь и пришли ответом на него.")


def send(draft, day=None):
    text, media = draft["text"], draft.get("media")
    kind = draft.get("media_type", "photo")
    time = draft.get("time")
    buttons = draft_buttons(f"{day}T{time}") if time and day else BUTTONS
    if media and kind != "link" and len(text) <= CAPTION_LIMIT:
        method = "sendVideo" if kind == "video" else "sendPhoto"
        return call(method, chat_id=ADMIN, **{kind: media}, caption=text,
                    parse_mode="HTML", reply_markup=buttons)
    # Text too long for a caption: send as a message with the media shown as a link preview.
    preview = {"url": media, "prefer_large_media": True, "show_above_text": True} if media else {"is_disabled": True}
    return call("sendMessage", chat_id=ADMIN, text=text, parse_mode="HTML",
                link_preview_options=preview, reply_markup=buttons)


def main(path):
    drafts = json.load(open(path, encoding="utf-8"))
    m = re.search(r"\d{4}-\d{2}-\d{2}", os.path.basename(path))
    day = m.group(0) if m else now_msk()[:10]
    if any(d.get("time") for d in drafts):
        call("sendMessage", chat_id=ADMIN, text=HELP.format(day=f"{day[8:]}.{day[5:7]}", n=len(drafts)))
    for i, d in enumerate(drafts, 1):
        try:
            send(d, day)
            print(f"sent {i}/{len(drafts)}")
        except Exception as e:  # keep going so one bad image doesn't block the rest
            print(f"draft {i} failed: {e}; retrying without media")
            send({**d, "media": None}, day)


if __name__ == "__main__":
    main(sys.argv[1])
