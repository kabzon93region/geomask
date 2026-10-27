# geomask

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Release](https://img.shields.io/badge/release-v1.10.0-blue)](https://github.com/kabzon93region/geomask/releases/tag/v1.10.0)
[![Language: Shell](https://img.shields.io/badge/language-POSIX%20sh-informational)](geomask.sh)
[![Platform: OpenWrt](https://img.shields.io/badge/platform-OpenWrt%2024.10%2B-00b5e2)](https://openwrt.org)
[![LuCI](https://img.shields.io/badge/UI-LuCI-ff6d00)](https://github.com/openwrt/luci)
[![Build](https://img.shields.io/badge/build-python%20build.py-success)](build.py)
[![CI](https://github.com/kabzon93region/geomask/actions/workflows/ci.yml/badge.svg)](https://github.com/kabzon93region/geomask/actions/workflows/ci.yml)

Автоматизированный установщик и конфигуратор домашнего стека обхода блокировок на OpenWrt.
Целевое устройство — **wwGate AX3000** (MediaTek Filogic, aarch64), прошивка OpenWrt 25.12.

> **Одна команда** поднимает гео-панель в LuCI, mihomo с двумя подписками, DNS-маршрутизацию
> magitrickle → mihomo, отдельный AI-туннель для выбранных устройств и сквозные E2E-проверки.

---

## Что делает

| Компонент | Назначение |
|---|---|
| **Гео-оверлей LuCI** | Панель «Гео-маскировка и AI»: статусы, подписки, устройства (`rpcd`-объект `geo-sync` + web-view) |
| **mihomo, 2 подписки** | Группы `VPN` (основная) и `AI` (для Google/сервисов, чувствительных к гео) |
| **magitrickle → mihomo sync** | DNS-маршрутизация по доменам, cron `*/5`, `rule-providers` в mihomo |
| **AI-устройства** | Режим «весь трафик через AI-подписку»: отдельный hev-туннель + слушатель mihomo `7891` |
| **AI-DNS (без утечки)** | DNS AI-устройств уходит в их же AI-туннель (метка → `ip rule` → `dev AI`) |
| **Сервисы и verify** | dnsmasq / magitrickle / mihomo / hev-socks5-tunnel + E2E-проверка 7 шагов |

## Установка

Рекомендуется **jsDelivr** (быстрее GitHub raw, виден прогресс скачивания):

```sh
curl -fL -m 60 -o /tmp/geomask.sh 'https://cdn.jsdelivr.net/gh/kabzon93region/geomask@master/geomask.sh' && sh /tmp/geomask.sh
```

Если `curl` отсутствует:

```sh
wget -T 30 -O /tmp/geomask.sh 'https://cdn.jsdelivr.net/gh/kabzon93region/geomask@master/geomask.sh' && sh /tmp/geomask.sh
```

Классический one-liner через GitHub raw (может быть **очень медленным** с некоторых сетей —
выглядит как зависание пустого экрана, используйте варианты выше):

```sh
sh <(wget -qO - 'https://raw.githubusercontent.com/kabzon93region/geomask/master/geomask.sh')
```

Из релиза (пин на конкретную версию):

```sh
opkg install https://github.com/kabzon93region/geomask/releases/download/v1.10.0/geomask.sh
```

## Быстрый старт

```sh
sh /tmp/geomask.sh            # интерактивное меню + сводка статусов
sh /tmp/geomask.sh --check    # read-only отчёт по всем шагам
sh /tmp/geomask.sh --auto     # всё без вопросов (стоп при verify-FAIL)
```

Полный список режимов — в [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Документация

| Документ | О чём |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Архитектура, компоненты, что и куда ставится на роутер |
| [docs/ROUTING.md](docs/ROUTING.md) | Модель маршрутизации: таблицы, `ip rule`, приоритеты, magitrickle |
| [docs/SUBSCRIPTIONS.md](docs/SUBSCRIPTIONS.md) | Подписки `sub`/`de5`, переключение, `x-hwid` |
| [docs/AI-DEVICES.md](docs/AI-DEVICES.md) | AI-туннель, hev-инстанс, слушатель 7891, STUN/WebRTC |
| [docs/AIDNS.md](docs/AIDNS.md) | Цепочка AI-DNS без утечки |
| [docs/SYNC.md](docs/SYNC.md) | sync magitrickle → mihomo |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Режимы запуска, бэкапы, безопасность |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Диагностика типовых проблем |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Сборка, песочница, тесты |
| [docs/releases/](docs/releases/) | Release notes по версиям |

## Структура репозитория

```
geomask.sh        # готовый установщик (публикуется, его и качает wget)
geomask.sh.in     # шаблон — править его
build.py          # сборка: python build.py -> geomask.sh
assets/           # файлы, вливаемые в установщик (@@ASSET:<name>@@)
  geo-backend.sh           бэкенд гео-панели (роутер)
  geo-sync                 rpcd-плагин (ubus geo-sync)
  acl-geo-sync.json        ACL для LuCI
  menu-geo-sync.json       пункт меню LuCI
  geo-sync-page.js         view/geo-sync/main.js (код страницы админки)
  sync_mtrickle_mihomo.sh  magitrickle -> mihomo ruleset sync
  ytdirect.yaml            rule-provider DIRECT-доменов
  99-default-vpn-route     hotplug: LAN -> TUN роутинг
docs/             # техническая документация
sandbox/          # локальная песочница (в git не входит)
```

## Требования

- OpenWrt 24.10+ (проверено на 25.12, aarch64/cortex-a53)
- `python3`, `curl`, `wget`, `ubus`/`rpcd`, LuCI
- mihomo (бинарь уже на целевом роутере; на чистой системе — отдельно)
- Шаг 3 требует рабочую подписку; шаг 4 — cron-sync magitrickle

## Безопасность

- Никаких `reboot` и касаний `network`/`pppoe`-конфига.
- Работающие сервисы не перезапускаются (только старт упавших).
- Бэкап перед каждой записью файла (`.bak_geomask_<timestamp>`).
- Полный бэкап настроек: `sh geomask.sh --backup` (= `sysupgrade -b`).
- `mihomo -t` перед установкой конфига; при провале — старый конфиг остаётся.
- Подписки применяются через API без перезапуска mihomo; полный рестарт — только по `--restart`.
- Жёсткая pin-блокировка рестартов: файл `/root/.geomask-norestart`.
- Секреты (URL подписок, hwid) не входят в скрипт — передаются аргументами и остаются на роутере.

## Лицензия

[MIT](LICENSE). Гео-оверлей и синк-скрипты — собственные доработки.
