# Delta bot

Черновики постов для канала @delta24news приходят в личку от @DeltaRossBot с кнопками
«Опубликовать» / «Отклонить». Одобренные посты публикуются в канал.

- `send_drafts.py` — отправляет черновики из `drafts/*.json`. Claude каждое утро добавляет новый файл в `drafts/`, и workflow `send.yml` сразу присылает черновики в Telegram.
- `poll.py` — каждые ~5 минут (GitHub Actions) обрабатывает нажатия кнопок и публикует пост.

## Секрет репозитория (Settings → Secrets and variables → Actions)
- `TELEGRAM_BOT_TOKEN` — токен от @BotFather

