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
          ──nft mark 0x64 (по skuid)──► ip rule 20002 fwmark 0x64 lookup 101 ──► dev AI ──► AI-нода
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
| ip rule | `20002 fwmark 0x64 lookup 101` |

Nft-таблица содержит:
- `pre_nat` (hook `prerouting`, priority `-110`): DNAT порта 53 AI-устройств на `192.168.0.53:5353`;
- `out_mark` (hook `output`, priority `-150`): `meta skuid <dnsai> mark set 0x64`.

Список AI-устройств берётся из `/etc/geo-devices.list` и синхронизируется при изменении набора.

## Управление

- Флаг `dnsai`/UID: `/etc/geomask-aidns.uid`.
- **Отключение**: `touch /etc/geomask-aidns.off` — шаг aidns становится no-op (DNS вернётся к обычной
  цепочке; возможна «DNS-утечка» для AI-устройств).

## Проверка

```sh
nft list table inet geomask_ai_dns
netstat -tlnp | grep -E '5055|5353'
ip rule show | grep 0x64
```
