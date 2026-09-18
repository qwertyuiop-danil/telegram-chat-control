# Установка

## Требования

- Python 3.10 или новее;
- `uv`;
- Telegram API ID и API hash с `my.telegram.org`;
- доступ к Telegram MTProto напрямую или через HTTP/SOCKS5-прокси;
- для минутной синхронизации — OpenClaw с поддержкой `definePluginEntry` и сервисов плагина.

Зафиксированные Python-зависимости находятся в `uv.lock`.

## Standalone-установка

Из корня репозитория:

```bash
uv sync --locked
uv run scripts/telegram.py --help
```

Чтобы хранить runtime-данные в явном каталоге:

```bash
export TELEGRAM_CHAT_CONTROL_HOME="$HOME/.local/share/telegram-chat-control"
```

Если переменная не задана, скилл выбирает:

1. `~/.openclaw/telegram-chat-control`, если каталог уже существует;
2. иначе `~/.codex/telegram-chat-control`.

### Подключение аккаунта

```bash
uv run scripts/telegram.py account add --comment "Основной"
```

Команда интерактивно запросит API ID/hash, создаст временный QR-код и будет ждать сканирования в Telegram. При включённом облачном пароле она запросит его в терминале. QR-файл удаляется после завершения.

На macOS и обычном desktop-сеансе сначала используется системный keyring. На headless Linux применяется приватный файл `<state>/secrets.json` с правами `0600`.

Проверьте подключение и выполните первую синхронизацию:

```bash
uv run scripts/telegram.py account list
uv run scripts/telegram.py sync run
uv run scripts/telegram.py sync status
```

Обычный первый `sync run` ограничивает новый диалог последними 200 сообщениями. Для осознанной загрузки всей доступной истории используйте `sync run --full`: команда очищает локальные индексные таблицы этого аккаунта и заново наполняет их из Telegram, поэтому может выполняться долго.

### Фоновый standalone-сервис

```bash
uv run scripts/telegram.py service install
uv run scripts/telegram.py service status
```

`service install` создаёт пользовательский LaunchAgent на macOS, systemd user unit на Linux или задачу Task Scheduler на Windows. Долгоживущий процесс принимает события и выполняет инкрементальную синхронизацию примерно раз в 5 секунд; раз в 5 минут он сверяет недавние удаления в подходящих чатах.

Управление:

```bash
uv run scripts/telegram.py service start
uv run scripts/telegram.py service stop
uv run scripts/telegram.py service uninstall
```

## Прокси

Приоритет переменных:

1. `TELEGRAM_CHAT_CONTROL_PROXY`;
2. `HTTPS_PROXY`;
3. `HTTP_PROXY`.

Поддерживаются `http://`, `https://` и `socks5://`. Для системного сервиса переменная должна быть задана в его окружении, а не только в интерактивной оболочке.

Пример:

```bash
export TELEGRAM_CHAT_CONTROL_PROXY=http://proxy.example:1080
uv run scripts/telegram.py sync run
```

## Установка в OpenClaw с синхронизацией раз в минуту

Минутное расширение не заменяет Python-скилл: оно только запускает его команду `sync run` по таймеру.

### 1. Разместите скилл

По умолчанию OpenClaw-версия использует `$OPENCLAW_HOME/skills/telegram-chat-control`, где `OPENCLAW_HOME` равен `$HOME/.openclaw`, если не задан явно.

Скопируйте туда содержимое репозитория без каталога `openclaw-extension`, затем подготовьте зависимости:

```bash
export OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"
cd "$OPENCLAW_HOME/skills/telegram-chat-control"
uv sync --locked
```

Runtime-каталог по умолчанию:

```text
$OPENCLAW_HOME/telegram-chat-control
```

Подключите аккаунт интерактивно или безопасно перенесите уже созданные секреты отдельно от Git. Не копируйте работающую StringSession с другой машины, пока прежний процесс использует её.

### 2. При необходимости настройте пути

[`index.ts`](../openclaw-extension/telegram-sync-minute/index.ts) строит пути относительно `OPENCLAW_HOME`. Их можно переопределить без изменения исходников:

- `OPENCLAW_HOME` — корень данных OpenClaw;
- `TELEGRAM_CHAT_CONTROL_SKILL_DIR` — каталог Python-скилла;
- `TELEGRAM_CHAT_CONTROL_HOME` — каталог runtime-данных Telegram;
- `TELEGRAM_CHAT_CONTROL_UV` — команда или абсолютный путь к `uv`.

Переменные должны быть доступны процессу gateway.

### 3. Установите расширение одним способом

Способ A — workspace discovery:

```text
<openclaw-workspace>/.openclaw/extensions/telegram-sync-minute/
```

Скопируйте туда три файла из `openclaw-extension/telegram-sync-minute`: `index.ts`, `openclaw.plugin.json`, `package.json`.

Способ B — явная link-установка из другого каталога:

```bash
openclaw plugins install --link /ABSOLUTE/PATH/telegram-sync-minute
```

Не сочетайте оба способа для одного ID: иначе OpenClaw выдаёт предупреждение о дублирующемся `telegram-sync-minute`.

Затем включите плагин:

```bash
openclaw plugins enable telegram-sync-minute
openclaw plugins inspect telegram-sync-minute --runtime --json
```

Плагин активируется при старте gateway. Перезапуск gateway является сервисным действием: выполняйте его отдельно в согласованное окно и только для нужного сервиса.

### 4. Проверка

После запуска gateway:

```bash
openclaw plugins list --json
```

Для `telegram-sync-minute` должны быть `enabled: true` и рабочий статус. В логах gateway ожидается строка:

```text
Telegram index sync scheduled every 60 seconds
```

Проверьте свежесть индекса непосредственно через Python-скилл:

```bash
cd "$OPENCLAW_HOME/skills/telegram-chat-control"
TELEGRAM_CHAT_CONTROL_HOME="$OPENCLAW_HOME/telegram-chat-control" \
uv run scripts/telegram.py sync status
```

`heartbeat_age_seconds` и `meta.sync_age_seconds` должны обновляться. Если плагин запускается раз в минуту, возраст последней успешной синхронизации обычно не должен устойчиво расти дольше нескольких интервалов.

## Обновление

1. Остановите выбранный механизм фоновой синхронизации.
2. Сделайте резервную копию runtime-каталога, особенно `accounts.json`, секретов и `data/accounts/*/telegram.sqlite3`.
3. Замените исходники и выполните `uv sync --locked`.
4. Запустите `uv run --locked python -m unittest discover -s tests -v`.
5. Выполните один `sync run`, проверьте `sync status` и только затем верните фоновый механизм.

Не запускайте один Telegram-сеанс одновременно со старой и новой копией.
