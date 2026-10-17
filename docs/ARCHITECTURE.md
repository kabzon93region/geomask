# Архитектура

## Назначение

geomask ставит на OpenWrt-роутер домашний стек обхода блокировок и даёт управление им из LuCI.
Инсталлятор идемпотентен, ничего не перезагружает и не трогает базовую сеть (`network`/`pppoe`).

## Компоненты

```
                         ┌─────────────────────────── LuCI ───────────────────────────┐
                         │  view/geo-sync/main.js  ──ubus──►  rpcd geo-sync (geo-backend)│
                         │  (статусы, подписки, устройства, режимы)                    │
                         └────────────────────────────────────────────────────────────┘
                                                    │
                    ┌───────────────────────────────┼───────────────────────────────┐
                    ▼                               ▼                               ▼
            /etc/config/mihomo                magitrickle                       /etc/geo-devices.list
            (proxy-providers, rules)          (DNS-routing)                     (dip dmode dname)
                    │                               │                               │
        ┌───────────┴────────────┐        sync_mtrickle_mihomo.sh            hotplug 99-default-vpn-route
        ▼                        ▼        (cron */5) → ruleset/*.yaml         (tables 100/101 + ip rule)
  hev main (TUN Mihomo)   hev ai (TUN AI)
    198.18.0.1              198.19.0.1
    socks 127.0.0.1:7890    socks 127.0.0.1:7891
        │                        │
        ▼                        ▼
   mihomo listener          mihomo listener
   (rules → VPN/AI/…)       ai-devices:7891 (proxy: AI)
```

## Поток данных

1. LAN-клиент → dnsmasq (маршрутизация по домену через magitrickle) → DoH.
2. Трафик клиента:
   - обычный LAN → `ip rule 32760` → table 100 → `dev Mihomo` → hev main → mihomo `7890` → правила (`RULE-SET` → VPN/AI/DIRECT);
   - AI-устройство → `ip rule 20000 from <ip> lookup 101` → table 101 → `dev AI` → hev ai → mihomo `7891` (proxy форсит `AI`);
   - bypass-устройство → `ip rule 20000 from <ip> lookup main` → напрямую;
   - DNS AI-устройства → nft-DNAT → `192.168.0.53:5353` → DoH (user `dnsai`) → метка `0x64` → `ip rule 20002` → table 101 → `dev AI`.

## Карта установленных файлов (роутер)

| Путь | Назначение |
|---|---|
| `/usr/bin/geo-backend.sh` | Бэкенд гео-панели (режимы устройств, подписки, статусы) |
| `/usr/libexec/rpcd/geo-sync` | rpcd-плагин, ubus-объект `geo-sync` |
| `/usr/share/rpcd/acl.d/luci-app-geo-sync.json` | ACL доступа LuCI к `geo-sync` |
| `/usr/share/luci/menu.d/luci-app-geo-sync.json` | Пункт меню «Гео-маскировка и AI» |
| `/www/luci-static/resources/view/geo-sync/main.js` | Код страницы админки |
| `/etc/config/mihomo` | Конфиг mihomo (провайдеры, группы, правила, listeners) |
| `/etc/mihomo/ruleset/{mtrickle,ai,ytdirect}.yaml` | rule-providers (file, classical) |
| `/root/sync_mtrickle_mihomo.sh` | Синк magitrickle → mihomo |
| `/etc/hotplug.d/iface/99-default-vpn-route` | Роутинг LAN→TUN, per-device `ip rule` |
| `/usr/share/nftables.d/ruleset-post/98-geomask-ai-dns.nft` | nft-таблица AI-DNS (`geomask_ai_dns`) |
| `/etc/init.d/{ai-doh,dnsmasq-ai}` | Второй DoH-воркер и второй dnsmasq для AI-DNS |
| `/etc/geo-devices.list` | Список устройств: `IP mode name` (`bypass`/`ai`/`realtz`) |
| `/etc/geomask-aidns.uid` / `-aidns.off` | UID пользователя `dnsai` / флаг отключения AI-DNS |
| `/root/geomask-backups/` | Полные бэкапы (`sysupgrade -b`) |

## Инсталлятор: шаги

1. **env** — проверка окружения (OpenWrt, apk/opkg, python3, curl, место, интернет).
2. **geo** — гео-оверлей (файлы панели + rpcd + ACL + меню).
3. **subs** — конфиг mihomo (провайдеры, группы, правила, listeners, sniffer) + `mihomo -t`.
4. **aidns** — цепочка AI-DNS без утечки (nft + второй dnsmasq + DoH + ip rule).
5. **sync** — скрипт синка + cron `*/5` + `rule-provider` + hotplug-роутинг.
6. **services** — старт только упавших сервисов.
7. **verify** — E2E-проверки.

## Сборка

`build.py` подставляет содержимое файлов `assets/*` в плейсхолдеры `@@ASSET:<name>@@` шаблона
`geomask.sh.in` и пишет `geomask.sh` (chmod 755). См. [DEVELOPMENT.md](DEVELOPMENT.md).
