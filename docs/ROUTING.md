# Маршрутизация

## TUN-интерфейсы и таблицы

| Интерфейс | Адрес | Таблица | hev-инстанс | mihomo listener |
|---|---|---|---|---|
| `Mihomo` | 198.18.0.1 | table **100** `default dev Mihomo` | `main` (`main.yml`, socks `127.0.0.1:7890`) | mixed `7890` (правила) |
| `AI` | 198.19.0.1 | table **101** `default dev AI` | `ai` (`ai.yml`, socks `127.0.0.1:7891`) | mixed `7891` `proxy: AI` |

Таблица 101 копирует маршруты из `main` (кроме `default`), поэтому LAN и служебные сети
(`192.168.0.0/24`, `10.10.0.0/24` WireGuard, `pppoe`) остаются локальными, а весь остальной
трафик идёт в `dev AI`.

## Правила `ip rule` (RPDB)

Меньший номер = **выше** приоритет (проверяется раньше).

| Приоритет | Правило | Назначение |
|---|---|---|
| `19900` | `from <ip> lookup main` | bypass-устройство → напрямую |
| `19900` | `from <ip> lookup 101` | AI-устройство → AI-туннель |
| `19901` | `iif br-lan ipproto icmp lookup main` | ICMP-bypass: LAN-ping не умирает в TUN |
| `19902` | `fwmark 0x64 lookup 101` | AI-DNS: DoH-воркер `dnsai` → AI-туннель |
| `19979…19999` | `fwmark 0x4d6167xx lookup 12982290xx` | правила групп magitrickle (**динамический** диапазон, см. ниже) |
| `32760` | `iif br-lan lookup 100` | обычный LAN → mihomo |
| `32766/32767` | `main` / `default` | штатные |

### ⚠️ Динамическая база magitrickle и geomask-guard

Приоритеты fwmark-правил magitrickle **не фиксированы**: при каждом их пересоздании (рестарт
магитрикле, изменение групп) ядро размещает новый блок **прямо ниже текущего минимума** в системе.
Наблюдения по проекту: `32673…` → `32645…` → `32589…` → `32558…`, а после того как наши правила
встали на `20000` (v1.5.8), блок сместился на **`19999…19979` — ниже наших**, и B-001 повторился
(18.10.2026: `2ip.io`/`myip.com` показывали РФ-IP на AI-устройстве).

**Вывод: любые жёстко заданные числа проигрывают.** Стабильность даёт **geomask-guard (v1.6.0)**:

- **nft-исключение**: `ip saddr <device> return` с комментарием `geomask-excl` в **голове**
  `ip mangle PREROUTING` (перед прыжками `MT_*`) — пакеты ai/bypass-устройств вообще не получают
  метку magitrickle, и положение fwmark-правил для них перестаёт иметь значение;
- **watchdog** (cron `*/5`): правила `19900/19901/19902` на месте и выше базы magitrickle —
  при нарушении пересаживает их на `(база − 100)`, сбрасывает conntrack устройств и пишет событие
  в syslog (`logread | grep geomask-guard`);
- дополнительно: проверка `table 101 default dev AI` и сброс conntrack при любых изменениях.

Файлы: `/usr/bin/geomask-guard.sh`, hotplug `/etc/hotplug.d/iface/99-geomask-guard` (запуск при
`ifup`), cron `*/5 * * * * /usr/bin/geomask-guard.sh`.

**Правило сопровождения**: стартовый приоритет новых per-device правил — `19900/19901/19902`;
фактический порядок контролирует geomask-guard, проверять: `ip rule show`.

## Режимы устройств (`/etc/geo-devices.list`)

Формат строки: `<IP> <mode> <name>`.

- `bypass` — весь трафик напрямую (реальный IP), правило `from <IP> lookup main` (19900).
- `ai` — весь трафик через AI-туннель, правило `from <IP> lookup 101` (19900).
- `realtz` — обычный путь mihomo (маскированный IP), реальный только часовой пояс (на стороне клиента).

Правила создаёт/пересоздаёт `geo-backend.sh` (`_apply_routing`) и hotplug `99-default-vpn-route`
(при `ifup`). Оба используют приоритет `19900`; удаление — по `from <ip> lookup <table>`, не по pref.

## Что решает маршрут, а что — правила mihomo

- **table 100** (обычный LAN) → mihomo `7890` → применяются `rules:` (RULE-SET/VPN/AI/DIRECT).
- **table 101** (AI-устройства) → mihomo `7891`, у которого `proxy: AI` — правила `rules:` **не**
  применяются, весь трафик форсированно идёт через группу `AI`.

Следствие: чтобы «домен → AI» для AI-устройства, менять правила mihomo **не нужно** — оно и так
в AI. Добавление домена в AI-группу magitrickle влияет только на обычные устройства (путь через
`7890`).

## ICMP-bypass

LAN-ICMP (`iif br-lan ipproto icmp`) уходит в `main` (напрямую), иначе ping до помеченного
magitrickle IP умирает внутри TUN. Приоритет `19901`.

## AI-DNS (метка `0x64`)

Пакеты DoH-воркера (user `dnsai`) помечаются `0x64` в nft-таблице `geomask_ai_dns`; правило
`19902 fwmark 0x64 lookup 101` отправляет их в `dev AI`. Подробности — [AIDNS.md](AIDNS.md).

## Диагностика

```sh
ip rule show
ip route show table 101 | head
ip route get 8.8.8.8 mark 0x64            # должно: dev AI table 101
ip route get 8.8.8.8 from <ip> mark 0x4d616769  # должно: dev AI table 101 (даже с меткой magitrickle)
nft list chain ip mangle PREROUTING | head -8   # geomask-excl должны быть ПЕРЕД MT_*
logread | grep geomask-guard              # действия watchdog (пересадки, сбросы)
conntrack -L 2>/dev/null | grep <ip>      # dst=198.19.0.1 → AI, dst=198.18.0.1 → main
```
