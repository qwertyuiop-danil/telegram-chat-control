# Использование

Все команды выполняются из корня скилла:

```bash
uv run scripts/telegram.py <команда>
```

Обычный ответ — JSON с двумя верхними полями:

- `meta`: аккаунт, свежесть индекса, лимит, пагинация и предупреждения;
- `items`: найденные объекты.

## Что работает без подключения к Telegram

После хотя бы одной синхронизации следующие команды читают SQLite напрямую:

```bash
uv run scripts/telegram.py chat list --limit 20
uv run scripts/telegram.py chat search "Название" --limit 10
uv run scripts/telegram.py chat show --chat CHAT_ID
uv run scripts/telegram.py message list --chat CHAT_ID --limit 20
uv run scripts/telegram.py message search "фраза" --chat CHAT_ID --match phrase
uv run scripts/telegram.py message show --chat CHAT_ID --message MESSAGE_ID --full
uv run scripts/telegram.py account list
uv run scripts/telegram.py allowlist list
uv run scripts/telegram.py sync status
```

SQLite находится в `<state>/data/accounts/<account-id>/telegram.sqlite3`. Команды чтения не запускают `sync run` автоматически: проверяйте `meta.sync_age_seconds`, если важна свежесть.

## Что требует Telegram-соединения

- `sync run`, `sync reconcile`;
- `chat open`;
- `profile show --refresh`, `--photo` и API-секции профиля;
- `message transcribe`, `message photo`;
- изменение allowlist по Telegram peer;
- `send`;
- `account add`.

Некоторые обычные `profile show` могут вернуться из локального кэша, но на это не следует рассчитывать для актуальных сведений.

## Типичный цикл чтения

Сначала найдите нужный чат:

```bash
uv run scripts/telegram.py chat search "Мария" --limit 10
```

Возьмите точный `chat_id` из результата и читайте историю:

```bash
uv run scripts/telegram.py message list --chat CHAT_ID --limit 20
```

Или ищите текст только в этом чате:

```bash
uv run scripts/telegram.py message search "договор" --chat CHAT_ID --match phrase --limit 20
```

Всегда учитывайте:

- `direction=outgoing` — сообщение владельца подключённого аккаунта;
- `direction=incoming` — входящее сообщение;
- `deleted.value=true` — Telegram-сообщение удалено, но сохранённый локальный текст оставлен в индексе;
- `meta.has_more=true` — есть следующая страница; передайте `meta.next_cursor` как `--cursor` с теми же фильтрами.

## Синхронизация

Один инкрементальный проход:

```bash
uv run scripts/telegram.py sync run
```

Полная переиндексация доступной истории:

```bash
uv run scripts/telegram.py sync run --full
```

Проверка удалений:

```bash
uv run scripts/telegram.py sync reconcile
uv run scripts/telegram.py sync reconcile --deep
```

`--deep` проверяет всю сохранённую историю подходящих чатов и может быть медленным.

Есть два фоновых режима, из которых следует выбрать один:

- `service install` — постоянное соединение, события и проходы примерно раз в 5 секунд;
- `telegram-sync-minute` — отдельный короткий `sync run` раз в 60 секунд под управлением OpenClaw.

Минутный плагин делает инкрементальную загрузку новых сообщений и состояния диалогов, но сам не запускает `sync reconcile` и не получает Telegram-события о редактировании. Правки надёжно ловит постоянный сервис; удаления — постоянный сервис либо отдельный периодический `sync reconcile`.

Python-скилл использует `<state>/sync.lock`, а расширение не запускает новый дочерний процесс, пока предыдущий не завершился. Ответ `already_running` означает, что другой проход уже держит lock.

## Периодический просмотр новых сообщений

Найдите чаты с недавними входящими сообщениями:

```bash
uv run scripts/telegram.py chat list --last_m 30 --direction incoming --limit 20
```

Затем получите общую хронологическую ленту:

```bash
uv run scripts/telegram.py message list \
  --chat GROUP_ID \
  --personal \
  --last_h 2 \
  --order asc \
  --limit 20
```

`--chat` можно повторять. `--personal` добавляет личные диалоги с людьми и ботами.

## Live-чтение без записи в индекс

```bash
uv run scripts/telegram.py chat open --peer @channel --limit 20
uv run scripts/telegram.py chat open --peer "https://t.me/+INVITE_HASH" --limit 20
```

`chat open` обращается к Telegram API, но не добавляет историю в SQLite и никогда не вступает в чат. Приватная invite-ссылка читается только если аккаунт уже является участником.

## Профили

```bash
uv run scripts/telegram.py profile show --peer @username --refresh --photo
uv run scripts/telegram.py profile show --peer @username --section common-groups --limit 20
uv run scripts/telegram.py profile show --peer @username --section gifts --limit 20
uv run scripts/telegram.py profile show --peer @username --section personal-channel --limit 20
```

Секции используют собственную пагинацию. Не смешивайте их cursor с cursor списка сообщений.

## Безопасная отправка

Отправка закрыта allowlist. Сначала владелец явно разрешает чат:

```bash
uv run scripts/telegram.py allowlist add --chat CHAT_ID --comment "Разрешено писать"
```

После этого:

```bash
uv run scripts/telegram.py send --chat CHAT_ID --text "Точный текст"
```

Удалить разрешение:

```bash
uv run scripts/telegram.py allowlist remove --chat CHAT_ID
```

Скилл не добавляет получателей в allowlist автоматически.

## Диагностика

```bash
uv run scripts/telegram.py account list
uv run scripts/telegram.py sync status
uv run scripts/telegram.py service status
```

Типичные случаи:

- `No active Telegram account` — выполните `account add` или выберите аккаунт через `account use --id ID`;
- `already_running` — дождитесь текущего прохода, не удаляйте lock вслепую;
- растущий `sync_age_seconds` — фоновый механизм не выполняет успешный проход;
- `AuthKeyDuplicatedError` — один StringSession использовался параллельно на разных машинах; остановите все копии и создайте новую авторизацию;
- ошибки соединения через прокси — явно задайте `TELEGRAM_CHAT_CONTROL_PROXY=http://proxy.example:1080` в окружении процесса;
- `Recipient is not in ... allowlist` — отправка безопасно отклонена, сначала требуется явное разрешение владельца.

Полный набор фильтров и примеры пагинации приведены в [CLI reference](../references/cli.md).
