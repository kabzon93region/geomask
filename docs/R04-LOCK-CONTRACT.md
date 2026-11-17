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
  `maintenance(10) → console(20) → cron(25) → config-tx(30) → routes-worker(35) →
  route-<table>(40) → sync(45) → devices(60) → subs(65)`.
* `routes-worker(35)` — single-flight очередь route-событий (R05). Worker берёт его первым и
  только потом `route-<table>(40)`: два worker'а не могут одновременно бороться за одну
  таблицу. Обратный порядок запрещён: писатель таблицы не берёт worker-lock (тест рангов).
* Очередь: очередь живёт в `GM_ROUTE_STATE_DIR` (default `/tmp/geomask-routes`), файлы
  `pending-<t>` / `work-<t>` / `error-<t>` / `parked-<t>`. `gm_route_enqueue <t>` пишет
  `pending-<t>` через temp+`mv` **до** любого spawn; worker атомарно `mv pending→work`,
  удаляет `work` только после успеха. Новый `pending`, появившийся за время sync, даёт ещё
  один проход (generation recheck).
* rc-контракт `geomask_routes_sync`: `0` ok, `75` busy (route-lock занят), `76` retryable read
  до мутации, `77` неудачная мутация (`replace`/`delete`/`cp`, а также чужой occupant
  префикса), `2` нет lock-примитива, `4` инверсия рангов. `1` больше не используется:
  вызывающий не мог отличить «повторить» от «отказать».
* Worker: `GM_ROUTE_LOCK_WAIT=2` на каждую попытку (родитель мог экспортировать 0), только
  `75/76` ретраятся — ≤5 попыток, backoff `1/2/4/8`, ≤3 активации, общий cap ~30 с. `77/2/4`
  не ретраятся: `77` — ownership-конфликт, его разбирает R12; событие уходит в `parked-<t>` +
  `error-<t>` (ts/rc/attempts) и ждёт **нового** события. Перед стартом worker вызывает
  `gm_lock_close_inherited`, иначе унаследованный fd держал бы console/updater-lock.
* ifup-hotplug (`99-default-vpn-route`) больше не берёт route-lock и не запускает `ip`/`uci`:
  он только `gm_route_enqueue` + detached worker. Контракт выхода: `0` — событие принято в
  очередь (не «применено»), `1` — pending не записан, `2` — нет lock-примитива. Ожидание TUN
  и все ретраи — внутри worker'а, ограниченно.
* Drain: `gm_route_drain` (inline, без spawn) вызывается в guard/cron (вне console-lock) и в
  установщике под `maintenance`; установщик по-прежнему читает `error-100`. Drain делает
  ровно одну попытку НА ТАБЛИЦУ ЗА ВЫЗОВ, но обходит ВСЕ pending-таблицы (100/101/102 и
  новое поколение, появившееся во время sync): busy/readable-rc возвращает событие в
  `pending-*` (не паркует), `77/2/4` паркует только эту таблицу, остальные продолжают
  обрабатываться. Поэтому cron-прогон не «останавливает автоматику» из-за временно занятой
  таблицы и одна проблемная таблица не блокирует другие.
* `rc=2` helper'а с существующим backend (не создать/не открыть lock-dir: ENOSPC/EACCES)
  трактуется как retryable `76`; только отсутствие самого примитива остаётся терминальным
  `2` (fail closed / park). Внешний набор кодов не меняется.
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
  До этого блока guard вызывает `gm_route_drain` вне console-lock: очередь route-событий
  разбирается cron-прогоном, а не ожиданием внутри чужой секции.
* Console/rpc (`geo-backend.sh`): `_console_rule_on` использует короткий бюджет
  `GM_CONSOLE_ROUTE_WAIT=2` (а не 20 с под console-lock), busy/read-rc возвращается как
  `"retryable":true`; `_ai_routes_ensure` не ставит `default dev AI`, если таблица 101 не
  синхронизирована, и `_apply_routing` не добавляет правило в такую таблицу.
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
| таблицы 100/101/102 (`owned-*`) | `route-<n>` (пишет worker), hotplug только очередь | `geomask-routes.sh`, `99-default-vpn-route` |
| очередь route-событий (`pending/work/error/parked`) | `routes-worker` (single flight) | `geomask-routes.sh`, hotplug-spawn, guard/cron drain, установщик |
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
* R05: `77` (неудачная мутация) намеренно **не** ретраится. Ownership/reconcile маршрутов
  (повторный `replace` после чужого occupant'а, восстановление `owned`) — R12; очередь
  паркует событие и ждёт нового ifup.
* Очередь не демон: `pending-*` старше последнего drain не чистится по возрасту, а drain
  запускается только guard/cron (каждые 5 мин) и установщиком. Если TUN так и не поднялся,
  событие остаётся `pending-*` (deferred, не parked) и будет применено при первом успешном
  подъёме/дренаже. Отдельного таймера/expiry нет — это осознанно: потеря ifup-события и была
  дефектом R05.
* Fixed-temp writers (P2/P3, не блокеры): `geomask-ytwatch.sh:190` (state),
  `geomask-vpnwatch.sh:109` (cursor), `/tmp/geomask-ytwatch.conf.new`,
  `geomask-audit-scheduler.py` (`schedule-runs.json.tmp`).
* `save_stream` установщика всё ещё использует `<target>.zmnew`; это безопасно только потому,
  что установщик single-instance (`maintenance`). Per-PID temp — P3.
* `cp -p`-бэкапы `config.yaml.bak_geomask_*` не прибираются по возрасту — P3, вне R04.
* Старый pid-updater (`/tmp/geomask-upd/pid`) невидим новому `updater`-lock: одно пересекающееся
  обновление сразу после апгрейда возможно. После первого запуска новой версии — нет.
* OS/стенд: семантика `flock(2)`, release после SIGKILL, `/proc`, fd-inheritance, BusyBox
  applet, overlay/tmpfs, procd/netifd, реальные `ip`/netns — **Linux-only**, на Windows не
  проверяются; зелёный Windows-прогон не является доказательством Linux-рантайма.
* Windows-тесты R04/R05 проверяют логику/статику: отказ второго writer'а при no-wait, бюджет
  ожидания, запрет обратного порядка/повтора, fail-closed при отсутствии примитива,
  очередь/rc/retry/park worker'а, top-entrypoint hotplug (<1 с, pending, ноль `ip`), drain после
  release, отсутствие reclaim-кода и совпадение путей tx/node-scan. kernel-тесты (flock,
  наследование fd, release после SIGKILL) помечены skip.
