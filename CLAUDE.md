# Delta bot

Telegram-бот канала @delta24news: присылает Нине черновики постов с кнопками, публикует одобренные по расписанию,
по её просьбам пишет и правит посты через нейросеть. Подробности в README.md.

## Устройство
- `poll.py` (workflow `poll.yml`, каждые ~5 минут) — кнопки и сообщения (голосовые передаёт в `ai.py` задачей `voice` с заранее выбранным временем из `slots.json`), очередь `queue.json`, вопросы бота `pending.json`, публикация по времени (опубликованный голосовой пост после правок дописывается в `style/examples.md`), команда `/cost`. Если Telegram недоступен (`tg.Unavailable`), необработанные обновления не подтверждаются, а посты остаются в очереди.
- `ai.py` (workflow `ai.yml`) — задача `daily` (утром и вручную: 7 новостей дня, в основном AI и технологии, по JSON-схеме по убыванию актуальности, каждой случайное время `schedule.random_times` в 07:00–21:00 с промежутком от часа, плюс дайджест последним в 22:00 (`schedule.digest_time`) → `drafts/<дата>.json` → коммит → `send.yml`) и задачи `chat`, `edit`, `voice` (расшифровка → пост голосом Нины по `style/` → картинка → черновик) и `image` («Другая картинка») из `poll.py`: веб-поиск свежих новостей, посты по `style-guide.md` с учётом `feedback.md`, отправка через `send_drafts.send`.
- `polza.py` — общие функции Polza AI. Все нейросети (текст; расшифровка голосовых `transcribe` → POST /audio/transcriptions; картинки `media` → POST /media и опрос GET /media/{id}) идут только через Polza: OpenAI-совместимый клиент, `base_url` https://polza.ai/api/v1, модель `anthropic/claude-sonnet-5.5`. Документация: https://polza.ai/docs (список страниц: https://polza.ai/docs/llms.txt); параметры брать из неё, не угадывать.
- `style/` — голос канала: `build_style.py` (выгрузка Telegram Desktop `result.json` → `posts.txt`), `analyze_style.py` (через Polza → `my-voice.md` и `examples.md`), запуск вручную workflow `style.yml`.
- `costs.py` — цена каждого запроса к Polza (`usage.cost_rub`) строкой в `costs.jsonl`, отчёт для `/cost`. `polza.py` записывает её сам; `costs.task` — на что потрачено.
- `send_drafts.py`, `schedule.py`, `tg.py` (повторы при сбоях, токен не попадает в ошибки), `drafthtml.py` — Telegram и расписание.
- Состояние коммитит `save_state.sh` (повтор с rebase). `feedback.md`, `costs.jsonl`, `images.jsonl` только дописываются строками и склеиваются `merge=union` (`.gitattributes`): параллельные запуски `ai.yml` не конфликтуют. `ai.py` сам останавливает задачу через 16 минут (`Deadline`) и говорит Нине.
- Тесты на заглушках, без сети: `pip install openai`, затем `python -m unittest discover -s tests -v`.
- `cloudflare/worker.js` — «дверной звонок» на Cloudflare Workers (cron каждую минуту запускает `poll.yml`, если есть новые обновления или пост по времени; обновления только подсматривает, без `offset`). Настройка: `cloudflare/README.md`, тесты: `node --test cloudflare/worker.test.js`.

## Правила
- Ключи и токены только из переменных окружения (`POLZA_API_KEY`, `TELEGRAM_BOT_TOKEN`, секреты GitHub). Не писать их в код, не печатать в логи и в ответы.
- Все тексты для Нины (сообщения бота, ответы, кнопки) на русском.
- Время везде по Москве (`schedule.now_msk()`).
