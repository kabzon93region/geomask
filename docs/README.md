# Документация geomask

Техническая документация установщика и стека обхода блокировок.

| Документ | Содержание |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Общая архитектура, компоненты, карта установленных файлов |
| [ROUTING.md](ROUTING.md) | Модель маршрутизации: таблицы, `ip rule`, приоритеты, magitrickle |
| [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md) | Подписки `sub`/`de5`, переключение, `x-hwid` |
| [AI-DEVICES.md](AI-DEVICES.md) | AI-туннель, hev-инстанс, слушатель `7891`, STUN/WebRTC |
| [AIDNS.md](AIDNS.md) | AI-DNS без утечки: DNAT → второй dnsmasq/DoH → метка → `dev AI` |
| [SYNC.md](SYNC.md) | Синхронизация magitrickle → mihomo (`rule-providers`) |
| [LISTS.md](LISTS.md) | Шаблоны списков MagiTrickle: снимок групп, сид/восстановление (v1.13.0) |
| [YOUTUBE.md](YOUTUBE.md) | YouTube: страница/картинки через запрет, сбойные рёбра googlevideo — в VPN (v1.14.0) |
| [TORRENTS.md](TORRENTS.md) | Торренты/P2P: слушатель `torrent-direct`, порты и списки трекеров — всегда мимо VPN (v1.14.4) |
| [OPERATIONS.md](OPERATIONS.md) | Режимы запуска, шаги, бэкапы, безопасность |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Диагностика типовых проблем |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Сборка, песочница, тесты |
| [releases/](releases/) | Release notes по версиям |

## Терминология

- **VPN-подписка** — основная подписка (провайдер `sub`), группа `VPN`.
- **AI-подписка** — подписка для AI/гео-чувствительных сервисов (провайдер `de5`), группа `AI`.
- **AI-устройство** — устройство, чей весь трафик идёт через AI-подписку (`mode=ai`).
- **bypass-устройство** — устройство, чей трафик идёт напрямую (`mode=bypass`).
- **mihomo** — прокси-ядро (Clash.Meta); TUN-интерфейсы `Mihomo` (основной) и `AI`.
- **hev-socks5-tunnel** — TUN→SOCKS5 мост (инстансы `main` и `ai`).
- **magitrickle** — DNS-based routing (группы доменов → интерфейс).
