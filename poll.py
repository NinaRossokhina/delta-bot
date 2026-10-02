"""Handle Nina's messages and button presses, and publish scheduled posts to the channel.

Runs from GitHub Actions. Telegram keeps unconfirmed updates for 24h. State lives in
queue.json (scheduled posts) and pending.json (questions the bot is waiting on), which
the workflow commits back to the repo; updates are confirmed (`--confirm OFFSET`) only
after that push succeeds. Anything that needs writing (new posts, edits) is handed to
the "AI tasks" workflow (ai.py).
"""
import copy
import json
import os
import re
import sys
import urllib.request

import costs
from drafthtml import to_html
from schedule import draft_buttons, next_free_slot, now_msk, scheduled_buttons
from tg import FILE_LIMIT, TOO_BIG, Unavailable, call

ADMIN = int(os.environ["ADMIN_CHAT_ID"])
CHANNEL = os.environ["CHANNEL_ID"]  # e.g. @delta24news
AI_ENABLED = os.environ.get("AI_ENABLED") == "true"
QUEUE, PENDING, FEEDBACK, SLOTS = "queue.json", "pending.json", "feedback.md", "slots.json"
TIME = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")
NO_AI = ("Пока я понимаю только кнопки под черновиками, ответ со временем (15:30) "
         "и ответ с исправленным текстом. Писать новые посты и слушать голосовые научусь, когда подключат ключ Polza AI.")
GOT_VOICE = "Получила, работаю над постом"
LONG_VOICE = " Голосовое длинное ({} мин), расшифровка займёт несколько минут."


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
    quiet(mark, message_id, f"✅ Опубликовано в {now_msk()[11:]}")  # the post is out; a lost label must not repeat it


EDITED, EXAMPLES = "voice_edited.jsonl", "style/examples.md"


def edited_voice_posts():
    """Ids of voice posts' new versions after «Изменить» (written by ai.py)."""
    try:
        return {json.loads(line)["msg"] for line in open(EDITED, encoding="utf-8") if line.strip()}
    except FileNotFoundError:
        return set()


def plain(html_text):
    """Post HTML as plain text, the way examples.md quotes posts."""
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", html_text)).strip()


def learn(html_text):
    """Add a published voice post (final version after edits) to style/examples.md."""
    try:
        old = open(EXAMPLES, encoding="utf-8").read()
    except FileNotFoundError:
        old = "# Образцы постов\n"
    n = len(re.findall(r"^## ", old, re.M)) + 1
    with open(EXAMPLES, "a", encoding="utf-8") as f:
        f.write(f"{'' if old.endswith(chr(10)) else chr(10)}\n## {n}. Голосовой пост после правок · {now_msk()[:10]}\n\n"
                f"_Финальная версия, которую Нина опубликовала после правок._\n\n{plain(html_text)}\n")


def quiet(fn, *args, **kwargs):
    """A call that must not undo the work already done (a reaction, a note, a button label):
    if Telegram fails here, the update is still treated as handled, so the task is not started twice."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"{getattr(fn, '__name__', fn)} failed: {e}")


def react(msg, emoji="👍"):
    quiet(call, "setMessageReaction", chat_id=ADMIN, message_id=msg["message_id"],
          reaction=[{"type": "emoji", "emoji": emoji}])


class Bot:
    def __init__(self):
        self.queue = load(QUEUE, [])
        self.pending = load(PENDING, {})
        self.edited = edited_voice_posts()  # question message id -> what the answer is for
        self.slots = load(SLOTS, [])  # times suggested to posts from voice messages ("•"), not taken yet

    def save(self):
        save(QUEUE, sorted(self.queue, key=lambda i: i["at"]))
        save(PENDING, dict(list(self.pending.items())[-20:]))
        save(SLOTS, sorted(s for s in set(self.slots) if s > now_msk()))

    def snapshot(self):
        return copy.deepcopy((self.queue, self.pending, self.slots))

    def restore(self, state):
        self.queue, self.pending, self.slots = state

    def pick_slot(self):
        """The nearest slot not taken by the queue or by another voice post still waiting for Nina,
        so several voice messages in a row get different times."""
        now = now_msk()
        self.slots = [s for s in self.slots if s > now]
        at = next_free_slot({i["at"] for i in self.queue} | set(self.slots), now)
        self.slots.append(at)
        return at

    def release_slot(self, draft):
        """The draft's suggested time («• 15:00») is free again once Nina chose a time or rejected it."""
        for row in draft.get("reply_markup", {}).get("inline_keyboard", []):
            for b in row:
                if b["text"].startswith("• ") and b["callback_data"][3:] in self.slots:
                    self.slots.remove(b["callback_data"][3:])

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
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command == "/start":
            say("Привет! Это Delta. Сюда приходят черновики постов. "
                "Можно написать, например: «пришли 2 новые новости» или «сделай пост про новый iPhone». "
                "Команда /cost покажет, сколько потрачено на Polza AI.")
            return
        if command == "/cost":
            say(costs.report())
            return
        voice = msg.get("voice") or msg.get("audio")
        if not text and not voice:
            return
        if voice and not AI_ENABLED:
            say(NO_AI)
            return
        if voice and (voice.get("file_size") or 0) > FILE_LIMIT:
            say(TOO_BIG)  # an open question stays open: she can answer it with a shorter recording
            return
        target = msg.get("reply_to_message")
        about = self.answered(target)
        if voice:
            self.on_voice(voice, about, target, msg)
            return
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

    def answered(self, target):
        """The open question this message answers (taken out of pending), or None."""
        if target and str(target["message_id"]) in self.pending:
            return self.pending.pop(str(target["message_id"]))
        if not target and self.pending and self.recent(list(self.pending.values())[-1]):
            # Answered right away without tapping "reply": take the most recent open question.
            return self.pending.pop(list(self.pending)[-1])
        return None

    def on_voice(self, voice, about, target, msg):
        """Voice or audio: ai.py downloads and transcribes it (task "voice"). An answer to
        «Что поправить?» (or a voice reply to a draft) becomes the edit, to «Почему?» the reason."""
        task = {"kind": "voice", "file_id": voice["file_id"], "mime": voice.get("mime_type") or "audio/ogg",
                "duration": voice.get("duration") or 0}
        if not about and target and self.is_open_draft(target):
            about = self.draft_info(target)
        if about and about["kind"] == "edit":
            task["edit"] = {k: v for k, v in about.items() if k != "asked"}
        elif about and about["kind"] == "reason":
            task["reason"] = about["title"]
        else:
            task["at"] = self.pick_slot()
        if not self.ai(task, msg):
            if "at" in task:
                self.slots.remove(task["at"])
            return
        minutes = task["duration"] // 60
        quiet(say, GOT_VOICE + (LONG_VOICE.format(minutes) if minutes >= 10 else ""))
        if "edit" in task:
            self.unqueue(task["edit"]["msg"])  # a queued post must not go out in its old version
            quiet(mark, task["edit"]["msg"], "✏️ Переписываю…")

    @staticmethod
    def recent(about):
        from datetime import datetime
        asked = datetime.fromisoformat(about.get("asked", "2000-01-01T00:00"))
        return (datetime.fromisoformat(now_msk()) - asked).total_seconds() < 30 * 60

    @staticmethod
    def buttons_of(message):
        return [b["callback_data"] for row in message.get("reply_markup", {}).get("inline_keyboard", []) for b in row]

    def is_open_draft(self, message):
        return any(b in ("pub", "edit") or b.startswith(("at:", "un:", "ui:")) for b in self.buttons_of(message))

    @staticmethod
    def draft_time(draft):
        """The draft's time: the scheduled one, else the suggested one («• 15:00»), else None."""
        rows = draft.get("reply_markup", {}).get("inline_keyboard", [])
        for b in (b for row in rows for b in row):
            if b["callback_data"].startswith(("un:", "ui:")) or (b["text"].startswith("• ") and b["callback_data"].startswith("at:")):
                return b["callback_data"][3:]
        return None

    @staticmethod
    def has_image_button(message):
        """A post from a voice message: it has «Другая картинка» (or will get it back on «Отменить»)."""
        return any(b == "img" or b.startswith("ui:") for b in Bot.buttons_of(message))

    def on_draft_reply(self, draft, text, msg):
        """Reply to a draft: "15:30" moves it, other text is an edit request (or the new text without AI)."""
        mid = draft["message_id"]
        m = TIME.match(text)
        if m and int(m[1]) <= 23 and int(m[2]) <= 59:
            old = next((b[3:] for b in self.buttons_of(draft) if b.startswith(("at:", "un:", "ui:"))), now_msk())
            at = f"{old[:10]}T{int(m[1]):02d}:{m[2]}"
            if any(i["msg"] == mid for i in self.queue):
                self.unqueue(mid)
                self.enqueue(draft, at)
                set_buttons(mid, scheduled_buttons(at, self.has_image_button(draft)))
            else:
                set_buttons(mid, draft_buttons(at, self.has_image_button(draft)))
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
        when = Bot.draft_time(draft)
        info = {"kind": "edit", "msg": draft["message_id"], "html": html,
                "media": media, "media_type": media_type, "day": (at or now_msk())[:10]}
        if when:
            info["at"] = when
        return {**info, "image": True} if Bot.has_image_button(draft) else info

    def start_edit(self, about, instructions, msg):
        if not AI_ENABLED:
            say(NO_AI)
            return
        about = {k: v for k, v in about.items() if k != "asked"}
        if self.ai({**about, "instructions": instructions}, msg):
            self.unqueue(about["msg"])  # a queued post must not go out in its old version
            quiet(mark, about["msg"], "✏️ Переписываю…")

    @staticmethod
    def ai(task, msg=None):
        try:
            run_ai(task)
        except Exception as e:
            print(f"could not start AI task: {type(e).__name__} {getattr(e, 'code', '')}")
            quiet(say, "Не получилось взяться за задачу (GitHub не ответил), попробуй ещё раз чуть позже.")
            return False
        if msg:
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
        if data in ("pub", "rej") or data.startswith("at:"):
            self.release_slot(draft)
        if data == "pub":
            self.unqueue(mid)
            try:
                publish(mid)
            except RuntimeError as e:
                note = f"Не получилось опубликовать: {str(e)[:150]}"
            else:
                if mid in self.edited:
                    quiet(learn, to_html(draft)[0])
        elif data.startswith("at:"):
            at = data[3:]
            self.unqueue(mid)
            self.enqueue(draft, at)
            set_buttons(mid, scheduled_buttons(at, self.has_image_button(draft)))
            note = f"Выйдет в {at[11:]}"
        elif data.startswith(("un:", "ui:")):
            self.unqueue(mid)
            set_buttons(mid, draft_buttons(data[3:], image=data.startswith("ui:")))
        elif data == "img":
            if not AI_ENABLED:
                note = "Картинки рисую через Polza AI, а ключ не подключён"
            elif self.ai({**self.draft_info(draft), "kind": "image", "buttons": draft.get("reply_markup")}):
                quiet(set_buttons, mid, {"inline_keyboard": [[{"text": "🎨 Рисую другую картинку…", "callback_data": "done"}]]})
                note = "Рисую другую картинку"
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
        quiet(call, "answerCallbackQuery", callback_query_id=q["id"], text=note)  # may be too old; harmless

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
        """Publish posts whose time came. If Telegram is down, they stay queued for the next run;
        if Telegram refuses (the draft was deleted), the post is dropped and Nina is told."""
        now = now_msk()
        for item in [i for i in self.queue if i["at"] <= now]:
            try:
                publish(item["msg"])
            except Unavailable as e:
                print(f"publishing {item['msg']} postponed: {e}")
                continue
            except RuntimeError as e:
                print(f"publishing {item['msg']} failed: {e}")
                quiet(say, f"Не получилось опубликовать пост на {item['at'][11:]}: {str(e)[:150]}")
            else:
                if item.get("html"):
                    quiet(learn, item["html"])
            self.unqueue(item["msg"])

    def enqueue(self, draft, at):
        """Queue a draft. A voice post's version after edits keeps its text, for examples.md on publishing."""
        item = {"msg": draft["message_id"], "at": at}
        if draft["message_id"] in self.edited:
            item["html"] = to_html(draft)[0]
        self.queue.append(item)


def main():
    bot = Bot()
    updates = call("getUpdates", timeout=0, allowed_updates=["message", "callback_query"])
    confirm = None
    for u in updates:
        state = bot.snapshot()
        try:
            bot.handle(u)
        except Unavailable as e:
            # Telegram stopped answering: this update and the rest stay unconfirmed and come again next run.
            print(f"update {u['update_id']}: {e}; the rest waits for the next run")
            bot.restore(state)
            break
        except Exception as e:  # one bad update must not block the rest
            print(f"update {u['update_id']} failed: {type(e).__name__}: {e}")
        confirm = u["update_id"] + 1
    bot.publish_due()
    bot.save()
    if confirm:
        with open(".confirm_offset", "w") as f:
            f.write(str(confirm))
    print(f"processed {len(updates)} updates, {len(bot.queue)} posts queued")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--confirm"]:
        call("getUpdates", offset=int(sys.argv[2]), timeout=0)
    else:
        main()
