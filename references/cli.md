# CLI reference

Все ответы имеют форму `{"meta": {...}, "items": [...]}`. `meta.next_cursor` передаётся в следующую команду как `--cursor`.

## Чаты

```bash
uv run scripts/telegram.py chat search "Мария" --limit 10
uv run scripts/telegram.py chat list --type group --member-max 40 --unread
uv run scripts/telegram.py chat list --last_m 30 --direction incoming
uv run scripts/telegram.py chat show --chat CHAT_ID
```

У `chat list` и `chat search` доступны `--type`, `--visibility`, `--name`, `--username`, `--unread`, `--archived`, `--muted`, `--pinned`, `--has-topics`, `--role`, `--member-min` и `--member-max`.

С `--last_m X`, `--last_h X` или `--date YYYY-MM-DD` список содержит только активные за этот период чаты. `--direction any|incoming|outgoing` выбирает направление активности; если направление не указано, используется `incoming`. Каждый результат содержит `activity` со счётчиками и временем последнего сообщения. Указывать `--direction` без периода нельзя.

## Сообщения

```bash
uv run scripts/telegram.py message list --chat CHAT_ID --limit 20
uv run scripts/telegram.py message search "договор" --chat CHAT_ID --match phrase
uv run scripts/telegram.py message list --direction outgoing --after 2026-01-01 --deleted only
uv run scripts/telegram.py message list --chat GROUP_1 --chat GROUP_2 --personal --last_h 2 --order asc
uv run scripts/telegram.py message list --chat CHAT_ID --date 2026-09-30 --order asc
uv run scripts/telegram.py message show --chat CHAT_ID --message MESSAGE_ID --full
```

`--chat` повторяемый: указанные чаты объединяются через OR. `--personal` добавляет все личные диалоги с людьми и ботами, поэтому его можно сочетать с группами. Фильтры: `--sender`, `--direction`, `--after`, `--before`, `--last_m`, `--last_h`, `--date`, `--deleted any|only|exclude`, `--edited`, `--media`, `--topic`, `--reply-to`, `--reactions`, `--pinned`. Поиск поддерживает `--match all|any|phrase`; он включает текст и локальную расшифровку голосовых.

`--last_m X` и `--last_h X` принимают положительные целые и при совместном использовании складываются. `--date` выбирает локальный календарный день компьютера. Они несовместимы между собой и с `--after`/`--before`. `--order asc|desc` задаёт порядок общей временной ленты; по умолчанию `desc`, для мониторинга и истории за день используй `asc`. Во всех периодических ответах `meta.period` содержит UTC-границы, локальную временную зону и источник периода.

Для короткого ответа используй `--fields chat,message_id,direction,sender,text` или `--max-text-chars N`. Лимит не больше 200; пока `meta.has_more=true`, передавай `meta.next_cursor` следующей команде с теми же фильтрами. `--format text` предназначен для человека и явно печатает чат, стрелку направления, отправителя, ID и статусы.

## Профили, синхронизация и сервис

```bash
uv run scripts/telegram.py profile show --peer @username --fields source,profile
uv run scripts/telegram.py profile show --peer PEER_ID --section admins --limit 20
uv run scripts/telegram.py sync run
uv run scripts/telegram.py sync reconcile
uv run scripts/telegram.py service install
uv run scripts/telegram.py service status
```

`sync reconcile --deep` проверяет всю сохранённую историю подходящих чатов и может занять время. Обычная сверка каждые 5 минут ограничена 200 последними сообщениями в личных чатах и группах до 40 участников.

`service install` создаёт пользовательский LaunchAgent на macOS, user-service systemd на Linux или Task Scheduler на Windows. Он синхронизирует только активный аккаунт.

## Подключение и отправка

```bash
uv run scripts/telegram.py account add --comment "Основной"
uv run scripts/telegram.py account list
uv run scripts/telegram.py allowlist add --chat CHAT_ID --comment "Разрешено писать"
uv run scripts/telegram.py send --chat CHAT_ID --text "Точный текст"
```

При первом `account add` QR-код открывается системным браузером, а API ID/hash и облачный пароль запрашиваются интерактивно. Секреты сохраняются в системном keyring, либо в защищённом локальном файле при отсутствии keyring.
