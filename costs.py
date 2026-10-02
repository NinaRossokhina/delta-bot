"""What the bot spends on Polza AI: every request's price goes to costs.jsonl, /cost sums it up.

Polza returns the price of each request in rubles (usage.cost_rub, also usage.cost). One line per
request: {"t": "2026-10-02T10:00", "task": "voice", "model": "...", "rub": 1.23}; "rub" is null
when the answer had no price. The file only grows by appending lines, so two workflows writing at
the same time merge cleanly (merge=union in .gitattributes).
"""
import json

from schedule import now_msk

FILE = "costs.jsonl"
task = "other"  # what the money is spent on; ai.py sets it to the task kind
TASKS = {"daily": "черновики на день", "chat": "просьбы", "edit": "правки", "voice": "посты из голосовых",
         "image": "картинки", "style": "голос канала", "other": "другое"}
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]


def price(data):
    """Rubles from a Polza answer (a dict or an SDK object with usage), or None if it has no price."""
    if data is None:
        return None
    if not isinstance(data, dict):
        dump = getattr(data, "model_dump", None)
        data = dump() if dump else {"usage": getattr(data, "usage", None)}
    for where in (data.get("usage"), data):
        if not isinstance(where, dict):
            continue
        for key in ("cost_rub", "cost"):
            value = where.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    pass
    return None


def record(model, answer):
    """Append one request's price. Never breaks the task: problems are only printed."""
    try:
        with open(FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": now_msk(), "task": task, "model": model, "rub": price(answer)},
                               ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"could not record the cost: {e!r}")


def load():
    rows = []
    try:
        with open(FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return rows


def rub(x):
    return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _period(rows, prefix):
    rows = [r for r in rows if str(r.get("t", "")).startswith(prefix)]
    total, unknown, by_task = 0.0, 0, {}
    for r in rows:
        if r.get("rub") is None:
            unknown += 1
            continue
        total += r["rub"]
        by_task[r.get("task", "other")] = by_task.get(r.get("task", "other"), 0) + r["rub"]
    return total, len(rows), unknown, by_task


def report(now=None):
    """The /cost answer for Nina: today and this month (Moscow time)."""
    now = now or now_msk()
    rows = load()
    lines = ["Расходы на Polza AI"]
    for title, prefix in ((f"Сегодня, {now[8:10]}.{now[5:7]}", now[:10]),
                          (f"{MONTHS[int(now[5:7]) - 1].capitalize()} {now[:4]}", now[:7])):
        total, n, unknown, by_task = _period(rows, prefix)
        lines.append("")
        lines.append(f"{title}: {rub(total)}, запросов: {n}")
        for kind, value in sorted(by_task.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {TASKS.get(kind, kind)}: {rub(value)}")
        if unknown:
            lines.append(f"  без цены в ответе Polza: {unknown}")
    return "\n".join(lines)
