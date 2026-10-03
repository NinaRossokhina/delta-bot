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


def draft_buttons(at, image=False, mark=False, now=None, digest=False):
    """Buttons under a draft that is not scheduled yet: three dates (today, tomorrow, the day after);
    the tapped one queues the post on that day at a time the bot picks (poll.py, pick_time): random,
    at least MIN_GAP minutes from other posts, spread over the day. `at` ('YYYY-MM-DDTHH:MM' Moscow)
    gives the day the draft is meant for, marked with "•". digest=True: the evening digest, always
    DIGEST_TIME (dg: instead of dt:). «Сейчас» shows today's date. image=True adds «Другая картинка»
    (posts from voice messages). `mark` is kept for older callers."""
    now = now or now_msk()
    today = now[:10]
    days = [shift_day(today, i) for i in range(3)]
    dates = [{"text": f"{'• ' if d == at[:10] else ''}{WEEKDAYS[datetime.fromisoformat(d).weekday()]}, {ddmm(d)}",
              "callback_data": f"{'dg' if digest else 'dt'}:{d}"} for d in days]
    rows = [dates, [{"text": "Изменить", "callback_data": "edit"},
                    {"text": f"Сейчас, {ddmm(today)}", "callback_data": "pub"},
                    {"text": "Отклонить", "callback_data": "rej"}]]
    if image:
        rows.append([IMAGE_BUTTON])
    return {"inline_keyboard": rows}


def _at_minutes(at):
    return _minutes(at[11:16])


def pick_time(day, taken, now=None, rng=random, digest=False):
    """A publish time 'HH:MM' for a post on `day`, or None if the day has no room: for the digest
    DIGEST_TIME plus a few random minutes (and an hour after the day's last post); otherwise a random
    minute in DAY_START-DAY_END, at least MIN_GAP from the posts in `taken` ('YYYY-MM-DDTHH:MM'),
    in the widest free stretch, so posts tapped one by one spread evenly over the day."""
    now = now or now_msk()
    points = sorted(_at_minutes(t) for t in taken if t[:10] == day)
    start, end = _minutes(DAY_START), _minutes(DAY_END)
    if day == now[:10]:
        start = max(start, _minutes(now[11:16]) + 15)
    if digest:
        t = max([_minutes(DIGEST_TIME)] + [p + MIN_GAP for p in points if p < _minutes(DIGEST_TIME) + MIN_GAP])
        t += rng.randint(0, 14)
        if day == now[:10]:
            t = max(t, start)
        clash = [p for p in points if abs(p - t) < MIN_GAP]
        return _hhmm(t) if t <= 23 * 60 + 55 and not clash else None
    if start > end:
        return None
    if not points:
        return _hhmm(rng.randint(start, end))
    options = []  # (how far from the nearest post, lowest, highest, best time)
    lo, hi = start, points[0] - MIN_GAP
    if lo <= hi:
        options.append((points[0] - lo, lo, hi, lo))
    for a, b in zip(points, points[1:]):
        lo, hi = max(a + MIN_GAP, start), min(b - MIN_GAP, end)
        if lo <= hi:
            options.append(((b - a) / 2, lo, hi, (a + b) // 2))
    lo, hi = max(points[-1] + MIN_GAP, start), end
    if lo <= hi:
        options.append((hi - points[-1], lo, hi, hi))
    if not options:
        return None
    _, lo, hi, best = max(options)
    if best == lo:  # the start of the day: a little after it, not exactly 07:00
        return _hhmm(lo + rng.randint(0, min(30, hi - lo)))
    if best == hi:
        return _hhmm(hi - rng.randint(0, min(30, hi - lo)))
    jitter = min(25, (hi - lo) // 2)
    return _hhmm(min(max(best + rng.randint(-jitter, jitter), lo), hi))


def scheduled_buttons(at, image=False, digest=False):
    """A scheduled draft: «Отменить» brings back draft_buttons (ui: keeps «Другая картинка», ug: the digest)."""
    cancel = "ui" if image else "ug" if digest else "un"
    return {"inline_keyboard": [[
        {"text": f"⏰ {ddmm(at[:10])} в {at[11:]}", "callback_data": "done"},
        {"text": "Отменить", "callback_data": f"{cancel}:{at}"},
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
