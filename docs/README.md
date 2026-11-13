# Документация geomask

Справки по установщику, стеку маршрутизации и панели. Всё, что описывает **текущее
поведение**, сверено с релизом **v1.30.7**; исторические документы помечены отдельно.

## Актуальное

| Документ | Содержание |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Компоненты и карта установленных файлов |
| [ROUTING.md](ROUTING.md) | Маршрутизация: таблицы, `ip rule`, приоритеты, MagiTrickle |
| [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md) | Подписки VPN/AI, обновление, переключение |
| [AI-DEVICES.md](AI-DEVICES.md) | AI-туннель, hev-инстанс, слушатель `7891`, STUN/WebRTC |
| [AIDNS.md](AIDNS.md) | AI-DNS без утечки: DNAT → второй dnsmasq/DoH → метка → `dev AI` |
| [SYNC.md](SYNC.md) | Синхронизация MagiTrickle → mihomo (`rule-providers`) |
| [LISTS.md](LISTS.md) | Списки MagiTrickle: шаблон, сид, точечная правка домена (`geomask-lists-edit.py`) |
| [YOUTUBE.md](YOUTUBE.md) | YouTube: страница и картинки напрямую, сбойные рёбра — в VPN |
| [TORRENTS.md](TORRENTS.md) | Торрент-ingress и прямой путь |
| [NODE-AUDIT.md](NODE-AUDIT.md) | Аудит узлов подписки и проверки стран |
| [MODULES.md](MODULES.md) | Модули: выбор при установке, версии, гейты cron/сервисов, отдельное обновление |
| [DIAGNOSTICS.md](DIAGNOSTICS.md) | Диагностика: проверки, маршрут домена, связанные хосты, логи 7 дней |
| [RELEASING.md](RELEASING.md) | Релизы: `tools/release.py`, контракт тега и ассета, инвариант байтов |
| [OPERATIONS.md](OPERATIONS.md) | Режимы запуска, шаги, бэкапы, безопасность |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Типовые проблемы и что смотреть |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Сборка, песочница, тесты |
| [releases/](releases/) | Release notes по версиям |

## Историческое (не описывает текущее поведение)

Планы, SPEC, аудиты и отчёты разных этапов. Читать как историю решений: часть требований
из них реализована, часть отменена, часть так и не принята.

| Документ | Что это |
|---|---|
| [PLAN-2026-11-06-geomask-v2-rework.md](PLAN-2026-11-06-geomask-v2-rework.md) | План переработки v2 (не принят целиком) |
| [STAGE-0-2026-11-07.md](STAGE-0-2026-11-07.md) | Контрольная точка этапа 0 |
| [SPEC-STAGE-1-SAFE-WRITES-2026-11-07.md](SPEC-STAGE-1-SAFE-WRITES-2026-11-07.md) | SPEC безопасных записей (частично реализовано) |
| [SPEC-STAGE-1A-CARRYOVER-2026-11-07.md](SPEC-STAGE-1A-CARRYOVER-2026-11-07.md) | Узкая подзадача переноса |
| [SPEC-STAGE-2-DIAGNOSTICS-2026-11-07.md](SPEC-STAGE-2-DIAGNOSTICS-2026-11-07.md) | SPEC диагностики (реализовано иначе, см. DIAGNOSTICS.md) |
| [SPEC-STAGE-3-POLICIES-2026-11-07.md](SPEC-STAGE-3-POLICIES-2026-11-07.md) | SPEC политик (не внедрён) |
| [SPEC-MIGRATION-NODE-AUDIT-v2-2026-11-08.md](SPEC-MIGRATION-NODE-AUDIT-v2-2026-11-08.md) | Миграция аудита узлов |
| [SPEC-PATH-EGRESS-AUDIT-2026-11-08.md](SPEC-PATH-EGRESS-AUDIT-2026-11-08.md) | Аудит выхода путей |
| [PLAN-NODE-AUDIT-COMPLETE-2026-11-08.md](PLAN-NODE-AUDIT-COMPLETE-2026-11-08.md) | План завершения node-audit |
| [STATUS-NODE-AUDIT-2026-11-08.md](STATUS-NODE-AUDIT-2026-11-08.md) | Статус node-audit |
| [SUMMARY-NODE-AUDIT-2026-11-09.md](SUMMARY-NODE-AUDIT-2026-11-09.md) | Итоги node-audit |
| [COMPLIANCE-NODE-AUDIT-2026-11-09.md](COMPLIANCE-NODE-AUDIT-2026-11-09.md) | Соответствие требованиям аудита |
| [GATES-EVIDENCE-2026-11-09.md](GATES-EVIDENCE-2026-11-09.md) | Доказательства прохождения гейтов |
| [AUDIT-2026-11-01.md](AUDIT-2026-11-01.md) | Независимый аудит v1.22.6 |
| [RESEARCH-GOOGLE-GEO-2026-11-08.md](RESEARCH-GOOGLE-GEO-2026-11-08.md) | Исследование географии Google |
| [SUPPORT-un1pro-openai-helsinki-2026-11-02.md](SUPPORT-un1pro-openai-helsinki-2026-11-02.md) | Разбор случая с узлом |

## Терминология

- **Подписка** — источник узлов и его ограничения (включая допустимость P2P).
- **Путь** — независимая политика/группа/транспорт (`VPN`, `VPN-noP2P`, `AI`).
- **Модуль** — часть релиза со своими файлами, версией и отдельным обновлением.
- **AI-устройство** — устройство, чей трафик идёт в AI-туннель (слушатель `7891`).
- **mihomo** — прокси-ядро (Clash.Meta): группы, правила, слушатели.
- **hev-socks5-tunnel** — создаёт TUN-интерфейсы и связывает их с SOCKS-слушателями.
- **MagiTrickle** — DNS-based routing: доменные группы → интерфейс, метки в nftables.
