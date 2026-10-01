# Delta bot

Черновики постов для канала @delta24news приходят в личку от @DeltaRossBot с кнопками
«Опубликовать» / «Отклонить». Одобренные посты публикуются в канал.

- `send_drafts.py` — отправляет черновики из `drafts/*.json` (запускает ежедневная задача Claude).
- `poll.py` — каждые ~5 минут (GitHub Actions) обрабатывает нажатия кнопок и публикует пост.

## Секреты репозитория (Settings → Secrets and variables → Actions)
- `TELEGRAM_BOT_TOKEN` — токен от @BotFather
- `ADMIN_CHAT_ID` — ваш chat id (бот присылает его в ответ на /start)
