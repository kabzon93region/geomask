# Канареечный прогон на живом роутере, 19.11.2026

Статус: **выполнен** (три итерации, финальная — успешная). Роутер: OpenWrt 25.12.5,
mediatek/filogic, `root@192.168.0.1`, установлен geomask **1.31.0**.

Документ фиксирует только наблюдаемое: что запускалось, что сломалось, что исправлено и чем
это подтверждено. Пункты плана, которым нужен reboot/стенд, остаются OPEN.

## Итог трёх прогонов

| Прогон | Время | Что произошло | Результат |
|---|---|---|---|
| 1 | 13:03–13:15 | установка 1.31.0, `--auto` оборвался на шаге 4 | шаг 4: ложный `AI-DNS: mark rule for 0x64 missing`; шаги 5–7 не выполнены; монитор 10/10 здоров; rc=1 |
| 2 | 13:20–13:31 | шаг 4 прошёл, шаг 7 отказал | `{"ok":false,"error":"нужен свежий полный бэкап"}` при свежем бэкапе; шаг 7 (IPv6) не применён; монитор 10/10 здоров; rc=1 |
| 3 | 13:33–13:45 | установка завершилась | `SUMMARY: pass=5 fail=0`, rc=**0**; `--check` после установки: `pass=6 fail=0`, rc=**0**; монитор **10/10** здоров |

## Дефекты, найденные на живом устройстве, и исправления

1. **nft печатает mark с padding** — `meta mark set 0x00000064`, а проверка шага 4 искала
   `mark set 0x64`. Правило было на месте. Исправлено: `_aidns_mark_present` сравнивает
   значение численно (hex/dec, любой padding). Коммит `eb6cd55`, тест
   `test_install_safety.py::test_the_ai_dns_mark_check_accepts_zero_padded_hex`.
2. **В busybox этой прошивки нет `stat(1)`** (`stat: not found`, rc 127). Из-за этого
   `_write_gate` видел возраст 0 и отказывал в записи («нужен свежий полный бэкап»), а
   snapshot переустановки записывал пустые mode/uid/dev:ino/size. Исправлено: гейт
   использует `date -r FILE +%s` как фолбэк; snapshot — `ls -ld`/`ls -ln`/`ls -i`/`wc -c`.
   Коммит `e3f787d`, тесты `test_backend_safety::test_the_write_gate_survives_a_missing_stat`
   и `test_install_safety::test_snapshot_metadata_works_without_stat`.

## Финальное состояние (проверено после прогона 3)

- `version=1.31.0`, mihomo pid 5510 (без рестарта — сервис был здоров), uptime 4 дня 12 ч
  (роутер **не** перезагружался), overlay: 78.4 МБ свободно.
- IPv6: `network.lan.ip6assign=0`, `dhcp.lan.ra=disabled`, глобальных адресов нет — шаг 7
  применил default «IPv6 off» (это изменение конфигурации владельца, штатное для geomask).
- `backup-ok`: `file=/root/geomask-backups/router-backup-20261119-133435.tar.gz`,
  `ts=1795084484` (свежий полный бэкап, созданный установщиком).
- `--check` после установки: `pass=6 fail=0 skipped_steps=6`, rc=0.

## Артефакты (ПК)

`Y:\openwrt\backup\canary-20261119-133353\` (успешный прогон):
- `canary-sysupgrade.tar.gz` sha256 `ac2addc2c4db4ff1b6d866e3311772f0bffcb615b8a18658c8c7fcc38b96cdc0`
- `canary-overlay.tar.gz` sha256 `770e7f03b1042b9ec78adf02ac313b2eec885c811197414ec97c52140ca42290`
- `canary-restore-set.txt` (871 путь), `canary-check-before.txt`, `canary-check-after.txt`,
  `install.log`, `install.log.remote`, `monitor.log`, `report.md`
- репетиция восстановления: `SAME` для `etc/config/network`, `firewall`, `dhcp` и
  `etc/mihomo/config.yaml`.

Предыдущие прогоны: `canary-20261119-130348` (1), `canary-20261119-132019` (2) — в них
`install.log` содержит исходные ошибки.

## Что это доказывает по плану

- Установка/обновление на живом OpenWrt проходит целиком: env/geo/subs/aidns/sync/services/
  verify, все проверки после — `ALL GOOD` (задача 2, задача 5 в части применения).
- AI-DNS на реальном устройстве: mark/DNAT/route-проверки проходят, «marked DNS exits via AI
  tunnel» и DNAT ai-устройств подтверждены (C04/C07, P10 в части порядка).
- IPv6 default-off применяется и проверяется на устройстве (C06 в части «на живой системе»;
  kernel/reboot — нет).
- Бэкап установщика содержит `etc/mihomo/config.yaml`; восстановление репетировано, но
  **фактический restore/reboot не выполнялся** (P09, P06 — остаются OPEN-STAND).
- Монитор 3×10 кругов: ping/SSH/интернет через роутер/DNS/mihomo/TUN/ip-rule/egress роутера
  стабильны (P01/P10 в части наблюдаемого здоровья — egress защищённого трафика не доказан).

## Что НЕ доказано этим прогоном

- power loss / kill во время установки и фактический откат (P06/P09) — нужен стенд;
- reboot/перезагрузка после установки и поведение после неё;
- ENOSPC и нехватка inode;
- G05 (два hev-инстанса, egress каждого) — не реализовано;
- реальные роли/ACL rpcd и полный retrieval 2 MiB (P07);
- обновление модулей (P02) и переустановка phase2a–2f на устройстве (R03/P03);
- матрица профилей и другие архитектуры.

## Следующие шаги

1. `gate29` на коммитах `eb6cd55`+`e3f787d` — прогнать, опубликовать `master`.
2. Перезагрузка роутера в согласованное с владельцем окно: проверить, что 1.31.0 и IPv6-off
   переживают reboot, сервисы/TUN/правила поднимаются, `--check` остаётся `ALL GOOD`.
3. После reboot — переустановка (`--reinstall`) в отдельное окно, с теми же бэкапами: это
   проверит snapshot/restore на живой системе (R03) и фолбэки `stat`.
4. Остальное — по `STAND-GATES-RUNBOOK.md`.

## Пост-канареечные находки (тот же день, после reboot)

Перезагрузка выявила то, что установка и монитор увидеть не могли:

1. **Firewall не поднялся** (`/etc/nftables.d/99-geomask-no-v6.nft: unexpected table`), NAT
   пропал. `_ipv6_guard_install` писал drop-in как `table inet fw4 { ... }`, а fw4 включает
   эти файлы *внутри* своей таблицы. Исправлено на `chain geomask_no_v6 { ... }`
   (`e9ebf0f`), перезагрузка после фикса проходит.
2. **Старые файлы после обновления**: шаги 4 и 5 при «check: ALL GOOD» пропускали apply, и
   `geomask-syncd.sh`, `geomask-lane.sh`, `geomask-node-scan.sh`, `geomask-seed-lists.py`,
   hotplug и др. оставались от 1.30.21. Из-за этого панель годами показывала «доступны
   обновления». Теперь шаги 2, 4, 5 всегда применяются (`1688927`); после установки все
   15 модулей `current`.
3. **Диагностический архив** отваливался по таймауту: 30 сохранённых логов редактировались 30
   отдельными запусками Python (16 с из 30 с бюджета ubus). Добавлен `batch text <manifest>`
   (`1688927`, `afbd67a`), сейчас `ubus call geo-sync diag_bundle` — 16 с, 34 556 байт, 30
   проверок, панель скачивает.
4. **`stat(1)` отсутствует** в busybox этой прошивки: гейт записей и snapshot падали/теряли
   метаданные (`e3f787d`, плюс `ls -l -n` в `1bd16e60`/`a54d640`).
5. **AI-DNS mark** сравнивался как строка, а nft печатает `0x00000064` (`eb6cd55`).

## Финальное состояние после третьей перезагрузки (18:15)

- `uptime` 202 с, `version=1.31.0`, сборка `705eea0d…` (установлена с ПК, sha256 сверен).
- `ip6assign=0`, `ra=disabled`, глобальных IPv6 нет; `table inet fw4` на месте,
  `chain geomask_no_v6` в ней с `type filter hook forward priority filter - 10`.
- mihomo pid 5601, три hev-процесса, TUN `Mihomo/AI/VPN-noP2P/Wireguard`, ip-rule 100/101,
  интернет и DNS с роутера — ok.
- `geomask --check`: `pass=4 fail=0`, rc=0; все 15 модулей `current` (после публикации
  `master`); архив диагностики — 16 с, 34 556 байт.
- Бэкап перед перезагрузкой: `router-backup-20261119-181307.tar.gz` (11 397 957 байт) +
  overlay-архив, sha256 сверены на ПК (`preboot3-20261119-181509`).
- `gate34`: 97 модулей, 1424 теста, 1394 выполнено, 30 allowlisted skip, 0 fail, PASS.

Остаются стендовыми: power loss/ENOSPC, G05, реальный rpcd/ACL, обновление модулей с
установкой файлов, переустановка phase2a–2f на устройстве, матрица профилей.

## Переустановка на живом роутере (вечер 19.11.2026)

`--reinstall --yes` под тем же сторожем, что и обновление (бэкап + репетиция restore +
монитор + авто-откат). Четыре попытки, каждая отказывала **до первой мутации** — preflight
сработал как задумано, и каждый отказ был ложным:

1. `MagiTrickle state: an empty /etc/magitrickle/state/config.yaml` — проверка искала
   `- name:`, а 0.8.x пишет `- id:`. Исправлено (`0def5d0`).
2. `unrestorable object: /etc/geomask/node-audit/state is a directory` — state-каталог
   объявлен regular-файлом. Переведён в `dir` с `snapshot NONE`, строка byte-таблицы убрана,
   проба каталога терпит отсутствие (`0def5d0`).
3. `snapshot: cannot stat /etc/mihomo` — без `stat(1)` фолбэк считал `wc -c < dir` и
   `ls` без `-d`; каталоги теперь `ls -l -n -d`, uid/inode с `-d`, пустые поля больше не
   отбрасывают всё значение (`f26a941`).
4. `pending uci delta for dhcp` — `/tmp/.uci/dhcp` был **пустым**; пустой delta теперь не
   считается чужой правкой (`e8b5af8`).

Успешный прогон (`canary-20261119-194522`, сборка `261df282…`): установка 70 с,
`verify: PASS`, `Переустановка geomask завершена`, rc=0; `--check` после — rc=0; монитор
10/10 здоров. mihomo не перезапускался (маркер владельца `/root/.geomask-norestart`).

После перезагрузки (19:58): `uptime` 1841 с, `version=1.31.0`, сборка `261df282…`,
`table inet fw4` + `chain geomask_no_v6`, mihomo pid 5750, три hev, magitrickle,
`geomask-syncd` running, TUN `Mihomo/AI/VPN-noP2P/Wireguard`, ip-rule 100/101, IPv6 off,
интернет/DNS ok, 15/15 модулей `current`, `--check` rc=0 — переустановка пережила reboot.

Бэкап перед reboot: `router-backup-20261119-194611.tar.gz` (sha256 `ab0f6287…`) +
`sysupgrade.tar.gz` `14718552…` и `overlay.tar.gz` `1fd77e3a…` на ПК
(`Y:\openwrt\backup\preboot4-20261119-195821`).
