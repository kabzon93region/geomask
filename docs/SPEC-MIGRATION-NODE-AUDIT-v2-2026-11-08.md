# Миграция node-audit: schema v1 → v2

**Дата:** 08.11.2026, MSK. **Статус:** контракт для реализации B1; в этой задаче live-состояние не меняется.

## 1. Исходное состояние (снапшот 20261108-200725)

Установлен на роутере `/usr/lib/geomask/geomask-node-audit.py` (SHA256 `03e53211b93807cdbdb584f2f253e4624714dd72cdfb482e03bb1c462f89b3e5`), файл настроек `/etc/geomask/node-audit.json`:

```json
{
  "generation": 6,
  "paths": {
    "ai": { "countries_allow": ["US"] },
    "vpn": { "countries_allow": ["DE","FR","NL","PL"] },
    "vpn_nop2p": { "countries_allow": ["DE"] }
  },
  "schema": 1
}
```

Текущие отчёты в `/tmp/geomask-node-audit/{vpn,vpn_nop2p,ai}.json` — последнее состояние проверок (см. снапшот).

## 2. Цели schema v2

- **Независимые ревизии путей**: изменение VPN не инвалидирует отчёт VPN-noP2P.
- **Явная привязка источника**: `source_binding` с идентификатором подписки/провайдера и отпечатком конфигурации.
- **Node identity**: отпечаток полного определения узла (transport/server/port/params), а не только имя.
- **Applied manifest**: отдельное подтверждённое поколение для runtime.
- **История событий**: append-only JSONL с ограниченным retention.
- **Job identity**: `job_id` для каждого запуска проверки/применения.
- **Обратная совместимость**: старый клиент получает STALE/RELOAD_REQUIRED, а не ломается молча.

## 3. Структура v2 (JSON)

```json
{
  "schema": 2,
  "generation": 7,
  "paths": {
    "vpn": {
      "countries_allow": ["DE","FR","NL","PL"],
      "path_generation": 3,
      "path_revision": "sha256-of-semantic-content",
      "source_binding": {
        "provider_ids": ["sub2"],
        "source_fingerprint": "sha256-of-endpoint+headers+params",
        "subscription_url_digest": "sha256-of-private-url",
        "bound_at": "2026-11-08T19:33:49+03:00"
      },
      "schedule": { "enabled": false, "time": "03:30", "mode": "check-only" },
      "applied": {
        "generation": 0,
        "path_revision": "",
        "preview_id": "",
        "job_id": "",
        "applied_at": "",
        "runtime_verified": false,
        "pool_digest": "",
        "status": "never"
      }
    },
    "vpn_nop2p": { ... },
    "ai": { ... }
  },
  "events": [
    { "event_id": "...", "timestamp": "...", "path": "vpn", "actor": "luci", "action": "save", "old_revision": "...", "new_revision": "...", "result": "success" },
    ...
  ],
  "event_index": { "next_id": 42, "truncated_before": "..." }
}
```

Поля `events` и `event_index` управляются модулем истории; в основной файл пишутся только терминальные сводки (последние N событий на путь), полный журнал — в отдельный rotating JSONL.

## 4. Правила миграции v1 → v2

Миграция запускается один раз при первой загрузке v2-модуля (функция `migrate_v1_to_v2()` в новом state-модуле). Она:

1. Читает существующий v1 файл.
2. Для каждого пути с `countries_allow` создаёт v2 структуру:
   - `path_generation` = 1 (первое поколение v2).
   - `path_revision` = SHA256 от канонического JSON только этого пути (countries_allow + source_binding).
   - `source_binding`: определяется через `resolve_source_binding(config, ident)` — текущие `provider_ids` из `group.use`, `source_fingerprint` = digest(concat(endpoint, sorted headers, transport params)), `subscription_url_digest` = sha256(private URL), `bound_at` = now.
   - `schedule` = дефолт (enabled=false, time=03:30, mode=check-only).
   - `applied` = zero generation / never.
3. `generation` файла = max(path_generation) + 1.
4. `events` = пустой массив (история начинается с миграции); `event_index` инициализируется.
5. **Никаких значений по умолчанию не подменяет** текущие выбранные страны пользователя.
5. Неизвестные/дополнительные поля v1 (если появятся) сохраняются в `_legacy` для аудита, но не используются.
6. Атомарная запись: временный файл + fsync + replace под flock.
7. После миграции старый v1 формат больше не читается; downgrade не предусмотрен без ручного восстановления из бэкапа.

## 5. Path revision — что входит, что нет

`path_revision` = `digest(canonical_json({ countries_allow, source_binding }))`.

**НЕ входит:** `schedule`, `applied`, `path_generation`, отчёты проверок, whole config hash, whole store hash.

**Результат:** `Save(vpn, ...)` меняет только `vpn.path_revision` и `vpn.path_generation`; `vpn_nop2p.path_revision` не меняется, его отчёт не становится stale без реальной зависимости.

## 6. Source binding — как считать fingerprint

Функция `resolve_source_binding(config, ident)`:

1. Находит целевую группу по `PATHS[ident][0]` (VPN/VPN-noP2P/AI).
2. Берёт `provider_ids = group.use` (упорядоченный список уникальных id).
3. Для каждого provider id из `proxy-providers` собирает:
   - `url` (приватный, не в RPC/UI)
   - `header` (если есть)
   - `transport` параметры (type, server, port, protocol-specific fields)
4. `source_fingerprint` = sha256(json_dumps(sorted provider specs)).
5. `subscription_url_digest` = sha256(primary provider url) — для сверки при refresh без утечки URL.

Если группа/провайдер не найдены — `source_binding = null`, путь считается unconfigured.

## 7. Node identity — формат

Для каждого узла из скачанной подписки:

```python
node_identity = {
    "name": node["name"],
    "transport": {k:v for k,v in node.items() if k in ("type","server","port","protocol","cipher","tls","udp","skip-cert-verify","sni","alpn","fingerprint","network","ws-opts","h2-opts","grpc-opts")},
    "provider_id": provider_id
}
node_fingerprint = sha256(json_dumps(node_identity, sort_keys=True))
```

Имена **не уникальны** глобально; ключ в managed pool = `provider_id + "/" + node_fingerprint[:16]`.

## 8. Обратная совместимость RPC

- `nodes_path_save(path_id, countries, expected_revision)` — `expected_revision` теперь интерпретируется как **path_revision** выбранного пути. Если передан старый whole-store SHA → возвращается ошибка `RELOAD_REQUIRED: expected_revision format changed; reload page`.
- `nodes_paths_get` возвращает `api_schema: 2` и для каждого пути: `path_revision`, `source_revision` (fingerprint), `applied.generation`, `applied.runtime_verified`.
- `nodes_path_check(path_id)` фиксирует в job'е используемые `path_revision` и `source_revision`; при публикации отчёта они сверяются — несовпадение = отчёт помечается `stale: true` с причиной.

Старый UI, не знающий о path_revision, при следующем Save получит STALE и потребует reload страницы — это корректное поведение.

## 9. Файлы состояния

| Путь | Назначение | Формат |
|---|---|---|
| `/etc/geomask/node-audit.json` | Canonical store (v2) | JSON, atomic write, 0600 |
| `/etc/geomask/node-audit/events.jsonl` | Полный журнал событий | JSONL, append-only, rotating |
| `/tmp/geomask-node-audit/{path}.json` | Последний отчёт проверки | JSON, перезаписывается |
| `/etc/geomask/node-audit/pools/{path}-gen{N}.json` | Immutable проверенный пул поколения | JSON, 0600, не в events.jsonl |

## 10. Тесты миграции (B1 acceptance)

| Сценарий | Ожидание |
|---|---|
| Чистый v1 файл с тремя путями | Мигрирует в v2, сохраняет все countries, path_generation=1, source_binding заполнены из текущего mihomo config |
| v1 файл без ai пути | ai получает дефолт source_binding=null, countries_allow из PATHS defaults, path_generation=1 |
| Пустой/отсутствующий файл | Создаёт v2 с дефолтами PATHS, generation=1 |
| Повторный запуск миграции на v2 | Идемпотентно: ничего не меняет, возвращает текущее состояние |
| Save одного пути после миграции | Меняет только этот путь, соседний отчёт не stale |
| Старый whole-store revision в Save | Ошибка RELOAD_REQUIRED с понятным текстом |

## 11. Развёртывание

- Новый state-модуль и мигрированный фасад ставятся **точечно** после свежего PC backup.
- Миграция запускается автоматически при первом `get()` или `save()` после установки v2.
- Старый формат отчётов в `/tmp` не мигрируется — они перезапишутся при следующей проверке.
- Rollback: восстановление v1 файла из бэкапа + перезапуск старого модуля.

---

**Примечание:** Этот документ — контракт для задачи B1. Реализация начинается с создания `assets/geomask-audit-state.py` с функцией `migrate_v1_to_v2()` и обновлёнными `load_store()`, `save()`, `get()`.