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
   - AI-устройство → `ip rule 19900 from <ip> lookup 101` → table 101 → `dev AI` → hev ai → mihomo `7891` (proxy форсит `AI`);
   - bypass-устройство → `ip rule 19900 from <ip> lookup main` → напрямую;
   - DNS AI-устройства → nft-DNAT → `192.168.0.53:5353` → DoH (user `dnsai`) → метка `0x64` → `ip rule 19902` → table 101 → `dev AI`.

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
| `/usr/lib/geomask/geomask-doctor.py` | Диагностика: проверки, маршрут домена, логи (модуль `doctor`) |
| `/usr/lib/geomask/geomask-lists-edit.py` | Точечная правка списков MagiTrickle (`groups/add/move/remove`) |
| `/usr/lib/geomask/geomask-module-update.py` | Обновление модулей по каталогу (sha256, откат) |
| `/etc/geomask/modules-index.json` | Каталог модулей: файлы, режимы, хеши, версии |
| `/etc/geomask/modules.json` | Выбранный набор модулей |
| `/etc/geomask/version` | Релиз этого роутера (панель сравнивает с GitHub) |
| `/etc/geomask/modules-state.json` | Базовая линия для «изменён локально» и версии частей |
| `/root/geomask-backups/` | Полные бэкапы (`sysupgrade -b`): ротация — самая свежая одна; SMB-шар `geomask-backups` (guest, read-only), ссылка в панели (v1.9.0); сборка в /tmp + перенос исключает вложение архивов |

## Инсталлятор: шаги

1. **env** — окружение: OpenWrt, apk/opkg, `mihomo` и `magitrickle` (обязательны), место,
   интернет. Здесь же **ставятся** `python3` и `curl`, если их нет (v1.30.6). На голом
   роутере шаг падает до записи файлов и говорит, что поставить сначала (v1.30.5).
2. **geo** — файлы: панель, rpcd, ACL, меню, скрипты ядра, каталог модулей, диагностика,
   правка списков; запись выбранного набора модулей. Файлы снятых модулей не пишутся.
3. **subs** — конфиг mihomo (провайдеры, группы, правила, listeners, sniffer) + `mihomo -t`.
4. **aidns** — цепочка AI-DNS без утечки (nft + второй dnsmasq + DoH + ip rule);
   `https-dns-proxy` ставится автоматически, если его нет (v1.30.7).
5. **sync** — скрипт синка + cron `*/5` + `rule-provider` + hotplug-роутинг, node-audit, guard.
6. **services** — старт только упавших сервисов.
7. **verify** — проверки; шаг только читает.

## Сборка

`build.py` подставляет содержимое файлов `assets/*` в плейсхолдеры `@@ASSET:<name>@@` шаблона
`geomask.sh.in` и пишет `geomask.sh` (chmod 755). См. [DEVELOPMENT.md](DEVELOPMENT.md).
