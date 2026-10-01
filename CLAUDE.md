# Delta bot

Telegram-бот канала @delta24news: присылает Нине черновики постов с кнопками, публикует одобренные по расписанию,
по её просьбам пишет и правит посты через нейросеть. Подробности в README.md.

## Устройство
- `poll.py` (workflow `poll.yml`, каждые ~5 минут) — кнопки и сообщения, очередь `queue.json`, вопросы бота `pending.json`, публикация по времени.
- `ai.py` (workflow `ai.yml`) — задача `daily` (вручную: 5–7 новостей дня по JSON-схеме → `drafts/<дата>.json` → коммит → `send.yml`) и задачи `chat` и `edit` из `poll.py`: веб-поиск свежих новостей, посты по `style-guide.md` с учётом `feedback.md`, отправка через `send_drafts.send`.
- `polza.py` — общие функции Polza AI. Все нейросети (текст, позже голосовые и картинки) идут только через Polza: OpenAI-совместимый клиент, `base_url` https://polza.ai/api/v1, модель `anthropic/claude-sonnet-5.5`. Документация: https://polza.ai/docs (список страниц: https://polza.ai/docs/llms.txt); параметры брать из неё, не угадывать.
- `send_drafts.py`, `schedule.py`, `tg.py`, `drafthtml.py` — Telegram и расписание.
- Тесты на заглушках, без сети: `pip install openai`, затем `python -m unittest discover -s tests -v`.

## Правила
- Ключи и токены только из переменных окружения (`POLZA_API_KEY`, `TELEGRAM_BOT_TOKEN`, секреты GitHub). Не писать их в код, не печатать в логи и в ответы.
- Все тексты для Нины (сообщения бота, ответы, кнопки) на русском.
- Время везде по Москве (`schedule.now_msk()`).
