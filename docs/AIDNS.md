# AI-DNS без утечки

## Проблема

Если AI-устройство ходит через AI-туннель, но DNS резолвится обычным резолвером/ISP, то DNS-запросы
утекают мимо туннеля, а некоторые сервисы видят «DNS из РФ» и флагуют использование VPN
(«страна из AI, DNS из ISP»).

## Цепочка (реализация)

```
AI-device DNS (any dst, :53)
  ──nft DNAT (nat prerouting priority -110; выигрывает у magitrickle/adblock/force_dns)──►
     192.168.0.53:5353   (второй dnsmasq, AI)
     └─► 127.0.0.1:5055  (второй DoH-воркер, user dnsai)
          ──nft mark 0x64 (по skuid)──► ip rule 19902 fwmark 0x64 lookup 101 ──► dev AI ──► AI-нода
```

RU-домены при этом резолвятся как прежде (через обычный путь) на других устройствах.

## Компоненты

| Элемент | Значение |
|---|---|
| DNAT | `ip saddr { <ai-ips> } meta l4proto { tcp,udp } th dport 53 dnat ip to 192.168.0.53:5353` |
| Второй dnsmasq | слушает `192.168.0.53:5353` (`/etc/dnsmasq-ai.conf`) |
| Второй DoH | `https-dns-proxy` как user `dnsai`, `127.0.0.1:5055` (`init.d/ai-doh`) |
| nft-таблица | `geomask_ai_dns` (файл `/usr/share/nftables.d/ruleset-post/98-geomask-ai-dns.nft`) |
| Метка | `0x64` (skuid `dnsai`) |
| ip rule | `19902 fwmark 0x64 lookup 101` |

Nft-таблица содержит:
- `pre_nat` (hook `prerouting`, priority `-110`): DNAT порта 53 AI-устройств на `192.168.0.53:5353`;
- `out_mark` (hook `output`, priority `-150`): `meta skuid <dnsai> mark set 0x64`.

Список AI-устройств берётся из `/etc/geo-devices.list` и синхронизируется при изменении набора.

## Управление

- Флаг `dnsai`/UID: `/etc/geomask-aidns.uid`.
- **Отключение**: `touch /etc/geomask-aidns.off` — шаг aidns становится no-op (DNS вернётся к обычной
  цепочке; возможна «DNS-утечка» для AI-устройств).

## Bootstrap-значения (осознанный хардкод)

`/etc/init.d/ai-doh` содержит значения, которые нельзя вычислить из конфига: они нужны
раньше, чем появится любая другая точка опоры, поэтому они зафиксированы в скрипте, а не в
`uci`/`/etc/geomask/aidns.env`:

| Значение | Где | Почему именно так |
|---|---|---|
| `-b 8.8.8.8,1.1.1.1` | `ai-doh.init` | bootstrap-резолверы для самого DoH-эндпоинта: без них `https-dns-proxy` не стартует, а AI-DNS резолвер ещё не поднят. Это не политика upstream (её задаёт `dnsmasq-ai.conf` → `127.0.0.1:5055`). |
| `-p 5055` | `ai-doh.init`, установщик (`AIDNS_PORT=5055`, `dnsmasq-ai.conf`) | loopback-порт второго DoH-воркера; связан с `server=127.0.0.1#5055` в `/etc/dnsmasq-ai.conf`. Менять только вместе с установщиком и переустановкой AI-DNS. |
| `-t socks5h://127.0.0.1:7891` | `ai-doh.init` | mihomo-listener `ai-devices` (proxy-группа `AI`); без него DoH уходит мимо AI-туннеля. |
| `AI TUN = AI` | `geo-backend.sh` (`_ai_tun_ensure`), `geomask.sh.in` | имя TUN задано кодом; alias-адрес второго dnsmasq берётся из UCI (`network.lanai`), а не из константы. |

Отдельного env-файла нет сознательно: `procd_set_param command` — статическая строка, а
генерация `/etc/geomask/aidns.env` добавила бы ещё один объект в write-set переустановки без
выигрыша (порт и bootstrap-серверы меняются только вместе с кодом). При смене этих значений
нужно править `ai-doh.init` + `geomask.sh.in` (`AIDNS_PORT`) и переустанавливать модуль ai_dns.

`ai-doh.init` при старте пишет в syslog (`logger -t ai-doh`), если AI TUN или listener
`127.0.0.1:7891` ещё не подняты. Это не отказ: порядок загрузки и переподключение mihomo
могут опережать воркер, procd перезапускает его сам. `stderr 1` отправляет ошибки воркера в
syslog; `dnsmasq-ai.init` при отсутствии `/etc/dnsmasq-ai.conf` пишет причину и возвращает
`1` вместо молчаливого respawn, а `procd_set_param file` перезапускает его при перезаписи
конфига.

## Проверка

```sh
nft list table inet geomask_ai_dns
netstat -tlnp | grep -E '5055|5353'
ip rule show | grep 0x64
logread -e ai-doh | tail
```
