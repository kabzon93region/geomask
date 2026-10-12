# zm-setup

Автоматизированный установщик и конфигуратор домашнего стека обхода блокировок на OpenWrt (wwGate AX3000 / mediatek-filogic):

- **Zapret-Manager** (админ-панель, форк StressOzz/Zapret-Manager) — установка/проверка;
- **гео-оверлей** — панель «Гео-маскировка и AI» в LuCI (rpcd geo-sync + Web-страница);
- **mihomo с 2 подписками** — группы `VPN` (основная подписка, exclude РФ/моб.) и `AI` (немецкая нода для Google/AI);
- **magitrickle → mihomo sync** — DNS-маршрутизация по доменам через cron каждые 5 минут;
- **сервисы и E2E-проверки** — dnsmasq/magitrickle/mihomo/hev-socks5-tunnel + сквозной verify.

## Установка (на роутере)

```sh
sh <(wget -qO - 'https://raw.githubusercontent.com/<you>/zm-setup/main/zm-setup.sh')
```

Или локально: скопируйте `zm-setup.sh` на роутер и запустите `sh /root/zm-setup.sh`.

## Режимы

| Режим | Команда | Что делает |
|---|---|---|
| Интерактив | `sh zm-setup.sh` | Меню: выбираешь шаги, каждый шаг — check → вопрос → apply → verify |
| Полный авто | `sh zm-setup.sh --auto` | Все шаги без вопросов; остановка при verify-FAIL |
| Только проверка | `sh zm-setup.sh --check` | Read-only отчёт по всем шагам, ничего не меняет |
| Один шаг | `sh zm-setup.sh --step 4` | Только шаг N (нумерация ниже) |
| Без вопросов | `+ --yes` | Не спрашивать подтверждений |
| С рестартом | `+ --restart` | Разрешить перезапуск mihomo и однократный sync (обрыв VPN ~3с) |
| С подписками | `+ --vpn-url URL --ai-url URL` | Задать URL подписок для шага 4 (в конфиг попадают только туда) |

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
- Бэкап перед каждой записью файла (`.bak_zmsetup_<timestamp>`).
- `mihomo -t` обязан пройти до установки конфига; при провале — старый конфиг остаётся.
- Перезапуск mihomo / однократный sync — только с `--restart` или явным «да» в интерактиве.
- Секреты (URL подписок, hwid) не входят в скрипт — передаются аргументами и остаются в конфиге роутера.

## Разработка

```
zm-setup.sh.in   # шаблон (правьте его)
assets/          # файлы, вливаемые в скрипт (сняты с рабочего роутера)
build.py         # сборка: python build.py  ->  zm-setup.sh
sandbox/         # тестовая песочница (fake root + стабы команд)
```

### Песочница (без роутера и без VM/WSL)

```sh
bash sandbox/run_tests.sh
```

Стабы (`sandbox/bin/pidof, ubus, mihomo, curl, crontab, nslookup, ip, wget, apk, python3`) читают состояние из `sandbox/state/`, фейковая файловая система — `sandbox/root_*` через переменную `ZM_PREFIX`. Сценарии: пустой root (всё FAIL, rc=1), здоровый root (всё OK, rc=0), auto-ремонт подписок, CLI-края. Ожидаемо: **13/13 PASS**.

### Сборка

```sh
python build.py        # собирает zm-setup.sh из zm-setup.sh.in + assets/
```

После правки любого файла в `assets/` пересоберите скрипт и прогоните песочницу.

## Требования (роутер)

- OpenWrt 24.10+ (проверено на 25.12, aarch64/cortex-a53)
- `python3`, `curl`, `wget`, `ubus`/`rpcd`/`luci`
- Для шага 2: доступ к GitHub raw
- Для шага 4: mihomo бинарь (устанавливается панелью Zapret-Manager, Mixomo)

## Лицензия

MIT. Гео-оверлей и синк-скрипты — собственные доработки; Zapret-Manager — © StressOzz, GPL-2.0.
