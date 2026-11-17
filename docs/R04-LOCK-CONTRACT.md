# R04 — контракт блокировок geomask

Один примитив: **kernel `flock(2)` на постоянном inode**. Реализация — `assets/geomask-lock.sh`
(устанавливается в `/usr/lib/geomask/geomask-lock.sh`, 644). Тот же код инжектируется в
`geomask.sh` (build.py), поэтому установщик блокируется той же реализацией, которую ставит.
Python использует `fcntl.flock` на **тех же файлах** (это тот же syscall).

## Инварианты

* Один файл блокировки на имя; файл создаётся при первом использовании и **никогда не
  удаляется и не пересоздаётся** во время работы. Нет PID, нет owner-файла, нет stale-reclaim,
  нет `rm -rf`.
* Владелец убит (включая `SIGKILL`) — ядро снимает lock при закрытии последнего fd/open file
  description. Ownerless-окна нет: до взятия lock ничего не создаётся. Unlock-after-takeover
  невозможен: файл не удаляется, значит нет второго inode.
* Ожидание ограничено: `gm_lock_acquire <name> [секунды] [путь]`. `0` — одна неблокирующая
  попытка. BusyBox `flock` не имеет `-w`, поэтому ожидание = `flock -n` + `sleep 1` до бюджета.
* Фиксированный порядок захвата (rank), проверяется в рантайме: захват lock с rank ≤ уже
  удерживаемого max завершается `rc=4`, повторный захват того же имени — `rc=3`. Порядок:
  `maintenance(10) → console(20) → cron(25) → config-tx(30) → route-<table>(40) → sync(45)
  → devices(60) → subs(65)`.
* Нет `flock` на Linux → `rc=2`, writer обязан отказать (fail-closed). mkdir-fallback удалён.
  Non-Linux (Windows-host тестов) — документированный portable-backend (O_EXCL), на роутере
  не выбирается; `SIGKILL`-release и inode-идентичность там не проверяются.
* Нечисловой бюджет ожидания — отказ `rc=2` (не «ждать вечно»); значение клампится до 3600 с.
* Каноническое имя пути tx-lock — `GM_CONFIG_TX_LOCK` (shell helper, tx, node-scan, torrent);
  `GM_LOCK_TX_FILE` — legacy-алиас только для инжектированных тестовых root'ов. В установщике
  путь вычисляется через `P` (`$(P /etc/geomask/node-audit/state/.config-tx.lock)`) и ДО
  инжекта helper'а, поэтому тестовый `GM_PREFIX` не открывает хостовый `/etc`.
* Установщик (и интерактивный мастер) отказывается применять шаги 2–7, если шаг 1 не прошёл
  обязательную проверку lock-примитива (`GM_STEP1_FATAL` в `step1_check`/`run_step`).
* Критическая секция короткая; внешние команды под lock — только короткоживущие (`ip`, `uci`,
  `cp`, `mktemp`). Команды, которые могут оставить демона (init-скрипты, сервисы), запускаются
  через `gm_lock_safe_exec`: в ребёнке закрываются зарезервированные fd (5–9), иначе демон
  унаследует descriptor и будет держать lock до перезагрузки. `gm_lock_close_inherited`
  вызывается установщиком на старте, чтобы не унаследовать `updater`-lock от `run.sh`.
* Установщик пишет `config.yaml` только под `config-tx` (обёртка `write_mihomo_config` вокруг
  `_write_mihomo_config_unlocked`, ранг 30 > maintenance 10), временные имена — per-PID;
  остатки прибирает `REINSTALL_TEMP_GLOBS` в `_rs_prune_temps`.
* Guard берёт `console` и делает route-sync **no-wait** (`GM_ROUTE_LOCK_WAIT=0` в subshell):
  занятая таблица 102 пропускается с WARN до следующего прогона, `console off` не ждёт 20 с.
* Scanner держит ОДИН бэкап на вид (`config.yaml.bak_nodescan`, `...bak_aiguard`), заменяемый
  атомарно, а не файл на каждый скан; ошибка записи бэкапа (ENOSPC/замена) логируется
  `WARN ... NOT saved`, и скан **продолжается без бэкапа** — осознанный выбор: бэкап
  диагностический, отказ скана из-за него оставил бы узлы непроверенными. Prune в откате не
  использует общий `.tmp.*`, чтобы не удалить живой temp другого писателя.
* `_mihomo_restore_preimage` возвращает preimage через `cp -p` (не temp+rename): запись
  неатомарна, читатель в этот момент может увидеть частичный файл. Это принятый компромисс:
  копирование идёт под `config-tx`, поэтому конкурирующий tx-писатель не войдёт, а mihomo
  перезапускается только после успешного `cp`/release; цена — короткое окно для читателей, не
  берущих lock (mihomo -t/CLI).
* В установщике `GM_CONFIG_TX_LOCK` и `GM_LOCK_DIR` вычисляются через `P` (GM_PREFIX) и
  экспортируются до инжекта helper'а: тестовый root не открывает хостовые `/etc` и `/tmp`,
  а дочерние writer'ы (например `geomask-lane.sh` из uninstall) наследуют те же пути.

## Что сериализовано

| Ресурс | Lock | Где |
|---|---|---|
| таблицы 100/101/102 (`owned-*`) | `route-<n>`, hotplug — no-wait | `geomask-routes.sh`, `99-default-vpn-route` |
| console on/off ↔ guard | `console` (guard — no-wait) | `geo-backend.sh`, `geomask-guard.sh` |
| `/etc/mihomo/config.yaml` (транзакция) | `config-tx` = `/etc/geomask/node-audit/state/.config-tx.lock` | `geomask-config-tx.py` |
| node-scan → config.yaml | тот же `config-tx` (дайджест-CAS внутри lock) | `geomask-node-scan.sh` |
| torrent/lane direct write | тот же `config-tx` + CAS по прочитанным байтам | `geomask-torrent.sh`, `geomask-lane.sh` |
| `/etc/crontabs/root`, `geomask-nodes.conf` | `cron` | `cmd_nodes_set`, `cmd_sync_set`, установщик, uninstall, restore |
| `/etc/geo-devices.list`, dhcp-hosts | `devices` (+ per-PID temp) | `cmd_dev_add/del`, `_dhcp_owned_add` |
| `/etc/geomask-subs.list` | `subs` (+ per-PID temp) | `cmd_sub_add/del` |
| self-update runner | `updater` (fd наследуется detached child) | `cmd_upd_apply` |
| переустановка/установка | `maintenance` (try-lock) | `menu_reinstall` |
| откат байтовых объектов (включая `config.yaml`) | `config-tx`: запись и `_rs_verify_files` в одной секции | `_rs_restore_files` → `_rs_restore_files_unlocked` |
| `/etc/mihomo/config.yaml` при установке (step3) | `config-tx` | `write_mihomo_config` |
| preimage-откат mihomo при неудачном restart | `config-tx` | `_mihomo_restore_preimage` в `safe_mihomo_restart` |
| ruleset/zapret/state temp | per-PID имена (без общего lock) | `sync_mtrickle_mihomo.sh` |

CAS транзакции: `start_transaction(..., expect_sha256=...)` сравнивает live с базой
**внутри** `tx_lock`, после recovery и до первой мутации; несовпадение = `STALE`, а не
молчаливая перезапись. Recovery перенесён внутрь lock (`R05`).

Откат берёт `config-tx` до первого `cp`, пишет байтовые объекты и проверяет их под тем же
lock; занятый lock — явный отказ (запись без сериализации не выполняется). Поздний
`_rs_restore_verify` идёт без lock и при расхождении сообщает о concurrent writer, а не
«verified».

**Принятый компромисс: один грубый config-tx на весь набор byte-объектов отката.** Сужение
до «только `config.yaml`» невозможно без отдельного per-object lock, который делили бы те же
писатели: `geomask-config-tx.py` владеет только `config.yaml`, остальные byte-объекты пишет
установщик (и только он), поэтому грубый lock ничего лишнего не сериализует. Если живой tx
держит lock дольше 30 с, откат падает **после** `_rs_stop_new_services` — громко
(`rollback incomplete`, снимок сохранён), но это mixed state по сервисам; альтернатива
(восстанавливать не-tx объекты до захвата) дала бы ещё более смешанное состояние. Выбран
явный отказ.

**Вне scope config-tx:** `_rs_restore_uci` (uci-ключи), `_rs_restore_shared` (dnsai/zapret),
`_rs_restore_sysctl`, `_rs_restore_links` (rc.d) — транзакция эти объекты не пишет, поэтому
config-tx их не защищает; другие писатели общего lock на них не берут. Брать там config-tx
значило бы имитировать сериализацию. Их конкурентные правки, как и раньше, ловятся
conflict/verify-проверками отката.

## Что осталось / границы

* `ip rule`/nft (R14) — несколько писателей без общего lock: вне объёма R04.
* Fixed-temp writers (P2/P3, не блокеры): `geomask-ytwatch.sh:190` (state),
  `geomask-vpnwatch.sh:109` (cursor), `/tmp/geomask-ytwatch.conf.new`,
  `geomask-audit-scheduler.py` (`schedule-runs.json.tmp`).
* `save_stream` установщика всё ещё использует `<target>.zmnew`; это безопасно только потому,
  что установщик single-instance (`maintenance`). Per-PID temp — P3.
* `cp -p`-бэкапы `config.yaml.bak_geomask_*` не прибираются по возрасту — P3, вне R04.
* Старый pid-updater (`/tmp/geomask-upd/pid`) невидим новому `updater`-lock: одно пересекающееся
  обновление сразу после апгрейда возможно. После первого запуска новой версии — нет.
* OS/стенд: семантика `flock(2)`, release после SIGKILL, `/proc`, fd-inheritance, BusyBox
  applet, overlay/tmpfs, procd/netifd — **Linux-only**, на Windows не проверяются; зелёный
  Windows-прогон не является доказательством Linux-рантайма.
* Windows-тесты R04 проверяют логику/статику: отказ второго writer'а при no-wait, бюджет
  ожидания, запрет обратного порядка/повтора, fail-closed при отсутствии примитива,
  no-wait hotplug, отсутствие reclaim-кода и совпадение путей tx/node-scan. kernel-тесты
  помечены skip.
