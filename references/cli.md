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

Если сеть требует прокси для MTProto, задай `TELEGRAM_CHAT_CONTROL_PROXY` как `http://HOST:PORT` или `socks5://HOST:PORT` в окружении сервиса. Если она не задана, клиент последовательно проверяет `HTTPS_PROXY`, затем `HTTP_PROXY`. Явная `TELEGRAM_CHAT_CONTROL_PROXY` всегда имеет приоритет.

## Подключение и отправка

```bash
uv run scripts/telegram.py account add --comment "Основной"
uv run scripts/telegram.py account list
uv run scripts/telegram.py allowlist add --chat CHAT_ID --comment "Разрешено писать"
uv run scripts/telegram.py send --chat CHAT_ID --text "Точный текст"
```

При первом `account add` QR-код открывается системным браузером, а API ID/hash и облачный пароль запрашиваются интерактивно. Секреты сохраняются в системном keyring, либо в защищённом локальном файле при отсутствии keyring.

## Исследование человека

```bash
uv run scripts/telegram.py profile show --peer @username --refresh --photo
uv run scripts/telegram.py profile show --peer @username --section common-groups --limit 20
uv run scripts/telegram.py profile show --peer @username --section common-groups --limit 20 --cursor RAW_CHAT_ID
uv run scripts/telegram.py profile show --peer @username --section gifts --limit 20
uv run scripts/telegram.py profile show --peer @username --section gifts --limit 20 --cursor NEXT_OFFSET
```

`profile.link` — публичная ссылка или Telegram deep link; `profile.about` — описание без обрезки; `profile.personal_channel` — прикреплённый канал. `--photo` скачивает текущую доступную аватарку в локальную папку аккаунта, возвращая `profile.photo_path` (null, если фото нет). Открой этот файл инструментом просмотра изображений.

Секции `common-groups` и `gifts` читаются из API, не из кэша. Общие группы находятся в `items[0].common_groups`; подарки — в `items[0].gifts`. Обе секции используют `meta.has_more/next_cursor`; для полного списка повторяй запрос с теми же peer/section до конца. Не используй cursor сообщений для профилей. Лимит 1–200, по умолчанию 20; точная полная страница групп может потребовать завершающего пустого запроса.

Подарки нормализованы: ID, название/ссылка коллекционного подарка (если есть), дата, даритель с именем и ссылкой, подпись `message`. `original_details` — отдельное первоначальное посвящение коллекционного подарка. Скрытый даритель не раскрывается; отсутствие дарителя или подписи не восстанавливается догадками. Запрашиваются только отображаемые в профиле подарки (`exclude_unsaved=True`). Если метод отсутствует в установленной Telethon или Telegram запрещает чтение, ответ явно содержит `*_unavailable`; остальные сведения и фото остаются доступны.

API: [общие группы](https://core.telegram.org/method/messages.getCommonChats), [подарки](https://core.telegram.org/method/payments.getSavedStarGifts), [поля подарка](https://core.telegram.org/constructor/savedStarGift). Нормализованный `gifts` заменяет прежний сырой TL-объект.

### Просмотр личного канала

```bash
uv run scripts/telegram.py profile show --peer @username --section personal-channel --limit 20
uv run scripts/telegram.py profile show --peer @username --section personal-channel --limit 20 --cursor MESSAGE_ID
```

Возвращает `personal_channel` (ID, название, ссылка, описание, число участников) и `posts` — последние посты по убыванию ID. `meta.next_cursor` позволяет читать более ранние посты. У поста указаны чат, отправитель/подпись, направление, дата, ссылка, тип медиа и до 500 символов текста с `text_truncated`. Полный текст доступен в Telegram по ссылке либо через `message show --chat CHANNEL_ID --message MESSAGE_ID --full`, если пост уже в локальном индексе. Медиа здесь не скачивается. Если личный канал не прикреплён, возвращаются null и пустые posts; запрет чтения отмечается отдельно и не означает отсутствия канала.

## Сообщения выбранных людей и приватные ссылки

```bash
uv run scripts/telegram.py message list --chat GROUP_ID --sender USER_ID --limit 20
uv run scripts/telegram.py message list --chat GROUP_ID --sender @alice --sender USER_ID_2
uv run scripts/telegram.py message search "встреча" --chat GROUP_ID --sender USER_ID
uv run scripts/telegram.py chat open --peer "https://t.me/+INVITE_HASH" --limit 20
uv run scripts/telegram.py chat open --peer "https://t.me/c/CHANNEL_ID/POST_ID" --limit 20
uv run scripts/telegram.py chat open --peer GROUP_ID --sender @alice --limit 20
```

У `message list/search` повторяемые `--sender` объединяются через OR, а группа и остальные фильтры — через AND. ID стабилен; username сравнивается без учёта регистра с локальным `entities.username`. Непроиндексированный или устаревший username может дать пустой результат: уточни ID или используй API.

`chat open` — чтение API без изменения членства и без записи истории в индекс. Принимает ID, username либо Telegram-ссылку; `--sender` здесь только один, фильтруется сервером Telegram. `meta.source=telegram_api`, `meta.chat` содержит профиль/описание чата, `items` — обычные нормализованные сообщения. `--limit` 1–200, `--cursor` — ID последнего сообщения предыдущей страницы; повторяй исходные peer/sender. `--max-text-chars` по умолчанию 500, можно увеличить для чтения полного текста. Не смешивай этот cursor с cursor локального `message list`.

Invite проверяется только через [messages.checkChatInvite](https://core.telegram.org/method/messages.checkChatInvite). Разрешено чтение исключительно при `ChatInviteAlready`; `ChatInvitePeek` не подтверждает членство. `t.me/c` разрешается по текущим диалогам, даже если StringSession не сохранила access hash. Истёкшая/недействительная ссылка не доказывает, что аккаунт покинул канал; требуется независимо известный ID или точное совпадение диалога. Вступление и заявки запрещены, обхода через join/import нет. Invite-ссылки приватны: не публикуй их в отчётах без необходимости.
