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

Деплой: из ветки `main`.

## Railway: staging

Создать отдельный Railway service или environment и привязать к ветке `staging`.

Переменные окружения:

- `SVERKAI_ENV=staging`
- `ANTHROPIC_API_KEY=<тестовый или тот же гостевой ключ>`
- `ADMIN_SECRET=<отдельный секрет для staging>`
- `SVERKAI_ALLOWED_KEY_HASHES=<тестовые пользователи>`
- `SVERKAI_GUEST_KEY_HASHES=<хеш гостевого ключа>`

Деплой: из ветки `staging`.

## Правила работы

- Новые правки сначала отправлять в `staging`.
- На staging прогонять локальные регрессии и 1-2 ручных теста через сайт.
- В production переносить только проверенные изменения.
- Реальные клиентские документы хранить только локально в `local_cases`; не коммитить их в GitHub.
- Админку открывать через `/?admin=1`.
- Не использовать один и тот же `ADMIN_SECRET` на production и staging.
