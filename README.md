# Telegram chat control

Скилл для безопасной работы с Telegram через локальный SQLite-индекс и Telethon. Подходит для Codex, OpenClaw и самостоятельного использования из командной строки.

## Возможности

- поиск чатов и сообщений без повторного обращения к Telegram;
- фильтрация по чатам, отправителям, направлению, периоду, медиа и статусу удаления;
- полнотекстовый поиск по сообщениям и расшифровкам голосовых;
- live-чтение доступных групп и каналов без вступления;
- просмотр профилей, общих групп, подарков и личного канала;
- сохранение сведений об удалённых сообщениях;
- безопасная отправка только в явно разрешённые чаты;
- фоновая синхронизация как системный сервис или OpenClaw-плагин.

## Как это работает

```text
Telegram MTProto
      |
      v
scripts/telegram.py sync run
      |
      v
<state>/data/accounts/<account-id>/telegram.sqlite3
      |
      +--> chat list/search/show
      +--> message list/search/show
```

После синхронизации команды чтения работают с локальной базой и не открывают Telegram-соединение. Подключение требуется для обновления индекса, live-чтения, расширенных данных профиля, загрузки медиа и отправки.

## Быстрый старт

Нужны Python 3.10+ и [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --locked
uv run scripts/telegram.py account add --comment "Основной"
uv run scripts/telegram.py sync run
uv run scripts/telegram.py chat list --limit 20
uv run scripts/telegram.py message list --chat CHAT_ID --limit 20
```

По умолчанию runtime-данные находятся в `~/.codex/telegram-chat-control`. Если существует `~/.openclaw/telegram-chat-control`, используется он. Явный путь задаётся переменной `TELEGRAM_CHAT_CONTROL_HOME`.

## Синхронизация в OpenClaw

Расширение [`telegram-sync-minute`](openclaw-extension/telegram-sync-minute) раз в 60 секунд запускает короткую инкрементальную синхронизацию. Оно не запускает второй процесс, пока предыдущий не завершился.

Пути можно настроить переменными окружения:

- `OPENCLAW_HOME`;
- `TELEGRAM_CHAT_CONTROL_SKILL_DIR`;
- `TELEGRAM_CHAT_CONTROL_HOME`;
- `TELEGRAM_CHAT_CONTROL_UV`.

Подробная установка описана в [docs/installation.md](docs/installation.md).

## Документация

- [Установка](docs/installation.md)
- [Использование](docs/usage.md)
- [Полный справочник CLI](references/cli.md)
- [Модель данных](references/data-model.md)
- [Инструкция для агента](SKILL.md)

## Безопасность

- Сессии, API hash, локальные базы, медиа и приватные ссылки не должны попадать в Git.
- Не запускайте одну Telegram StringSession одновременно на разных машинах: Telegram может отозвать ключ с `AuthKeyDuplicatedError`.
- Отправка разрешена только в чат, заранее добавленный владельцем в локальный allowlist.
- Используйте один механизм фоновой синхронизации: системный сервис или OpenClaw-плагин.
