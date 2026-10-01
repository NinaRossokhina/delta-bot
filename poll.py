"""Handle button presses and publish scheduled posts to the channel.

Runs on a schedule (GitHub Actions). Telegram keeps unconfirmed updates for 24h.
Scheduled posts live in queue.json, which the workflow commits back to the repo;
updates are confirmed (`--confirm OFFSET`) only after that push succeeds.
"""
import json
import os
import re
import sys

from schedule import draft_buttons, now_msk, scheduled_buttons
from tg import call

ADMIN = int(os.environ["ADMIN_CHAT_ID"])
CHANNEL = os.environ["CHANNEL_ID"]  # e.g. @delta24news
QUEUE = "queue.json"
TIME = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


def load_queue():
    try:
        return json.load(open(QUEUE, encoding="utf-8"))
    except FileNotFoundError:
        return []


def save_queue(queue):
    with open(QUEUE, "w", encoding="utf-8") as f:
        json.dump(sorted(queue, key=lambda i: i["at"]), f, indent=1)
        f.write("\n")


def set_buttons(message_id, markup):
    call("editMessageReplyMarkup", chat_id=ADMIN, message_id=message_id, reply_markup=markup)


def mark(message_id, label):
    set_buttons(message_id, {"inline_keyboard": [[{"text": label, "callback_data": "done"}]]})


def publish(message_id):
    call("copyMessage", chat_id=CHANNEL, from_chat_id=ADMIN,
         message_id=message_id, reply_markup={"inline_keyboard": []})
    mark(message_id, f"✅ Опубликовано в {now_msk()[11:]}")


def unqueue(queue, message_id):
    queue[:] = [i for i in queue if i["msg"] != message_id]


def retime(queue, msg):
    """Admin replied to a draft with a time like 15:30: move the draft to that time."""
    m = TIME.match(msg.get("text", ""))
    draft = msg.get("reply_to_message")
    if not m or not draft or int(m[1]) > 23 or int(m[2]) > 59:
        return
    buttons = [b["callback_data"] for row in draft.get("reply_markup", {}).get("inline_keyboard", []) for b in row]
    old = next((b[3:] for b in buttons if b.startswith(("at:", "un:"))), None)
    if not old:
        return  # not a scheduled-style draft that is still open (or already published / rejected)
    at = f"{old[:10]}T{int(m[1]):02d}:{m[2]}"
    mid = draft["message_id"]
    if any(i["msg"] == mid for i in queue):
        unqueue(queue, mid)
        queue.append({"msg": mid, "at": at})
        set_buttons(mid, scheduled_buttons(at))
    else:
        set_buttons(mid, draft_buttons(at))
    try:
        call("setMessageReaction", chat_id=ADMIN, message_id=msg["message_id"],
             reaction=[{"type": "emoji", "emoji": "👍"}])
    except RuntimeError:
        pass


def handle(update, queue):
    msg = update.get("message")
    if msg and msg.get("text", "").startswith("/start"):
        call("sendMessage", chat_id=msg["chat"]["id"],
             text="Привет! Это Delta. Сюда будут приходить черновики постов.")
        return
    if msg and msg["chat"]["id"] == ADMIN:
        retime(queue, msg)
        return
    q = update.get("callback_query")
    if not q or q["from"]["id"] != ADMIN:
        return
    mid, data, note = q["message"]["message_id"], q["data"], None
    if data == "pub":
        unqueue(queue, mid)
        publish(mid)
    elif data.startswith("at:"):
        at = data[3:]
        unqueue(queue, mid)
        queue.append({"msg": mid, "at": at})
        set_buttons(mid, scheduled_buttons(at))
        note = f"Выйдет в {at[11:]}"
    elif data.startswith("un:"):
        unqueue(queue, mid)
        set_buttons(mid, draft_buttons(data[3:]))
    elif data == "rej":
        unqueue(queue, mid)
        mark(mid, "✖️ Отклонено")
    try:
        call("answerCallbackQuery", callback_query_id=q["id"], text=note)
    except RuntimeError:
        pass  # query too old to answer; harmless


def publish_due(queue):
    now = now_msk()
    for item in [i for i in queue if i["at"] <= now]:
        try:
            publish(item["msg"])
        except RuntimeError as e:
            print(f"publishing {item['msg']} failed: {e}")
        unqueue(queue, item["msg"])


def main():
    queue = load_queue()
    updates = call("getUpdates", timeout=0, allowed_updates=["message", "callback_query"])
    for u in updates:
        try:
            handle(u, queue)
        except RuntimeError as e:
            print(f"update {u['update_id']} failed: {e}")
    publish_due(queue)
    save_queue(queue)
    if updates:
        with open(".confirm_offset", "w") as f:
            f.write(str(updates[-1]["update_id"] + 1))
    print(f"processed {len(updates)} updates, {len(queue)} posts queued")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--confirm"]:
        call("getUpdates", offset=int(sys.argv[2]), timeout=0)
    else:
        main()
