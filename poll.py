"""Handle Nina's messages and button presses, and publish scheduled posts to the channel.

Runs from GitHub Actions. Telegram keeps unconfirmed updates for 24h. State lives in
queue.json (scheduled posts) and pending.json (questions the bot is waiting on), which
the workflow commits back to the repo; updates are confirmed (`--confirm OFFSET`) only
after that push succeeds. Anything that needs writing (new posts, edits) is handed to
the "AI tasks" workflow (ai.py).
"""
import json
import os
import re
import sys
import urllib.request

from drafthtml import to_html
from schedule import draft_buttons, now_msk, scheduled_buttons
from tg import call, download

ADMIN = int(os.environ["ADMIN_CHAT_ID"])
CHANNEL = os.environ["CHANNEL_ID"]  # e.g. @delta24news
AI_ENABLED = os.environ.get("AI_ENABLED") == "true"
QUEUE, PENDING, FEEDBACK = "queue.json", "pending.json", "feedback.md"
TIME = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")
NO_AI = ("Пока я понимаю только кнопки под черновиками, ответ со временем (15:30) "
         "и ответ с исправленным текстом. Писать новые посты и слушать голосовые научусь, когда подключат ключ Polza AI.")
NOT_HEARD = "Не получилось разобрать голосовое, попробуй ещё раз или напиши текстом."


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def run_ai(task):
    """Start the "AI tasks" workflow with this task (needs GITHUB_TOKEN with actions: write)."""
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/ai.yml/dispatches",
        data=json.dumps({"ref": "main", "inputs": {"task": json.dumps(task, ensure_ascii=False)}}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    urllib.request.urlopen(req, timeout=30)


def say(text, **extra):
    return call("sendMessage", chat_id=ADMIN, text=text, **extra)


def set_buttons(message_id, markup):
    call("editMessageReplyMarkup", chat_id=ADMIN, message_id=message_id, reply_markup=markup)


def mark(message_id, label):
    set_buttons(message_id, {"inline_keyboard": [[{"text": label, "callback_data": "done"}]]})


def publish(message_id):
    call("copyMessage", chat_id=CHANNEL, from_chat_id=ADMIN,
         message_id=message_id, reply_markup={"inline_keyboard": []})
    mark(message_id, f"✅ Опубликовано в {now_msk()[11:]}")


def react(msg, emoji="👍"):
    try:
        call("setMessageReaction", chat_id=ADMIN, message_id=msg["message_id"],
             reaction=[{"type": "emoji", "emoji": emoji}])
    except RuntimeError:
        pass


class Bot:
    def __init__(self):
        self.queue = load(QUEUE, [])
        self.pending = load(PENDING, {})  # question message id -> what the answer is for

    def save(self):
        save(QUEUE, sorted(self.queue, key=lambda i: i["at"]))
        save(PENDING, dict(list(self.pending.items())[-20:]))

    def unqueue(self, mid):
        self.queue[:] = [i for i in self.queue if i["msg"] != mid]

    def ask(self, text, about, buttons=None):
        markup = {"force_reply": True, "input_field_placeholder": "Напиши ответ"}
        if buttons:
            markup = {"inline_keyboard": buttons}
        q = say(text, reply_markup=markup)
        self.pending[str(q["message_id"])] = {**about, "asked": now_msk()}

    # --- messages ---------------------------------------------------------
    def on_message(self, msg):
        text = msg.get("text", "")
        if text.startswith("/start"):
            say("Привет! Это Delta. Сюда приходят черновики постов. "
                "Можно написать, например: «пришли 2 новые новости» или «сделай пост про новый iPhone».")
            return
        voice = msg.get("voice") or msg.get("audio")
        if voice and not text:
            if not AI_ENABLED:
                say(NO_AI)
                return
            text = self.hear(voice)
            if not text:
                say(NOT_HEARD)
                return
            msg = {**msg, "text": text, "entities": []}
        if not text:
            return
        target = msg.get("reply_to_message")
        about = None
        if target and str(target["message_id"]) in self.pending:
            about = self.pending.pop(str(target["message_id"]))
        elif not target and self.pending and self.recent(list(self.pending.values())[-1]):
            # Answered right away without tapping "reply": take the most recent open question.
            about = self.pending.pop(list(self.pending)[-1])
        if about and about["kind"] == "reason":
            self.save_reason(about, text)
            react(msg)
        elif about and about["kind"] == "edit":
            self.start_edit(about, text, msg)
        elif target and self.is_open_draft(target):
            self.on_draft_reply(target, text, msg)
        elif AI_ENABLED:
            self.ai({"kind": "chat", "text": text}, msg)
        else:
            say(NO_AI)

    @staticmethod
    def hear(voice):
        """Text of a voice message via Polza speech to text; empty string if it failed."""
        try:
            import polza  # needs the openai package and POLZA_API_KEY, only when a voice comes
            return polza.transcribe(download(voice["file_id"]), voice.get("mime_type") or "audio/ogg")
        except Exception as e:
            print(f"could not transcribe a voice message: {e!r}")
            return ""

    @staticmethod
    def recent(about):
        from datetime import datetime
        asked = datetime.fromisoformat(about.get("asked", "2000-01-01T00:00"))
        return (datetime.fromisoformat(now_msk()) - asked).total_seconds() < 30 * 60

    @staticmethod
    def buttons_of(message):
        return [b["callback_data"] for row in message.get("reply_markup", {}).get("inline_keyboard", []) for b in row]

    def is_open_draft(self, message):
        return any(b in ("pub", "edit") or b.startswith(("at:", "un:")) for b in self.buttons_of(message))

    def on_draft_reply(self, draft, text, msg):
        """Reply to a draft: "15:30" moves it, other text is an edit request (or the new text without AI)."""
        mid = draft["message_id"]
        m = TIME.match(text)
        if m and int(m[1]) <= 23 and int(m[2]) <= 59:
            old = next((b[3:] for b in self.buttons_of(draft) if b.startswith(("at:", "un:"))), now_msk())
            at = f"{old[:10]}T{int(m[1]):02d}:{m[2]}"
            if any(i["msg"] == mid for i in self.queue):
                self.unqueue(mid)
                self.queue.append({"msg": mid, "at": at})
                set_buttons(mid, scheduled_buttons(at))
            else:
                set_buttons(mid, draft_buttons(at))
            react(msg)
        elif AI_ENABLED:
            self.start_edit(self.draft_info(draft), text, msg)
        else:
            self.replace_text(draft, msg)
            react(msg)

    @staticmethod
    def draft_info(draft):
        html, media, media_type = to_html(draft)
        at = next((b[3:] for b in Bot.buttons_of(draft) if b.startswith("at:")), None)
        return {"kind": "edit", "msg": draft["message_id"], "html": html,
                "media": media, "media_type": media_type, "day": (at or now_msk())[:10]}

    def start_edit(self, about, instructions, msg):
        if not AI_ENABLED:
            say(NO_AI)
            return
        about = {k: v for k, v in about.items() if k != "asked"}
        if self.ai({**about, "instructions": instructions}, msg):
            mark(about["msg"], "✏️ Переписываю…")

    @staticmethod
    def ai(task, msg):
        try:
            run_ai(task)
        except Exception as e:
            print(f"could not start AI task: {e!r}")
            say("Не получилось взяться за задачу, попробуй ещё раз чуть позже.")
            return False
        react(msg, "👀")
        return True

    def replace_text(self, draft, msg):
        markup = draft.get("reply_markup", {})
        if "photo" in draft or "video" in draft:
            if len(msg["text"]) > 1024:
                say("Под фото помещается до 1024 символов, этот текст длиннее. Сократи, пожалуйста.")
                return
            call("editMessageCaption", chat_id=ADMIN, message_id=draft["message_id"], caption=msg["text"],
                 caption_entities=msg.get("entities", []), reply_markup=markup)
        else:
            call("editMessageText", chat_id=ADMIN, message_id=draft["message_id"], text=msg["text"],
                 entities=msg.get("entities", []), reply_markup=markup,
                 link_preview_options=draft.get("link_preview_options"))

    def save_reason(self, about, reason):
        with open(FEEDBACK, "a", encoding="utf-8") as f:
            f.write(f"- {now_msk()[:10]} «{about['title']}»: {reason.strip()}\n")

    # --- buttons ----------------------------------------------------------
    def on_button(self, q):
        draft, data, note = q["message"], q["data"], None
        mid = draft["message_id"]
        if data == "pub":
            self.unqueue(mid)
            publish(mid)
        elif data.startswith("at:"):
            at = data[3:]
            self.unqueue(mid)
            self.queue.append({"msg": mid, "at": at})
            set_buttons(mid, scheduled_buttons(at))
            note = f"Выйдет в {at[11:]}"
        elif data.startswith("un:"):
            self.unqueue(mid)
            set_buttons(mid, draft_buttons(data[3:]))
        elif data == "edit":
            self.ask("Что поправить? Ответь текстом или голосовым.", self.draft_info(draft))
        elif data == "rej":
            self.unqueue(mid)
            mark(mid, "✖️ Отклонено")
            title = to_html(draft)[0].split("\n")[0]
            title = re.sub(r"<[^>]+>", "", title)[:80]
            self.ask("Почему? Учту в следующих постах. Можно текстом или голосовым.", {"kind": "reason", "title": title},
                     buttons=[[{"text": "Пропустить", "callback_data": "skip"}]])
        elif data == "skip":
            self.pending.pop(str(mid), None)
            call("deleteMessage", chat_id=ADMIN, message_id=mid)
        try:
            call("answerCallbackQuery", callback_query_id=q["id"], text=note)
        except RuntimeError:
            pass  # query too old to answer; harmless

    def handle(self, update):
        msg = update.get("message")
        if msg and msg["chat"]["id"] == ADMIN:
            self.on_message(msg)
        elif msg and msg.get("text", "").startswith("/start"):
            call("sendMessage", chat_id=msg["chat"]["id"], text="Привет! Это Delta.")
        q = update.get("callback_query")
        if q and q["from"]["id"] == ADMIN:
            self.on_button(q)

    def publish_due(self):
        now = now_msk()
        for item in [i for i in self.queue if i["at"] <= now]:
            try:
                publish(item["msg"])
            except RuntimeError as e:
                print(f"publishing {item['msg']} failed: {e}")
            self.unqueue(item["msg"])


def main():
    bot = Bot()
    updates = call("getUpdates", timeout=0, allowed_updates=["message", "callback_query"])
    for u in updates:
        try:
            bot.handle(u)
        except Exception as e:  # one bad update must not block the rest
            print(f"update {u['update_id']} failed: {e}")
    bot.publish_due()
    bot.save()
    if updates:
        with open(".confirm_offset", "w") as f:
            f.write(str(updates[-1]["update_id"] + 1))
    print(f"processed {len(updates)} updates, {len(bot.queue)} posts queued")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--confirm"]:
        call("getUpdates", offset=int(sys.argv[2]), timeout=0)
    else:
        main()
