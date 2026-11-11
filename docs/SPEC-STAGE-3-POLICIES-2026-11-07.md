# Этап 3: Политики и фильтры по путям — спецификация

> **DRAFT / НЕ ДОПУЩЕН К ВНЕДРЕНИЮ — 08.11.2026, MSK.** Приоритет имеет [утверждённый основной план](PLAN-2026-11-06-geomask-v2-rework.md). Примеры ниже противоречивы и **не являются конфигурацией для установки**. Наличие моделей/preview не означает working enforcement; этапы 1–3 не приняты.

## Обязательные исправления перед реализацией/приёмкой

- Пример `google.com,DIRECT`, `GEOIP,US,REJECT` и неопределённое `GEOIP,!,US` несовместимы с AI/US. Не применять и не копировать их в live-конфиг. Фильтр узлов не подменять правилами маршрутизации назначений.
- P2P-разрешённость — свойство подписки, наследуемое каждым путём. В эскизе subscriptions этого поля нет, VPN помечен p2p_allowed=false, ниже тот же источник объявляется P2P-разрешённым. Нужна согласованная схема и migration фактического состояния, не hardcode sub/de5.
- Пустой допустимый пул блокирует каждый активный обязательный путь, а не только admission_required/AI. Нет DIRECT, другой страны или другой подписки как скрытого резерва.
- HTTP 204 Google и HTTP 200 OpenRouter не доказывают US. Для обязательного сервисного набора действуют AND, достоверная география, конечный срок и UNKNOWN при отсутствии доказательства (основной план §5.2).
- Mark/drop сам по себе не доказывает fail-closed. Требуется заранее установленный механизм отзыва/expiry без новой административной записи при недоступном ПК, прекращение активных соединений, охват source-rule/fixed listeners/AI-DNS/ai/bypass и отрицательные тесты.
- P2P-ingress не универсальный обход: известное AI-назначение через него отвергается; поддержка UDP/DHT/µTP подтверждается runtime, неподдержанное блокируется. До default-deny фиксируются разрешённые приложения, узкий management/control-plane и предпросмотр несовместимостей.
- Мигрируются действительные ограничения всех путей; новая география применяется только отдельным явным действием после preview, не вместе с форматом. Live apply зависит не только от этапа 1, но и доказанных контрактов диагностики/допуска и условий основного плана.

Эти замечания не реализуют enforcement и не исправляют production-код. Тесты и живая приёмка в этой документационной задаче не выполнялись.

**Основание:** [основной план](PLAN-2026-11-06-geomask-v2-rework.md) §6 этап 3, [SPEC-STAGE-2](SPEC-STAGE-2-DIAGNOSTICS-2026-11-07.md).  
**Цель:** versioned-модель путей/подписок, независимые фильтры, строгий запрет при пустом пуле, AI admission AND, P2P только через proxy.  
**Область:** локальная реализация core models, filters, migration, preview. Live apply — после валидации этапа 1.

## 1. Versioned-модель (canonical store)

Единый источник правды: `/etc/geomask/policies.yaml` (или JSON). Никаких дубликатов в shell/UI.

```yaml
# /etc/geomask/policies.yaml
version: 1
schema: "geomask-policy-v1"
paths:
  - id: vpn
    name: "VPN"
    interface: "mihomo"
    p2p_allowed: false
    subscription: "sub"           # reference to subscription id
    countries_allow: ["NL", "DE", "FR", "SE"]
    countries_deny: []            # доп. запреты поверх гео
    asns_allow: []
    asns_deny: ["AS210976"]
    node_filter: "base_filter"    # reference to named filter
    admission_required: false
    enabled: true

  - id: vpn_nop2p
    name: "VPN-noP2P"
    interface: "mihomo"
    p2p_allowed: false
    subscription: "de5"           # restricted subscription
    countries_allow: ["DE", "PL"]
    countries_deny: []
    asns_allow: []
    asns_deny: ["AS210976"]
    node_filter: "base_filter"
    admission_required: false
    enabled: true

  - id: ai
    name: "AI"
    interface: "mihomo"
    p2p_allowed: false
    subscription: "sub"           # same as vpn, isolated via slot
    countries_allow: ["US"]
    countries_deny: []
    asns_allow: []
    asns_deny: []
    node_filter: "ai_filter"      # stricter filter
    admission_required: true      # AND of mandatory services
    admission_services: ["google", "chatgpt", "openrouter"]
    admission_ttl: 300
    enabled: true

subscriptions:
  - id: sub
    name: "Main"
    url: "https://..."
    type: http
  - id: de5
    name: "Restricted"
    url: "https://..."
    type: http

filters:
  - id: base_filter
    rules:
      - "DOMAIN-SUFFIX,google.com,DIRECT"
      - "GEOIP,RU,REJECT"
      - "GEOIP,BY,REJECT"
  - id: ai_filter
    rules:
      - "DOMAIN-SUFFIX,google.com,DIRECT"
      - "GEOIP,US,REJECT"    # AI must be US only
      - "GEOIP,!,US,REJECT"
```

**Правила:**
- `id` стабилен, не зависит от имени/эмодзи
- Путь ссылается на подписку по `id`, не копирует её
- P2P-запрет подписки наследуется всеми путями, использующими её
- `admission_required` только для AI-пути

## 2. Миграция фактических настроек

**Алгоритм (идемпотентный, повторяемый):**
1. Прочитать текущие `nodes.conf`, mihomo config, geo-devices.list, subscriptions
2. Для каждого пути из текущей конфигурации:
   - Определить используемую подписку
   - Извлечь текущие allow/deny countries, ASNs
   - Сохранить в `policies.yaml` с `version: 1`
3. Сохранить оригиналы в `/etc/geomask/policies.yaml.backup.<timestamp>`
4. Не удалять старые настройки до явного подтверждения

**Файл миграции:** `tools/migrate_policies.py`

## 3. Preview (предпросмотр)

CLI/JSON вывод:
```json
{
  "path": "ai",
  "subscription": "sub",
  "total_nodes": 12,
  "admitted": 3,
  "rejected": {
    "wrong_country": 5,
    "admission_fail": 2,
    "wrong_asn": 2
  },
  "missing_countries": [],
  "unknown_results": 0,
  "preview_only": true
}
```

## 4. Строгий запрет при пустом пуле

Если `admitted == 0` для пути с `admission_required: true`:
- Путь → BLOCKED (mark 0xdead, table drop)
- UI: "AI path blocked: no admitted nodes (reasons: ...)"
- Никакого fallback на DIRECT/VPN/другую подписку

## 5. AI admission AND

Все обязательные сервисы должны пройти:
- Google generate_204 → HTTP 204
- ChatGPT cdn-cgi/trace → loc=US
- OpenRouter → HTTP 200

Любой FAIL/UNKNOWN → узел исключён из AI-группы.

## 6. P2P только через proxy

- Существующий P2P-прокси → P2P-разрешённый VPN (sub с p2p_allowed=true)
- Вне прокси: known P2P / UNKNOWN / неразрешённое → BLOCKED
- Остальное разрешённое → выбранный путь устройства

## 7. Файлы для создания

### Новые файлы:
- `tools/policy_model.py` — dataclasses, validation, serialization
- `tools/migrate_policies.py` — миграция текущих настроек
- `tools/policy_preview.py` — preview CLI/JSON
- `tools/path_filters.py` — генерация ruleset из политик
- `tests/test_policy_model.py` — unit tests
- `tests/test_migration.py` — migration tests
- `tests/test_preview.py` — preview tests

### Изменяемые файлы:
- `tools/geo-rpc.json` — добавить `policy_get`, `policy_preview`, `policy_apply`
- `assets/geo-backend.sh` — `cmd_policy_*` handlers
- `assets/geo-sync-page.js` — UI для политик/превью
- `tools/geo-rpc.json` — генерация через gen-geo-rpc.py

## 8. Приёмка этапа 3 (local)

- [ ] `policy_model.py` валидирует схему,rejects invalid
- [ ] `migrate_policies.py` идемпотентно создает policies.yaml из текущих настроек
- [ ] `policy_preview.py` показывает admitted/rejected/missing/unknown
- [ ] Пустой admitted pool → path BLOCKED, не DIRECT/fallback
- [ ] AI admission AND работает: любой FAIL/UNKNOWN исключает узел
- [ ] P2P через proxy → только в P2P-разрешённый VPN
- [ ] Unit tests PASS
- [ ] RPC parity OK
- [ ] Build parity OK
- [ ] Sandbox 309/0

## 9. Границы

**Входит:** модели, миграция, preview, фильтры, строгий запрет, AI AND, P2P proxy.  
**НЕ входит:** live apply, UI переработка (этап 4), модульность (этап 5).