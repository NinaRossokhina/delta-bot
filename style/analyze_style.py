"""Nina's voice from her channel posts via Polza AI: style/posts.txt -> style/my-voice.md and style/examples.md.

The model writes the voice description and picks 10–15 typical posts by id; examples.md quotes those posts verbatim.
Needs POLZA_API_KEY. Run after build_style.py: python style/analyze_style.py
"""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import costs
import polza
from build_style import HERE, header, read

costs.task = "style"  # its price shows in /cost as «голос канала»
MAX_CHARS = 400_000  # a very big channel is sampled evenly to fit the model's context

PROMPT = """Ниже посты из Telegram-канала Нины, каждый с заголовком [пост <id> · <дата>].
Изучи, как она пишет, и составь описание её голоса, по которому другой автор (или нейросеть) сможет писать неотличимо от неё.

Поле voice: Markdown-документ на русском, начинается с «# Мой голос». Разделы:
## Тон
## Длина (в словах или знаках, типичная и разброс; посчитай по постам, а не на глаз)
## Как начинаю
## Как заканчиваю
## Абзацы и форматирование
## Эмодзи (какие, сколько, где; если не использует — так и напиши)
## Любимые слова и обороты
## Чего я не делаю
## Короткая памятка (5–8 пунктов для быстрой проверки черновика)
Пиши от первого лица Нины («я пишу…»), конкретно, с короткими цитатами из постов как примерами. Не выдумывай того, чего нет в постах.

Поле examples: 10–15 самых характерных постов (id из заголовка) разных типов и длины, у каждого одна строка, чем он характерен."""

SCHEMA = {
    "type": "object",
    "properties": {
        "voice": {"type": "string"},
        "examples": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "why": {"type": "string"}},
            "required": ["id", "why"], "additionalProperties": False}},
    },
    "required": ["voice", "examples"],
    "additionalProperties": False,
}


def sample(items, limit=MAX_CHARS):
    """All posts if they fit into `limit` characters, else an even sample of them, in order."""
    total = sum(len(t) for _, _, t in items)
    if total <= limit:
        return items
    return items[::math.ceil(total / limit)]


def analyze(items):
    corpus = "\n\n".join(f"{header(i, d)}\n{t}" for i, d, t in sample(items))
    choice = polza.chat([{"role": "system", "content": PROMPT}, {"role": "user", "content": corpus}],
                        response_format=polza.json_schema("voice", SCHEMA))
    return json.loads(polza.text(choice.message))


def examples_md(items, picked):
    by_id = {i: (d, t) for i, d, t in items}
    parts = ["# Образцы постов\n\nСамые характерные посты канала, дословно. Отобраны вместе с my-voice.md."]
    for n, ex in enumerate((e for e in picked if e["id"] in by_id), 1):
        date, text = by_id[ex["id"]]
        parts.append(f"## {n}. Пост {ex['id']} · {date}\n\n_{ex['why']}_\n\n{text}")
    return "\n\n".join(parts) + "\n"


def main():
    items = read(HERE / "posts.txt")
    result = analyze(items)
    (HERE / "my-voice.md").write_text(result["voice"].strip() + "\n", encoding="utf-8")
    (HERE / "examples.md").write_text(examples_md(items, result["examples"]), encoding="utf-8")
    print(f"{len(items)} постов разобрано, образцов: {len(result['examples'])}")


if __name__ == "__main__":
    main()
