# План: канареечный прогон geomask на реальном роутере с авто-откатом

Статус: **подготовлен, не выполнен**. Рабочий роутер в этой сессии не использовался.
Документ — исполнимый порядок действий с обязательным бэкапом, фоновым монитором и
автоматическим восстановлением. Все команды выполняются с ПК (Windows, PowerShell 5.1) в
`Y:\openwrt`; на роутере — только через SSH и только штатными средствами проекта.

## 0. Что доказываем и чем это отличается от WSL

| Что | Где доказывается | Этот прогон |
|---|---|---|
| Linux unit-gate (CI-187-класс): posix-only/симлинки/права | WSL/Linux-машина | нет (отдельный промпт `AGENT-PROMPT-LINUX.md`) |
| Установка/обновление на живом OpenWrt, сервисы, TUN, nft, DNS | роутер | **да** |
| Реальный dataplane: LAN-клиент → DNS/маршрут → интернет | роутер | **да**, в объёме текущей конфигурации |
| Power loss / ENOSPC / reboot-loop | нужен одноразовый стенд | нет |
| G05 (два hev-инстанса, egress каждого) | стенд + решение владельца | нет |
| Реальные роли/ACL rpcd, полный retrieval 2 MiB | стенд | нет |
| Матрица профилей (все/минимальный/ai_dns/…) | чистые образы | нет |

Вывод: этот прогон — самый объективный из доступных сейчас, но он **не заменяет** стендовые
пункты плана. Он закрывает runtime-часть текущего профиля и даёт материал для P01/P06/P09/
P10/P11/P07 (наблюдаемое поведение), не выдавая себя за полную матрицу.

## 1. Рекомендуемый порядок

1. **Сначала WSL-машина** (дёшево, без риска): прогнать `AGENT-PROMPT-LINUX.md` — Linux
   unit-gate на том же SHA. Это отсеивает Linux-only дефекты до касания роутера.
2. **Потом канареечный прогон на роутере** по этому плану (≈40–60 мин, из них ~20 мин
   мониторинг).
3. Если Linux-прогон или канарейка выявили дефекты — они идут в план починки отдельными
   партиями; повтор канарейки только после фикса и локального gate.

## 2. Что нужно до старта (проверить и записать)

- Доступ: `ssh -i ~/.ssh/openwrt_final root@192.168.0.1` (значение по умолчанию из
  `fetch_backup.ps1`; фактический LAN-IP подтвердить `uci get network.lan.ipaddr`).
- Роутер: модель, версия OpenWrt, `opkg`/`apk`, свободно в overlay и `/tmp` (`df -h`),
  `python3` + `python3-yaml`, установленная версия geomask.
- Текущая конфигурация: `uci show network/firewall/dhcp` — **не менять**; mihomo-конфиг не
  переписывать (`--replace-config` и `--adopt-config` в прогоне запрещены).
- Текущий статус здоровья: `sh /tmp/geomask-check.sh` — это `geomask.sh --check` (read-only),
  выход сохранить как baseline.
- Записать: `sysupgrade -l` (список файлов, попадающих в бэкап) — критично, см. §4.

## 3. Схема и роли

```
ПК (PowerShell)                     Роутер (OpenWrt)
----------------                    ----------------
canary.ps1  --оркестратор-->  ssh/scp  /tmp/canary/*
  | 1) бэкап (pull)                   
  | 2) репетиция restore (в /tmp)     
  | 3) установка (--auto --yes)       
  | 4) монитор 60 c  --probe-->  /tmp/geomask-canary-probe.sh (JSON)
  | 5) 3 фейла --> откат --> reboot --> проверка 2 мин
  +-- логи: Y:\openwrt\backup\canary-<ts>\monitor.log
```

Монитор живёт **на ПК**, а не на роутере: если установка ломает сеть/DNS, процесс на роутере
тоже может умереть, а ПК остаётся живым и может выполнить откат. Это принципиально.

## 4. Бэкап: два артефакта и проверка keep-list

OpenWrt `sysupgrade -b` архивирует **не всё**: `/etc/mihomo`, `/etc/geomask` и часть
установленных файлов в него не попадают, если их нет в `/etc/sysupgrade.conf` или
`/lib/upgrade/keep.d/*`. Поэтому:

1. `ssh ... 'sysupgrade -b /tmp/canary-sysupgrade.tar.gz'` — штатный LuCI-формат (UCI и
   системные файлы). Именно в `/tmp`, а не сразу в `/root/geomask-backups`: на этом роутере
   `/root` входит в keep-list, и бэкап, созданный внутри `/root`, включает сам себя
   (проверено: 24 МБ → 49 МБ с вложенной копией). После создания архив переносится в
   `/root/geomask-backups` только для удобства, а на ПК — через `scp`/`fetch_backup.ps1`;
   проверка `tar tzf | grep -c canary-` обязательна.
2. `ssh ... 'sysupgrade -l'` — **сохранить список** и убедиться, что в нём есть
   `/etc/config/network`, `/etc/config/firewall`, `/etc/config/dhcp`. Если `/etc/mihomo/`,
   `/etc/geomask/`, `/etc/init.d/geomask*`, `/usr/lib/geomask`, `/etc/hotplug.d/*geomask*`
   отсутствуют — собрать второй архив «overlay bundle»:
   `tar czf /tmp/canary-overlay.tar.gz -C / etc/mihomo etc/geomask etc/geomask-nodes.conf etc/geomask-ytwatch.conf etc/hotplug.d etc/init.d etc/crontabs etc/rc.d usr/lib/geomask usr/bin/<geomask*> www/luci-static/resources/<geomask*> 2>/dev/null`
   (точный список — из `tests/reinstall_write_set.json`, только существующие пути).
3. Проверка обоих: `tar -tzf ... | wc -l`, `sha256sum`, размер; список файлов сохранить на ПК
   (`tar -tzf` → `restore-set.txt`) — он же нужен для очистки «лишнего» при откате.
4. Скачать на ПК штатным `fetch_backup.ps1` (проверяет checksum; host key checking не
   выключается) в `Y:\openwrt\backup\canary-<ts>\`; overlay-архив — `scp`.
5. Записать `sha256` обоих архивов на ПК и на роутере; сравнить.
6. **Репетиция восстановления без изменений системы**: распаковать оба архива в
   `/tmp/restore-rehearsal/` и сравнить ключевые файлы (`diff` для
   `/etc/config/network`, `/etc/config/firewall`, `/etc/config/dhcp`, `/etc/mihomo/config.yaml`
   — если он в архиве). Только после успешной репетиции — установка.

Сетевой копией считается копия на ПК (через SSH/SMB). SMB-шара `geomask-backups` —
опционально: включать её только если владелец уже включил
`/etc/geomask/backup-share.enable`; сам маркер в прогоне не создавать.

## 5. Установка (канареечная сборка)

1. Собрать проверенный билд локально: `python build.py` → `python tools/build_module_index.py`
   → `python build.py` → `python tools/check_build.py --verify-only`; `sha256` записать.
2. `scp geomask.sh root@...:/tmp/geomask-canary.sh`; `ssh ... 'sh -n /tmp/geomask-canary.sh'`.
3. Read-only baseline: `ssh ... 'sh /tmp/geomask-canary.sh --check' | tee canary-check-before.txt`
   (ничего не меняет, никаких скачиваний).
4. Установка: `ssh ... 'sh /tmp/geomask-canary.sh --auto --yes --restart' | tee install.log`
   - `--auto` — все шаги без вопросов, стоп на FAIL;
   - `--yes` — без подтверждений;
   - `--restart` — разрешает рестарт mihomo/magitrickle (VPN рвётся ~3 c; это осознанно);
   - **не используется**: `--reinstall`, `--replace-config`, `--adopt-config`, `--modules`
     (сохраняем текущий выбор и конфиг владельца).
5. После установки: `--check` ещё раз (`canary-check-after.txt`), плюс `logread -l 200` и
   `/etc/geomask/backup-ok`.
6. Если установка вернула FAIL — **не откатывать сразу**: дать монитору 3 круга, затем откат
   по §7 (FAIL установки уже означает「сломанное состояние」, но откат подтверждается
   наблюдаемым здоровьем, а не только кодом возврата).

## 6. Монитор (ПК, PowerShell, раз в 60 с)

Параметры: `-Router root@192.168.0.1 -Key <path> -IntervalSec 60 -FailThreshold 3
-GraceSec 180 -BootWaitSec 120`.

Проверки каждого круга (все логируются):

| # | Проверка | Критичная | Как |
|---|---|---|---|
| 1 | Доступность роутера | да | `Test-Connection` 1 пакет, 3 c |
| 2 | SSH и probe | да | `ssh -o BatchMode=yes -o ConnectTimeout=5 ... 'sh /tmp/geomask-canary-probe.sh'` |
| 3 | Интернет с ПК через роутер | да | HTTP 204 `http://connectivitycheck.gstatic.com/generate_204`, 5 c |
| 4 | DNS через роутер | да | `Resolve-DnsName example.com -Server 192.168.0.1 -DnsOnly` |
| 5 | Mihomo жив | да | probe: `pidof mihomo` |
| 6 | TUN на месте | да | probe: `ip -o link show` содержит Mihomo/AI/Console (по факту выбранных модулей) |
| 7 | Таблицы 100/101/102 | да | probe: `ip rule` |
| 8 | nft-таблицы geomask | предупреждение | probe: `nft list tables` |
| 9 | Интернет с самого роутера | да | probe: `wget -q -T 5 -O /dev/null http://connectivitycheck.gstatic.com/generate_204` |
| 10 | Сервисы: dnsmasq, ai-doh/dnsmasq-ai, syncd | предупреждение | probe: initd running/listening |

Правила счёта:

- первые `GraceSec` после установки/перезагрузки фейлы не считаются (сервисы поднимаются);
- круг «здоров» — если все критичные проверки прошли; любой успешный круг сбрасывает счётчик;
- **3 круга подряд с хотя бы одной критичной ошибкой** → откат (§7);
- некритичные (8, 10) только пишутся в лог и не вызывают откат;
- SSH-ошибка сама по себе тоже критична, но при недоступном SSH откат невозможен — см. §7.4.

Формат лога (одна строка на событие, ASCII):

```
2026-11-19T04:20:00+03:00 INFO  round=12 ssh=ok ping=ok web=ok dns=ok mihomo=ok tun=ok rules=ok inet=ok
2026-11-19T04:21:00+03:00 WARN  round=13 dns=FAIL (resolver timeout) fails=1/3
```

## 7. Авто-откат

### 7.1 Условие
3 подряд критичных круга (или установка завершилась FAIL и монитор подтвердил нездоровье).

### 7.2 Шаги (одна SSH-сессия, каждый шаг логируется)
1. Сверить `sha256` локальных архивов с записанными при создании; при расхождении — стоп
   (не восстанавливать из повреждённой копии).
2. `scp` оба архива и `restore-set.txt` в `/tmp/canary-restore/`.
3. **Очистка того, что бэкап не вернёт**: удалить файлы в geomask-путях, которых нет в
   `restore-set.txt` и у которых mtime новее старта установки. Пути-кандидаты (только они):
   `/etc/geomask/*`, `/etc/mihomo/*` (кроме `config.yaml`, если он в архиве),
   `/etc/init.d/geomask*`, `/etc/init.d/ai-doh`, `/etc/init.d/dnsmasq-ai`,
   `/etc/hotplug.d/*geomask*`, `/usr/lib/geomask/*`, `/usr/bin/geomask*`,
   `/www/luci-static/resources/*geomask*`, `/etc/crontabs/root` (восстановится архивом),
   `/tmp/geomask*`. Файлы владельца вне этих путей не трогаются.
4. Распаковать: `tar -xzf canary-sysupgrade.tar.gz -C /` и
   `tar -xzf canary-overlay.tar.gz -C /`; `sync`.
5. `reboot` (как LuCI после restore).
6. Ждать `BootWaitSec` = 120 c: ping + SSH с повтором каждые 5 c; затем 5 минут обычного
   мониторинга.
7. Итог в лог и в `report.md`: `ROLLED_BACK` (здоров) / `ROLLBACK_FAILED` (нездоров после
   отката) / `ROLLBACK_IMPOSSIBLE` (SSH не поднялся).

### 7.3 Что НЕ откатывается этим механизмом
- Изменения в `/etc/config/*`, сделанные **после** бэкапа другими средствами (их не было).
- Пакеты, поставленные установщиком (`apk/opkg`) — установка в этом прогоне их не ставит.
- Повреждённый uboot/ядро/overlay вне файлов — здесь не воспроизводится.

### 7.4 Если SSH не отвечает
- 5 кругов подряд: писать `CRITICAL`, создать на ПК
  `Y:\openwrt\backup\canary-<ts>\MANUAL-ACTION-REQUIRED.txt` с точными шагами (LuCI →
  System → Backup → Restore, файл с ПК; при недоступности LuCI — failsafe/питание).
- Автоматический `reboot` в этом состоянии **не выполнять** (может превратить частичную
  недоступность в boot-loop).
- Мониторинг продолжать: если роутер вернётся, сразу откатить по §7.2.

## 8. Логи и evidence

Каталог: `Y:\openwrt\backup\canary-<YYYYMMDD-HHMMSS>\`
- `monitor.log` — все круги, решения, откат;
- `install.log`, `canary-check-before.txt`, `canary-check-after.txt`;
- `restore-set.txt`, `sysupgrade-list.txt`;
- `sha256.txt` (архивы, локально и на роутере);
- `report.md` — итог: SHA сборки, версия роутера, что проверено, вердикт, что осталось OPEN.

В репозиторий (после прогона) — `docs/stand/ROUTER-CANARY-EVIDENCE-<date>.md` со ссылками на
эти файлы и выводами; в `docs/superpowers/plans/2026-11-18-execution-log.md` — короткая запись.

## 9. Критерии приёмки прогона

- бэкап создан, проверен (`tar -tzf`, sha256, `sysupgrade -l`) и лежит на ПК;
- репетиция restore прошла до установки;
- установка `--auto --yes --restart` завершилась без FAIL, `--check` после — без новых
  ошибок;
- 10 кругов монитора подряд здоровы (≈10 мин), интернет/DNS/маршруты/TUN согласованы;
- либо сработал откат и роутер вернулся в здоровое состояние за ≤ 2 мин после `reboot`;
- логи полные, без пропусков кругов; в evidence нет «предположительно».

## 10. Запреты этого прогона

- не менять `/etc/config/network` вручную и не «чинить» сеть вручную во время мониторинга;
- не использовать `--reinstall`, `--replace-config`, `--adopt-config`, `--modules`;
- не включать SMB-шару бэкапов, если владелец её не включал;
- не публиковать релиз/тег по итогам прогона (публикация — отдельным решением);
- не оставлять на роутере `/tmp/canary-*` после успешного прогона (убрать в конце; при откате
  они исчезнут сами после reboot).

## 10а. Первый безопасный прогон (19.11.2026, без установки)

- Роутер: OpenWrt 25.12.5 (mediatek/filogic), overlay 185 МБ (свободно 76 МБ после уборки),
  установлен geomask 1.30.21, mihomo/hev/magitrickle/rpcd живы, `tun=AI,Mihomo,VPN-noP2P`,
  `rules=100,101`.
- Бэкап: `canary-sysupgrade.tar.gz` 24 524 281 байт (sha256 `5fccc62f…`), overlay-bundle
  17 494 683 байт (sha256 `4f6690ea…`), restore-set 862 пути; sha256 совпали на роутере и ПК;
  в архиве есть `etc/config/{network,firewall,dhcp}` и `etc/mihomo/config.yaml`.
- Репетиция восстановления: `SAME` для network/firewall/dhcp/**mihomo config.yaml** — то есть
  keep-list этого роутера включает `/etc/mihomo`, отдельный bundle для него не нужен (но
  остаётся для новых файлов `/usr/lib/geomask` и `/etc/geomask`).
- `--check` перед установкой: `pass=4 fail=11 skipped=4`. Часть FAIL — ожидаемая разница
  версий (1.30.21 не содержит новых `geomask-routes.sh`, `geomask-lock.sh`, `dnsguard`,
  `console.py`, `diag-*`, `mihomo-adopt`, `reload-state`). Неожиданное и не связанное с
  новой сборкой: `AI-DNS: mark rule for 0x64 missing` (при наличии ip-rule 0x64→101 и
  загруженной nft-таблицы) и предупреждение IPv6 (`ip6assign=0, ra=server`). Эти два пункта —
  кандидаты в план починки, если подтвердятся после установки.
- Найдены и исправлены два дефекта самой канарейки: бэкап писался в `/root` и вкладывал сам
  себя (теперь staging в `/tmp` + парковка прежних `canary-*`), и `paste(1)` на этом OpenWrt
  отсутствует, из-за чего probe показывал `tun=none rules=none` (теперь `ls /sys/class/net`
  и `grep -o 'lookup 10[0-9]'`).

## 11. Чек-лист команд (сокращённо)

```powershell
# 0) сборка и хеш
cd Y:\openwrt\geomask
python build.py; python tools\build_module_index.py; python build.py
python tools\check_build.py --verify-only
(Get-FileHash geomask.sh -Algorithm SHA256).Hash

# 1) бэкап
ssh -i $env:USERPROFILE\.ssh\openwrt_final root@192.168.0.1 "sysupgrade -b /tmp/canary-sysupgrade.tar.gz; sysupgrade -l > /tmp/sysupgrade-list.txt; sha256sum /tmp/canary-*.tar.gz"
scp -i $env:USERPROFILE\.ssh\openwrt_final root@192.168.0.1:/tmp/canary-sysupgrade.tar.gz Y:\openwrt\backup\canary-<ts>\
powershell -File Y:\openwrt\geomask\fetch_backup.ps1 -DstDir Y:\openwrt\backup\canary-<ts>

# 2) установка
scp geomask.sh root@192.168.0.1:/tmp/geomask-canary.sh
ssh ... "sh -n /tmp/geomask-canary.sh && sh /tmp/geomask-canary.sh --check" | Tee-Object canary-check-before.txt
ssh ... "sh /tmp/geomask-canary.sh --auto --yes --restart" | Tee-Object install.log

# 3) монитор с авто-откатом (реализуется как tools/canary/run-canary.ps1)
```

Скрипты уже в репозитории: `tools/canary/run-canary.ps1` (оркестратор + сторож на ПК),
`tools/canary/probe.sh` (read-only JSON-проверки на роутере), `tools/canary/restore.sh`
(восстановление архивов и очистка лишнего на роутере). Покрытие — `tests/test_canary_scripts.py`
(ASCII, `sh -n`, контракт ключей probe, обязательный `StrictHostKeyChecking=yes`, установка
только с `-Execute`, откат только после `-FailThreshold` кругов).

Безопасный прогон без установки (только бэкап + репетиция восстановления):

```powershell
powershell -File Y:\openwrt\geomask\tools\canary\run-canary.ps1
```

Полный прогон (установка + монитор + авто-откат) — только когда владелец рядом:

```powershell
powershell -File Y:\openwrt\geomask\tools\canary\run-canary.ps1 -Execute
```
