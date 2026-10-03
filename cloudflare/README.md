# «Дверной звонок» на Cloudflare Workers

GitHub запускает `poll.yml` по расписанию с большими задержками (бывает 10–30 минут). Воркер
`worker.js` на Cloudflare раз в минуту смотрит:

- есть ли у бота новые сообщения или нажатия кнопок;
- есть ли в `queue.json` пост, которому пора выйти (время по Москве).

Если есть, он сразу запускает `poll.yml` через GitHub API. Ещё раз в 5 минут он запускает
`hot.yml`, поиск срочных новостей, а в 21:00 по Екатеринбургу (16:00 UTC) `ai.yml`, сбор черновиков на завтра
(у GitHub это расписание опаздывало на часы или пропускалось). Сообщения воркер только «подсматривает»:
он их не подтверждает, поэтому `poll.py` получает их как обычно. Если `poll.yml` уже идёт, второй
запуск не делается; если последний запуск упал, повтор не чаще раза в 5 минут. Расписание в
`poll.yml` остаётся как запасной вариант.

Всё помещается в бесплатный тариф Cloudflare (1 440 запусков в сутки при лимите 100 000).

## Что понадобится

- Аккаунт GitHub с доступом к репозиторию `NinaRossokhina/delta-bot`.
- Токен бота от @BotFather (тот же, что в секрете `TELEGRAM_BOT_TOKEN` на GitHub).
- Бесплатный аккаунт Cloudflare: https://dash.cloudflare.com/sign-up (почта и пароль, карта не нужна).

## Шаг 1. Токен GitHub

1. Откройте https://github.com/settings/personal-access-tokens/new
   (или: аватар справа вверху → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token).
2. **Token name**: `delta-bot-doorbell`.
3. **Expiration**: выберите срок (например, 1 год) и запишите себе дату: когда токен истечёт, звонок
   перестанет работать, и нужно будет сделать новый (шаг 1) и заменить секрет (шаг 3).
4. **Repository access** → **Only select repositories** → выберите `NinaRossokhina/delta-bot`.
5. **Permissions** → **Repository permissions**:
   - **Actions** → **Read and write** (запуск `poll.yml`);
   - **Contents** → **Read-only** (чтение `queue.json`; пока репозиторий публичный, работает и без
     этого, но так надёжнее). **Metadata: Read-only** добавится сам.
6. Нажмите **Generate token** и скопируйте токен (начинается с `github_pat_`). GitHub покажет его
   только один раз; никуда не публикуйте его.

## Шаг 2. Создать воркер

1. Войдите в https://dash.cloudflare.com.
2. В меню слева: **Compute (Workers)** → **Workers & Pages** (в некоторых версиях панели просто **Workers & Pages**).
3. **Create** → во вкладке **Workers** выберите **Start with Hello World!** (или **Create Worker**).
4. В поле имени впишите `delta-bot-doorbell` и нажмите **Deploy**.
5. Нажмите **Edit code** (или **Continue to project** → кнопка **Edit code** справа вверху).
6. В редакторе слева откройте файл `worker.js`, удалите всё, что там есть, и вставьте целиком
   содержимое файла [`cloudflare/worker.js`](worker.js) из репозитория
   (на GitHub: открыть файл → кнопка **Copy raw file**).
7. Нажмите **Deploy** справа вверху и вернитесь назад стрелкой рядом с именем воркера.

## Шаг 3. Секреты

1. На странице воркера: **Settings** → **Variables and Secrets** → **+ Add**.
2. **Type**: `Secret`, **Variable name**: `TELEGRAM_BOT_TOKEN`, **Value**: токен бота от @BotFather.
3. Ещё раз **+ Add**: **Type** `Secret`, **Variable name** `GITHUB_TOKEN`, **Value**: токен из шага 1.
4. Нажмите **Deploy** (или **Save**). Имена пишите точно так, большими буквами.

Секреты после сохранения не видны даже вам; чтобы заменить, нажмите на карандаш рядом и введите новое значение.

## Шаг 4. Запуск раз в минуту

1. **Settings** → **Trigger Events** (в старой панели **Triggers**) → **+ Add** → **Cron Triggers**.
2. Выберите **Cron expression** (или вкладку **Custom**) и впишите `* * * * *` (пять звёздочек через пробел = каждую минуту).
3. **Add** / **Save**.

## Шаг 5. Проверка

1. На странице воркера найдите **Domains & Routes**: там адрес вида
   `https://delta-bot-doorbell.<ваше-имя>.workers.dev`. Откройте его в браузере. Ответ:
   - `idle` — всё настроено, сейчас делать нечего;
   - `new messages: started` или `post due: started` — `poll.yml` запущен;
   - `... already running` — `poll.yml` уже идёт, ждём его;
   - `error: ...` — смотрите таблицу ниже.
2. Напишите боту что-нибудь в Telegram и подождите минуту. На GitHub в **Actions** → **Publish
   approved posts** должен появиться новый запуск с пометкой `workflow_dispatch`.
3. Журнал воркера: страница воркера → **Observability** (или **Logs**) → **Live** / **Begin log stream**;
   каждая минута пишет одну строку из списка выше.

## Если что-то не так

| Ответ | Что делать |
|---|---|
| `error: secrets TELEGRAM_BOT_TOKEN / GITHUB_TOKEN are not set` | Шаг 3: проверьте имена секретов и нажмите Deploy. |
| `error: Telegram: 401 Unauthorized` | Неверный токен бота, замените секрет `TELEGRAM_BOT_TOKEN`. |
| `error: queue.json: 401`, `error: runs: 401` | Токен GitHub неверный или истёк: шаг 1, затем замените `GITHUB_TOKEN`. |
| `error: dispatch failed 403` / `404` | У токена нет **Actions: Read and write** или не выбран репозиторий `delta-bot` (шаг 1). |
| `hot news: error 404` (в логах воркера) | Workflow `hot.yml` ещё не попал в ветку `main` или у токена нет **Actions: Read and write**. |
| `... last run failed, waiting` | Последний `poll.yml` упал; откройте его в GitHub Actions и посмотрите ошибку. Воркер повторит через 5 минут. |

Выключить звонок: **Settings** → **Trigger Events** → удалить Cron Trigger (бот продолжит работать
по расписанию GitHub). Обновить код: снова **Edit code**, вставить новый `worker.js`, **Deploy**.

## Для разработчика

Тесты на заглушках, без сети: `node --test cloudflare/worker.test.js` (Node 18+).
