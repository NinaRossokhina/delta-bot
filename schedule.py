"""Shared bits for scheduled publishing: Moscow time and the draft buttons."""
import random
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))


def now_msk():
    return datetime.now(MSK).strftime("%Y-%m-%dT%H:%M")


SLOTS = [f"{h:02d}:00" for h in range(7, 23)]  # time buttons under a draft: every hour 07:00-22:00
ROW = 4  # time buttons per row
DAY_START, DAY_END, MIN_GAP = "07:00", "21:00", 60  # window of the daily news' random times; posts at least MIN_GAP minutes apart
DIGEST_TIME = "22:00"  # the evening digest, after the news


IMAGE_BUTTON = {"text": "Другая картинка", "callback_data": "img"}


WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def ddmm(day):
    """'2026-10-03' -> '03.10'."""
    return f"{day[8:10]}.{day[5:7]}"


def shift_day(day, days):
    return f"{datetime.fromisoformat(day) + timedelta(days=days):%Y-%m-%d}"


def draft_buttons(at, image=False, mark=False, now=None):
    """Buttons under a draft that is not scheduled yet. `at` is 'YYYY-MM-DDTHH:MM' Moscow time: its day
    is the publish date, shown in the first row with «◀» / «▶» to change it (d:<new at>); then one button
    per time slot of that day, the tapped one sets the publish time. A time outside SLOTS (set by
    replying "15:30" or a random daily time) gets its own button; mark=True marks `at` as the suggested
    time with "•". «Сейчас» shows today's date. image=True adds «Другая картинка» (posts from voice messages)."""
    now = now or now_msk()
    day, times = at[:10], sorted(set(SLOTS) | {at[11:]})
    date = [{"text": f"📅 {WEEKDAYS[datetime.fromisoformat(day).weekday()]}, {ddmm(day)}", "callback_data": "done"},
            {"text": f"{ddmm(shift_day(day, 1))} ▶", "callback_data": f"d:{shift_day(day, 1)}T{at[11:]}"}]
    if day > now[:10]:
        date.insert(0, {"text": f"◀ {ddmm(shift_day(day, -1))}", "callback_data": f"d:{shift_day(day, -1)}T{at[11:]}"})
    buttons = [{"text": f"• {t}" if mark and t == at[11:] else t, "callback_data": f"at:{day}T{t}"} for t in times]
    rows = [date] + [buttons[i:i + ROW] for i in range(0, len(buttons), ROW)]
    rows.append([{"text": "Изменить", "callback_data": "edit"},
                 {"text": f"Сейчас, {ddmm(now[:10])}", "callback_data": "pub"},
                 {"text": "Отклонить", "callback_data": "rej"}])
    if image:
        rows.append([IMAGE_BUTTON])
    return {"inline_keyboard": rows}


def scheduled_buttons(at, image=False):
    """A scheduled draft: «Отменить» brings back draft_buttons (ui: instead of un: keeps «Другая картинка»)."""
    return {"inline_keyboard": [[
        {"text": f"⏰ {ddmm(at[:10])} в {at[11:]}", "callback_data": "done"},
        {"text": "Отменить", "callback_data": f"{'ui' if image else 'un'}:{at}"},
    ]]}


def too_close(at, taken, gap=MIN_GAP):
    """A time in `taken` less than `gap` minutes from `at` (all 'YYYY-MM-DDTHH:MM'), else None."""
    when = datetime.fromisoformat(at)
    return next((t for t in sorted(taken) if abs(datetime.fromisoformat(t) - when) < timedelta(minutes=gap)), None)


def next_free_slot(taken, now=None):
    """The nearest slot after now (Moscow) at least MIN_GAP minutes from every queued post: 'YYYY-MM-DDTHH:MM'.
    `taken` is a set of such strings (queue.json "at" values). Looks up to a week ahead."""
    now = now or now_msk()
    day = datetime.fromisoformat(now[:10])
    for _ in range(7):
        for t in SLOTS:
            at = f"{day:%Y-%m-%d}T{t}"
            if at > now and not too_close(at, taken):
                return at
        day += timedelta(days=1)
    return f"{day:%Y-%m-%d}T{SLOTS[0]}"


def _minutes(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def random_times(n, now=None, rng=random, start=DAY_START, end=DAY_END):
    """n random publish times 'HH:MM' for a day's drafts, ascending, on a 5-minute grid: between
    `start` and `end` Moscow time and at least MIN_GAP minutes apart (less if the rest of the day
    is too short, e.g. a run by hand in the evening). The first time goes to the most relevant post.
    `now` ('YYYY-MM-DDTHH:MM') is the earliest moment; for tomorrow's drafts pass '<tomorrow>T00:00'."""
    now = now or now_msk()
    last = 23 * 60 + 55
    start = min(max(_minutes(start), -(-(_minutes(now[11:16]) + 15) // 5) * 5), last)  # not sooner than in 15 minutes
    end = min(max(_minutes(end), start + 5 * (n - 1)), last)
    gap = min(MIN_GAP, (end - start) // max(n - 1, 1) // 5 * 5)
    slack = (end - start - gap * (n - 1)) // 5
    offsets = sorted(rng.randint(0, slack) for _ in range(n))
    times = [min(start + 5 * o + gap * i, last) for i, o in enumerate(offsets)]
    return [_hhmm(t) for t in times]


def _hhmm(t):
    return f"{t // 60:02d}:{t % 60:02d}"


def digest_time(after):
    """The evening digest's time: DIGEST_TIME, or a bit after the day's last news `after` ('HH:MM') on a late run."""
    return max(DIGEST_TIME, _hhmm(min(_minutes(after) + 5, 23 * 60 + 55)) if after else DIGEST_TIME)
