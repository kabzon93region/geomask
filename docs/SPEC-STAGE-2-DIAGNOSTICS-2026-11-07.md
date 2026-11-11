# Этап 2: Честные проверки и диагностика — спецификация

> **DRAFT / НЕ ДОПУЩЕН К ВНЕДРЕНИЮ — 08.11.2026, MSK.** Приоритет имеет [утверждённый основной план](PLAN-2026-11-06-geomask-v2-rework.md). Локальные status/cache-классы и тестовые файлы не доказывают интеграцию диагностики, соблюдение AI/US или завершение этапа. Этап 1 не принят. Текст ниже — эскиз с обязательными исправлениями, не разрешение live apply.

## Обязательные исправления перед реализацией/приёмкой

- Проверка узла не выполняется через выбранный production AI-listener как доказательство конкретного кандидата. Нужен заранее подготовленный изолированный probe-тракт без смены production-группы и без административной записи из read-only RPC (основной план §5.2).
- Имя mihomo proxy не стабильная идентичность. Ключ доказательства должен учитывать узел/endpoint, источник/подписку, ревизию политики и контрольного набора; смена любого из них инвалидирует допуск.
- Google HTTP 204 и OpenRouter HTTP 200 — доступность, не доказательство US. Для каждого обязательного сервиса определить достоверный метод подтверждения географии или вернуть UNKNOWN; AND всех обязательных доказательств, не усреднение.
- TTL=300 и расписание — предложения, не согласованные параметры. Нужны явные сроки, инвалидация и согласование нагрузки до живого внедрения.
- Разделить read-only диагностику, изменяющее применение политики и заранее установленный fail-closed enforcement. «ai-guard read-only», одновременно записывающий exclude-filter через safe_write, не является одним непротиворечивым контрактом. Истечение допуска при недоступном ПК не допускает новой административной записи/рестарта вместо заранее работающего запрета (основной план §5.1).
- CI обязан выявлять исходный build drift: не пересобирать artifact перед сравнением с исходным; baseline выполнять в изолированной копии. Не выдавать предложенный workflow ниже за существующий CI.

Сегодняшние проверки/пробы в рамках этой документационной актуализации не запускались. Этап не принят.

**Основание:** [основной план](PLAN-2026-11-06-geomask-v2-rework.md) §6 этап 2, [SPEC-STAGE-1](SPEC-STAGE-1-SAFE-WRITES-2026-11-07.md).  
**Цель:** исправить сведение статусов, привести проверки к политике, реализовать read-only диагностику с объяснением сайта для клиента, создать сервис доказательств допуска, обновить CI.  
**Область:** локальные исправления assets/*, backend/panel, tools/geo-rpc.json, tests/*. Никаких live-изменений.

## 1. Исправление сведения статусов (morning-check/dnscheck/vpnwatch/installer)

### Проблема
- `geomask-morning-check.sh`: UNKNOWN только WARN, не влияет на FAIL → ложный "safe"
- `geomask-dnscheck.sh`: пустой ответ / 401 / timeout = 0 нарушений (нечестно)
- `geomask-vpnwatch.sh`: ошибка парсера не отличается от "всё чисто"
- installer: ошибки API/сервисов подавлены

### Решение: единый enum статуса проверки
```python
# tools/diag_status.py
from enum import Enum

class CheckStatus(Enum):
    OK = "ok"                    # проверка прошла, нарушений нет
    FAIL = "fail"                # нарушение обнаружено
    UNKNOWN = "unknown"          # не удалось определить (timeout, 401, parse error, empty)
    SKIPPED = "skipped"          # проверка не применима (нет конфига, отключено)
```

Все скрипты возвращают JSON с этим статусом. Сводный отчёт считает:
- `UNKNOWN` → не "всё чисто", а "требует внимания"
- `FAIL` → нарушение
- `OK` → чисто

### Файлы к изменению
- `assets/geomask-morning-check.sh` → JSON output с CheckStatus
- `assets/geomask-dnscheck.sh` → JSON output, 401/timeout/empty = UNKNOWN
- `assets/geomask-vpnwatch.sh` → JSON output, parser error = UNKNOWN
- `geomask.sh.in` (installer) → собирает статусы, не подавляет ошибки

## 2. Приведение проверок к действующей политике

### Проблема
- morning-check ожидает AI через torrent listener (закреплён за VPN/DIRECT)
- AI admission должен проверяться через AI listener (socks5h://127.0.0.1:7891)

### Решение
- Удалить ожидание AI через torrent listener из morning-check
- Добавить проверку AI admission через AI listener (совпадает с node-scan --ai-guard)
- dnscheck: проверять AI-DoH через AI listener, а не через main

### Файлы
- `assets/geomask-morning-check.sh` L67-79: заменить probe target
- `assets/geomask-dnscheck.sh`: добавить AI-DoH probe

## 3. Read-only диагностика: объяснение сайта для клиента

### Новый RPC метод: `diag_explain`
```
Input: { "client_ip": "192.168.0.102", "destination": "chatgpt.com" }
Output: {
  "client_ip": "192.168.0.102",
  "device_mode": "ai",
  "path": "AI",
  "selected_node": "USA 3",
  "node_admitted": true,
  "admission_proof": { "service": "chatgpt", "loc": "US", "ts": "..." },
  "dns_path": "AI-DoH (socks5h://127.0.0.1:7891)",
  "ip_rules": ["17941 from 192.168.0.102 lookup 101"],
  "nft_marks": ["0x4d61677f -> table 1298229119"],
  "zapret_excluded": false,
  "predicted_route": "AI tunnel via pppoe-wan",
  "evidence_limit": "single measurement at 2026-11-07T19:30:00Z, not proof of all future packets"
}
```

### Реализация
- `tools/geo-rpc.json`: добавить метод `diag_explain`
- `assets/geo-backend.sh`: `cmd_diag_explain` — собирает ip rule, nft, mihomo group, admission cache
- `assets/geo-sync-page.js`: UI кнопка "Объяснить сайт для этого устройства"

### Важно
- **Read-only**: не меняет выбор production-группы, не вызывает mutating ai-guard
- **Evidence limit**: явное поле `evidence_limit` в ответе

## 4. Сервис доказательств допуска (Admission Service)

### Спецификация
```python
# tools/admission_service.py
class AdmissionProof:
    service: str           # "google", "chatgpt", "openrouter"
    endpoint: str          # URL проверки
    method: str            # "http_proxy" | "dns_proxy" | "direct"
    proxy: str             # socks5h://127.0.0.1:7891
    result: CheckStatus    # OK | FAIL | UNKNOWN
    evidence: dict         # {"loc": "US", "colo": "EWR", "http": 200, "latency_ms": 520}
    ts: str                # RFC3339
    ttl_seconds: int       # срок действия (напр. 300)
    probe_version: int     # версия пробы для инвалидации кэша
```

### Кэш допуска
- Хранится в `/tmp/geomask-admission-cache.json`
- Ключ: `node_id` (mihomo proxy name)
- Значение: список `AdmissionProof` по всем обязательным сервисам
- TTL: 300 сек (настраиваемо)
- Инвалидация: при смене узла, релоаде mihomo, ручном сбросе

### Node-scan интеграция
- `--ai-guard` не мутирует production: только читает кэш/проверяет
- Результат записывается в кэш, исключение через `safe_write` (exclude-filter)
- `cmd_nodes_get` возвращает `admission_cache` для UI

## 5. CI: все тесты и проверки

### GitHub Actions workflow
```yaml
# .github/workflows/ci.yml
jobs:
  test:
    runs-on: windows-latest  # или self-hosted с Git-bash
    steps:
      - uses: actions/checkout@v4
      - name: Setup Python
        uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - name: Setup Node
        uses: actions/setup-node@v4
        with: { node-version: '20' }
      - name: Install deps
        run: pip install pyyaml
      - name: RPC parity
        run: python tools/gen-geo-rpc.py --check
      - name: Build
        run: python build.py
      - name: Build parity
        run: python tools/check_build.py
      - name: Unit tests
        run: python -m unittest discover -s tests -p 'test_*.py' -v
      - name: Shell syntax
        run: bash -n geomask.sh
      - name: JS syntax
        run: node --check assets/geo-sync-page.js
      - name: Sandbox
        run: bash tests/run_tests.sh
```

### Требования к тестам
- Проверяют **поведение**, а не наличие строки
- Каждый FAIL должен быть воспроизводим локально
- Нет "remove failing test to get green"

## 6. Файлы для создания/изменения

### Новые файлы:
- `tools/diag_status.py` — enum CheckStatus
- `tools/admission_service.py` — AdmissionProof, кэш, TTL
- `tools/geo-rpc.json` — добавить `diag_explain`, `admission_cache_get`
- `tests/test_diagnostics.py` — тесты статусов, diag_explain, admission cache
- `tests/test_admission_service.py` — тесты кэша, TTL, инвалидации

### Изменяемые файлы:
- `assets/geomask-morning-check.sh` — JSON output, AI probe через AI listener
- `assets/geomask-dnscheck.sh` — JSON output, AI-DoH probe
- `assets/geomask-vpnwatch.sh` — JSON output, parser error = UNKNOWN
- `assets/geo-backend.sh` — `cmd_diag_explain`, `cmd_admission_cache_get`, интеграция admission cache
- `geomask.sh.in` (installer) — собирает статусы, не подавляет ошибки
- `assets/geo-sync-page.js` — UI "Объяснить сайт", admission cache display
- `tools/geo-rpc.json` — новые методы, генерация через gen-geo-rpc.py
- `.github/workflows/ci.yml` — полный CI pipeline

## 7. Критерии приёмки этапа 2 (local)

- [ ] Все скрипты диагностики возвращают JSON с CheckStatus
- [ ] `UNKNOWN` не считается успехом в сводных отчётах
- [ ] AI probe использует AI listener (не torrent)
- [ ] `diag_explain` работает read-only, не меняет production group
- [ ] Admission cache: запись/чтение/TTL/инвалидация проходят тесты
- [ ] Unit tests: 100% PASS (включая новые)
- [ ] RPC parity: OK
- [ ] Build parity: OK
- [ ] Sandbox: 309/0
- [ ] CI workflow проходит локально (act или ручной прогон)

## 8. Границы этапа 2

**Входит:** исправление статусов, policy-aligned probes, diag_explain, admission service, CI.  
**НЕ входит:** live deploy, миграция политик (этап 3), UI переработка (этап 4), модульность (этап 5).