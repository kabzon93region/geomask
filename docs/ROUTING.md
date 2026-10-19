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
(18.10.2026: `2ip.io`/`myip.com` показывали РФ-IP на AI-устройстве). За 18.10 база продолжила дрейф
(`19979 → 19537 → 19416`) — пересоздания происходят регулярно.

**Вывод: любые жёстко заданные числа проигрывают.** Стабильность даёт **geomask-guard (v1.6.1)** —
три слоя:

- **(A) nft-исключение**: `ip saddr <device> return` с комментарием `geomask-excl` в **голове**
  `ip mangle PREROUTING` (перед прыжками `MT_*`) — пакеты ai/bypass-устройств не получают метку
  magitrickle. Ограничение: позиция зависит от порядка в общей цепочке — при пересоздании правил
  magitrickle прыжки `MT_*` могут встать выше (окно до следующего прогона guard, ≤5 мин);
- **(A2) mark-clear (v1.6.1, страховый слой)**: собственная таблица `ip geomask_excl`
  (hook prerouting **-149**, сразу после mangle magitrickle `-150` и до метки AI-DNS `-110`),
  одно правило: `ip saddr @excl_ips meta mark & 0xffff0000 == 0x4d610000 counter meta mark set 0`.
  Стирает **только** метки magitrickle (`0x4d61xxxx`) у наших устройств. Таблица **своя** —
  magitrickle и fw4 её не трогают, позиция в `PREROUTING` не важна → **окно уязвимости закрыто
  полностью**: даже если правило (A) вытеснено, к моменту выбора маршрута метки нет → fwmark-правила
  не совпадают → решает только `from <ip>`. Элементы set синхронизируются из `geo-devices.list`;
- **(B) watchdog** (cron `*/5`): правила `per-device/icmp/aidns` на месте и выше базы magitrickle —
  при нарушении пересаживает на `(база − 100)`, сбрасывает conntrack, пишет в syslog
  (`logread | grep geomask-guard`). Плюс страховка `table 101 default dev AI`.

Файлы: `/usr/bin/geomask-guard.sh`, hotplug `/etc/hotplug.d/iface/99-geomask-guard` (запуск при
`ifup`), cron `*/5 * * * * /usr/bin/geomask-guard.sh`.

> **Инцидент 18.10 23:27–23:35** (мотивация A2): в окне пересадки видео HLS не грузилось на `.125` —
> трафик уходил в основной туннель, где CDN видео (`hls.intrdb.com`/`cdn361`) не отдаёт сегменты с
> РФ-эгреджа (DIRECT/MAIN: таймаут 30 с; через AI-ноду: 2,5 с). После пересадки — восстановилось.

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
nft list table ip geomask_excl                 # mark-clear: set + правило (counter растёт при сбое позиции A)
logread | grep geomask-guard              # действия watchdog (пересадки, сбросы)
conntrack -L 2>/dev/null | grep <ip>      # dst=198.19.0.1 → AI, dst=198.18.0.1 → main
```
