# geomask

Автоматизированный установщик и конфигуратор домашнего стека обхода блокировок на OpenWrt (wwGate AX3000 / mediatek-filogic):

- **Zapret-Manager** (админ-панель, форк StressOzz/Zapret-Manager) — установка/проверка;
- **гео-оверлей** — панель «Гео-маскировка и AI» в LuCI (rpcd geo-sync + Web-страница);
- **mihomo с 2 подписками** — группы `VPN` (основная подписка, exclude РФ/моб.) и `AI` (немецкая нода для Google/AI); AI-подписка меняется из LuCI-страницы (поле + «Применить») или терминала — без перезапуска mihomo;
- **magitrickle → mihomo sync** — DNS-маршрутизация по доменам через cron каждые 5 минут;
- **сервисы и E2E-проверки** — dnsmasq/magitrickle/mihomo/hev-socks5-tunnel + сквозной verify.

## Установка (на роутере)

```sh
sh <(wget -qO - 'https://raw.githubusercontent.com/kabzon93region/geomask/master/geomask.sh')
```

Или локально: скопируйте `geomask.sh` на роутер и запустите `sh /root/geomask.sh`.

## Режимы

| Режим | Команда | Что делает |
|---|---|---|
| Интерактив | `sh geomask.sh` | Главное меню: сводка статусов (роутер, сервисы, VPN/AI узлы, geo, pin) + циклическое меню действий |
| Пересстановка | `sh geomask.sh --reinstall` | «Удалить → установить → настроить geomask»: сносит гео-оверлей/sync/cron, переустанавливает всё, спрашивает обе подписки; **mihomo не перезапускается** |
| Полный авто | `sh geomask.sh --auto` | Все шаги без вопросов; остановка при verify-FAIL |
| Только проверка | `sh geomask.sh --check` | Read-only отчёт по всем шагам, ничего не меняет |
| Один шаг | `sh geomask.sh --step 4` | Только шаг N (нумерация ниже) |
| Без вопросов | `+ --yes` | Не спрашивать подтверждений |
| С рестартом | `+ --restart` | Разрешить перезапуск mihomo (обрыв VPN ~3с). **Игнорируется**, если на роутере есть флаг `/root/.geomask-norestart` |
| С подписками | `+ --vpn-url URL --ai-url URL` | Задать URL подписок (в конфиг попадают только туда) |

После каждого действия меню возвращается на главный экран со свежей сводкой. Пункты меню: диагностика, подписки, гео-панель, Zapret-Manager, sync, сервисы, verify, пошаговый мастер, пересстановка (`f`).

Подписки применяются **без перезапуска mihomo**: конфиг пишется с бэкапом, затем провайдеры `sub`/`de5` обновляются через локальный API (`PUT /providers/proxies/...`) — VPN не прерывается. Полный рестарт только по `--restart`.

## Структура репозитория

```
geomask.sh       # готовый установщик (публикуется, его и качает wget)
geomask.sh.in    # шаблон — править его
build.py         # сборка: python build.py -> geomask.sh
assets/          # файлы, вливаемые в установщик:
  geo-backend.sh           # бэкенд гео-панели (Go/sh логика на роутере)
  geo-sync                 # rpcd-плагин (ubus-объект geo-sync)
  acl-geo-sync.json        # ACL: доступ LuCI к geo-sync
  menu-geo-sync.json       # пункт меню «Гео-маскировка и AI»
  geo-sync-page.js         # КОД СТРАНИЦЫ АДМИНКИ в LuCI (view/geo-sync/main.js): статусы, AI-подписка, устройства
  sync_mtrickle_mihomo.sh  # magitrickle -> mihomo ruleset sync
  ytdirect.yaml            # rule-provider DIRECT-доменов
  99-default-vpn-route     # hotplug: LAN -> TUN роутинг
```

> Код страницы админки в LuCI — `assets/geo-sync-page.js` (устанавливается на роутер как `/www/luci-static/resources/view/geo-sync/main.js`).

## Шаги

1. **env** — проверка окружения: OpenWrt, apk/opkg, python3, curl, место, интернет.
2. **panel** — официальная панель Zapret-Manager (по явному согласию скачивается внешний установщик StressOzz).
3. **geo** — установка гео-оверлея: `/usr/bin/geo-backend.sh`, rpcd `geo-sync`, ACL, меню LuCI, страница `geo-sync/main.js`.
4. **subs** — конфиг mihomo: два proxy-provider (`sub` + `de5`), группы `VPN`/`AI`, правила `ytdirect→DIRECT`, `cloudflare-ech→AI`, `ai→AI`, `mtrickle→VPN`, `MATCH→GLOBAL`, sniffer. Перед записью — бэкап и `mihomo -t`.
5. **sync** — `/root/sync_mtrickle_mihomo.sh` + cron `*/5` + `ytdirect.yaml` + hotplug-роутинг (`99-default-vpn-route`).
6. **services** — запуск **только упавших** сервисов; работающие не трогаются.
7. **verify** — E2E: DNS обычный/adblock, HTTP 204, группы VPN/AI в mihomo API, ubus панели, ICMP-bypass правило.

## Безопасность

- **Никакого reboot** и никаких касаний network/pppoe-конфига.
- Работающие сервисы не перезапускаются (только старт упавших).
- Бэкап перед каждой записью файла (`.bak_geomask_<timestamp>`).
- `mihomo -t` обязан пройти до установки конфига; при провале — старый конфиг остаётся.
- Перезапуск mihomo — только с `--restart` или явным «да»; по умолчанию подписки применяются через API без перезапуска.
- **Жёсткая pin-блокировка**: файл `/root/.geomask-norestart` на роутере полностью отключает `--restart` (даже с флагом соединение не прервётся). Чтобы вернуть рестарты — удалите файл.
- Секреты (URL подписок, hwid) не входят в скрипт — передаются аргументами и остаются в конфиге роутера.

## Разработка

```sh
python build.py        # собирает geomask.sh из geomask.sh.in + assets/
```

После правки любого файла в `assets/` пересоберите установщик и прогоните тесты.

### Тесты

Локальная песочница `sandbox/` (fake root + стабы команд `pidof/ubus/mihomo/curl/crontab/nslookup/ip/wget/apk`) запускается на любой машине без роутера и без VM/WSL:

```sh
bash sandbox/run_tests.sh     # ожидаемо: 25/25 PASS
```

Песочница — локальный инструмент разработки, **в репозиторий не входит** (в `.gitignore`).

## Требования (роутер)

- OpenWrt 24.10+ (проверено на 25.12, aarch64/cortex-a53)
- `python3`, `curl`, `wget`, `ubus`/`rpcd`/`luci`
- Для шага 2: доступ к GitHub raw
- Для шага 4: mihomo бинарь (ставится панелью Zapret-Manager, Mixomo)

## Лицензия

MIT. Гео-оверлей и синк-скрипты — собственные доработки; Zapret-Manager — © StressOzz, GPL-2.0.
