"""Shared bits for scheduled publishing: Moscow time and the draft buttons."""
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))


def now_msk():
    return datetime.now(MSK).strftime("%Y-%m-%dT%H:%M")


def draft_buttons(at):
    """Buttons under a draft that is not scheduled yet. `at` is 'YYYY-MM-DDTHH:MM' Moscow time."""
    return {"inline_keyboard": [
        [{"text": f"Опубликовать в {at[11:]}", "callback_data": f"at:{at}"}],
        [{"text": "Сейчас", "callback_data": "pub"}, {"text": "Отклонить", "callback_data": "rej"}],
    ]}


def scheduled_buttons(at):
    return {"inline_keyboard": [[
        {"text": f"⏰ В очереди на {at[11:]}", "callback_data": "done"},
        {"text": "Отменить", "callback_data": f"un:{at}"},
    ]]}
