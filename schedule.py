"""Shared bits for scheduled publishing: Moscow time and the draft buttons."""
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))


def now_msk():
    return datetime.now(MSK).strftime("%Y-%m-%dT%H:%M")


SLOTS = ["09:00", "12:00", "15:00", "18:00", "21:00"]


IMAGE_BUTTON = {"text": "Другая картинка", "callback_data": "img"}


def draft_buttons(at, image=False, mark=False):
    """Buttons under a draft that is not scheduled yet: one per time slot, the tapped one sets
    the publish time. `at` is 'YYYY-MM-DDTHH:MM' Moscow time; a time outside SLOTS
    (set by replying "15:30") gets its own button. mark=True marks `at` as the suggested time
    with "•"; image=True adds «Другая картинка» (posts from voice messages)."""
    day, times = at[:10], sorted(set(SLOTS) | {at[11:]})
    rows = [
        [{"text": f"• {t}" if mark and t == at[11:] else t, "callback_data": f"at:{day}T{t}"} for t in times],
        [{"text": "Изменить", "callback_data": "edit"}, {"text": "Сейчас", "callback_data": "pub"},
         {"text": "Отклонить", "callback_data": "rej"}],
    ]
    if image:
        rows.append([IMAGE_BUTTON])
    return {"inline_keyboard": rows}


def scheduled_buttons(at, image=False):
    """A scheduled draft: «Отменить» brings back draft_buttons (ui: instead of un: keeps «Другая картинка»)."""
    return {"inline_keyboard": [[
        {"text": f"⏰ В очереди на {at[11:]}", "callback_data": "done"},
        {"text": "Отменить", "callback_data": f"{'ui' if image else 'un'}:{at}"},
    ]]}


def next_free_slot(taken, now=None):
    """The nearest slot after now (Moscow) that no queued post takes: 'YYYY-MM-DDTHH:MM'.
    `taken` is a set of such strings (queue.json "at" values). Looks up to a week ahead."""
    now = now or now_msk()
    day = datetime.fromisoformat(now[:10])
    for _ in range(7):
        for t in SLOTS:
            at = f"{day:%Y-%m-%d}T{t}"
            if at > now and at not in taken:
                return at
        day += timedelta(days=1)
    return f"{day:%Y-%m-%d}T{SLOTS[0]}"
