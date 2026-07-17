# Стенды SverkAI

Рекомендуемая схема для тестового периода:

| Стенд | Ветка GitHub | Назначение |
| --- | --- | --- |
| production | `main` | Публичный рабочий сайт для пользователей |
| staging | `staging` | Проверка новых правок, новых актов и админских сценариев до выката |

## Railway: production

Переменные окружения:

- `SVERKAI_ENV=production`
- `ANTHROPIC_API_KEY=<общий гостевой ключ>`
- `ADMIN_SECRET=<секрет администратора>`
- `SVERKAI_ALLOWED_KEY_HASHES=<хеши пользовательских ключей, если храним через env>`
- `SVERKAI_GUEST_KEY_HASHES=<хеш гостевого ключа, чтобы он не мог быть личным входом>`
- `SVERKAI_GUEST_RECONCILE_LIMIT=2`
- `SVERKAI_GUEST_EXPERT_AUDIT_LIMIT=1`
- `SVERKAI_GUEST_USAGE_WINDOW_DAYS=30`
- `SVERKAI_GUEST_MAX_FILE_MB=2`
- `SVERKAI_USER_MAX_FILE_MB=10`

Деплой: из ветки `main`.

Постоянный белый список ключей:

- ключи, которые должны переживать redeploy, хранить в Railway Volume `/app/data/allowed_keys.json` или в `SVERKAI_ALLOWED_KEY_HASHES`;
- ключи, добавленные через админку, сохраняются в Railway Volume и переживают следующий redeploy;
- локально можно получить готовые env-строки командой `python manage_keys.py env`.

## Railway: staging

Создать отдельный Railway service или environment и привязать к ветке `staging`.

Переменные окружения:

- `SVERKAI_ENV=staging`
- `ANTHROPIC_API_KEY=<тестовый или тот же гостевой ключ>`
- `ADMIN_SECRET=<отдельный секрет для staging>`
- `SVERKAI_ALLOWED_KEY_HASHES=<тестовые пользователи>`
- `SVERKAI_GUEST_KEY_HASHES=<хеш гостевого ключа>`
- `SVERKAI_GUEST_RECONCILE_LIMIT=2`
- `SVERKAI_GUEST_EXPERT_AUDIT_LIMIT=1`
- `SVERKAI_GUEST_USAGE_WINDOW_DAYS=30`
- `SVERKAI_GUEST_MAX_FILE_MB=2`
- `SVERKAI_USER_MAX_FILE_MB=10`

Деплой: из ветки `staging`.

## Правила работы

- Новые правки сначала отправлять в `staging`.
- На staging прогонять локальные регрессии и 1-2 ручных теста через сайт.
- В production переносить только проверенные изменения.
- Реальные клиентские документы хранить только локально в `local_cases`; не коммитить их в GitHub.
- Админку открывать через `/?admin=1`.
- Не использовать один и тот же `ADMIN_SECRET` на production и staging.

## Постоянные данные Railway

- Для каждого окружения подключить отдельный Railway Volume к пути `/app/data`.
- В Volume переносить `allowed_keys.json`, `guest_usage.json` и `history_*.json` напрямую, без Git.
- Не хранить в репозитории API-ключи, `ADMIN_SECRET`, клиентские таблицы, историю или гостевые счётчики.
- Переменные добавлять на вкладке Railway `Variables`; значения секретов при возможности помечать как sealed.
- Production подключать к ветке `main`, staging — к ветке `staging`.
