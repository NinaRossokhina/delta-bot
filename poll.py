"""Handle button presses: publish approved drafts to the channel.

Runs on a schedule (GitHub Actions). Telegram keeps unconfirmed updates for 24h,
so no state is stored here: confirming via the offset is enough.
"""
import os

from tg import call

ADMIN = int(os.environ["ADMIN_CHAT_ID"])
CHANNEL = os.environ["CHANNEL_ID"]  # e.g. @delta24news


def mark(msg, label):
    call("editMessageReplyMarkup", chat_id=ADMIN, message_id=msg["message_id"],
         reply_markup={"inline_keyboard": [[{"text": label, "callback_data": "done"}]]})


def handle(update):
    msg = update.get("message")
    if msg and msg.get("text", "").startswith("/start"):
        call("sendMessage", chat_id=msg["chat"]["id"],
             text="Привет! Это Delta. Сюда будут приходить черновики постов.")
        return
    q = update.get("callback_query")
    if not q or q["from"]["id"] != ADMIN:
        return
    draft = q["message"]
    if q["data"] == "pub":
        call("copyMessage", chat_id=CHANNEL, from_chat_id=ADMIN,
             message_id=draft["message_id"], reply_markup={"inline_keyboard": []})
        mark(draft, "✅ Опубликовано")
    elif q["data"] == "rej":
        mark(draft, "✖️ Отклонено")
    try:
        call("answerCallbackQuery", callback_query_id=q["id"])
    except RuntimeError:
        pass  # query too old to answer; harmless


def main():
    updates = call("getUpdates", timeout=0, allowed_updates=["message", "callback_query"])
    for u in updates:
        try:
            handle(u)
        except RuntimeError as e:
            print(f"update {u['update_id']} failed: {e}")
    if updates:
        call("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0)
    print(f"processed {len(updates)} updates")


if __name__ == "__main__":
    main()
