"""Shared bits for scheduled publishing: Moscow time and the draft buttons."""
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))


def now_msk():
    return datetime.now(MSK).strftime("%Y-%m-%dT%H:%M")


SLOTS = ["09:00", "12:00", "15:00", "18:00", "21:00"]


def draft_buttons(at):
    """Buttons under a draft that is not scheduled yet: one per time slot, the tapped one sets
    the publish time. `at` is 'YYYY-MM-DDTHH:MM' Moscow time; a time outside SLOTS
    (set by replying "15:30") gets its own button."""
    day, times = at[:10], sorted(set(SLOTS) | {at[11:]})
    return {"inline_keyboard": [
        [{"text": t, "callback_data": f"at:{day}T{t}"} for t in times],
        [{"text": "Изменить", "callback_data": "edit"}, {"text": "Сейчас", "callback_data": "pub"},
         {"text": "Отклонить", "callback_data": "rej"}],
    ]}


def scheduled_buttons(at):
    return {"inline_keyboard": [[
        {"text": f"⏰ В очереди на {at[11:]}", "callback_data": "done"},
        {"text": "Отменить", "callback_data": f"un:{at}"},
    ]]}
