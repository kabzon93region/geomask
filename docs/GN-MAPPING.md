# G/N mapping — старые находки против текущего дерева

Сверка прежних ID с HEAD (R10, пункт 6 плана независимой перепроверки). Это не отчёт о
готовности: каждая строка называет **текущий** факт в коде, а не прежнее утверждение.

- `G01–G06` — [AUDIT-2026-11-14-R2-PS5-and-geomask.md §5](../../docs/AUDIT-2026-11-14-R2-PS5-and-geomask.md#L52-L88);
- `N00–N12` — [PLAN-2026-11-14-postfix-v1.30.23.md](../../docs/PLAN-2026-11-14-postfix-v1.30.23.md#L28-L115).

## Статусы

| Статус | Смысл |
|---|---|
| `FIXED` | дефект закрыт в текущем дереве; в доказательстве обязательна ссылка на `tests/`, `docs/` или коммит-хеш. |
| `OPEN` | дефект воспроизводится по коду; в статусе назван владелец (R-задача). Если отдельной R-задачи на исправление нет, назван gate `R11`, который это отслеживает, и это сказано явно. |
| `OUT-OF-SCOPE` | вне границ geomask/R-плана; указано, почему. |

Правило `FIXED` проверяется статически: [tests/test_gn_mapping.py](../tests/test_gn_mapping.py).

## Таблица

| ID | Текущий факт (file:line) | Статус | Доказательство |
|---|---|---|---|
| G01 | `assets/geomask-dnscheck.sh:55-63` — rc `nslookup` проверяется, ответ берётся только после `Name:`/BusyBox `Address N:`, серверный `Address:` не считается ответом; `:74-93` — LAN из UCI + `ip -4 addr`, `--quick` без сетевых вызовов, нет nslookup/python/config → `UNKNOWN` rc 2 | `FIXED` | R10a, коммит `b061698`; `tests/test_dnscheck_contract.py` (11 тестов, RED на старом ассете) |
| G02 | `assets/geomask-doctor.py:539-548` — для `AND,`/`OR,` по-прежнему `any(hit[0])`, т.е. AND ведёт себя как OR; `:505-521` `match_condition` не моделирует network/порты/source и возвращает `False` без пометки «контекст неизвестен» | `OPEN (R11 gate; план console-network-reliability A1–A4)` | поведенческого теста нет; факт читается прямо в `doctor:539-548` |
| G03 | `assets/geo-backend.sh:901-909` — `SEC=$(uci add dhcp host)`, затем `uci set "$SEC.mac=..."` без префикса `dhcp.` и без проверки rc сеттеров; при успешном `uci commit` секция попадает в `_dhcp_owned_add` | `OPEN (R11 gate; план console-network-reliability A1–A4)` | тест с пустым MAC есть только для раннего выхода (`tests/test_backend_safety.py:189`); реальный UCI-путь не покрыт |
| G04 | `assets/geo-backend.sh:641-657` — запись исходных значений один раз, валидный документ не перезаписывается; `:659-670` — restore всех четырёх опций; `:781-797` — `on` восстанавливает и удаляет запись; повторный `off` не теряет `ip6assign` | `FIXED` | `tests/test_backend_safety.py:317` `test_ipv6_off_then_on_restores_every_layer`; R03 добавил per-interface IPv6 (`tests/test_reinstall_phase2b.py`) |
| G05 | `assets/geomask-doctor.py:701-707` — один `pidof hev-socks5-tunnel` на все инстансы; `:890-906` `check_path` судит по имени узла и числу узлов; `:1108-1116` `check_path_egress` ухудшает state только для выхода «по умолчанию», мёртвый fixed listener остаётся `ok` | `OPEN (R11 gate; план console-network-reliability A1–A4)` | runtime-теста нет; `check_path_egress` покрыт только статикой doctor-тестов |
| G06 | guard-часть закрыта: `assets/geomask-guard.sh:267-284` требует ровно `default dev AI`, `:193-196` LAN из UCI, `:241-260` rc каждой мутации; но `assets/geomask-routes.sh:628-629` по-прежнему считает `pref 32760` достаточным без сверки `iif`/`table`, а `assets/geomask-guard.sh:441-448` сбрасывает conntrack **всех** ai/bypass-устройств, не только затронутого. Hotplug больше не пишет `ip`/`uci` сам: `assets/99-default-vpn-route:33-77` кладёт событие в очередь | `OPEN (R05/R12 residual)` | закрытые части: R10a `tests/test_guard_entrypoint.py`, R05 `tests/test_route_queue.py`, R12 `tests/test_routes_ownership.py`; на два остатка тестов нет |
| N00 | `geomask.sh.in:2792-2820` — `apply_ipv6_default` выключает IPv6 по умолчанию, `:5340-5342` — и при переустановке; `--ipv6=keep` — единственный выход; три слоя в `assets/geo-backend.sh:641-799` | `FIXED` | коммит `c0f3731`; `tests/test_install_safety.py:207-236` (off/keep/refused) |
| N01 | `assets/geomask-routes.sh:152-195` — синхронизация под kernel-flock `geomask-lock.sh` (ранг `route-<table>` 40), без mkdir/PID-эвристик; два писателя не теряют owned-список | `FIXED` | R04, коммит `71d7bde`; `tests/test_lock_protocol.py`, `tests/test_routes_ownership.py` |
| N02 | `assets/geomask-routes.sh:702-760` — bounded retry только 75/76, `parked-<t>`/`retry-<t>`; `:863-890` drain превращает `retry` в `pending`; `assets/99-default-vpn-route:47-76` — очередь пишется до spawn, событие не теряется при занятом lock | `FIXED` | R05, коммит `6ad1f15`; `tests/test_route_queue.py` (включая поздний TUN и release-then-drain) |
| N03 | `assets/geo-backend.sh:1095-1099` — не менять ruleset только когда не читается LAN-адрес; `:1140-1145` — неизвестный (но синтаксически возможный) адрес alias всё ещё даёт `reject` и `state=error`, т.е. «alias временно не поднят» и «адрес неверен» не различаются | `OPEN (R11 gate; aidns mapping)` | тест «alias пропал и вернулся» отсутствует; `_aidns_sync` покрыт только косвенно |
| N04 | `geomask.sh.in:1121-1134` — `step3_apply` при непустом конфиге и отсутствии PyYAML всё ещё классифицирует свой конфиг как чужой и советует `--adopt-config`/`--replace-config`; прямой текст «установите python3-yaml» есть только в reinstall-пути `:5802-5805`. Маркер `# geomask-managed: 1` (`:304`, `:1320`) спасает конфиги, записанные текущим шаблоном, но не принятый/старый конфиг | `OPEN (R11 gate; одиночный --step 3)` | `tests/test_reinstall_preflight.py:196` доказывает только reinstall; пункт N04 в плане не отмечен |
| N05 | `assets/geo-backend.sh:1357-1361` — `_mask_url` вырезает userinfo и оставляет только схему+хост+`…`; журнал/ответы панели не содержат `user:pass@` и токенов | `FIXED` | `tests/test_backend_safety.py:446` (`test_a_subscription_url_never_shows_its_credentials`), `:453` (plain URL) |
| N06 | `assets/geo-backend.sh:74-92` — маркер `/etc/geomask/backup-ok` пишется только после проверки архива; `:103-128` — `cmd_backup` создаёт архив из панели; RPC `backup` (`tools/geo-rpc.json:519-523`) | `FIXED` | `tests/test_backup_gate.py:38` (fresh), `:53` (expired), `:103` (full cycle); RPC-манифест `gen-geo-rpc --check` |
| N07 | `assets/geo-backend.sh:1833-1838` — без digest релиза установщик не запускается; подписанного манифеста/закреплённого ключа нет, старые релизы без digest обновить нельзя | `OPEN (R11 release gate)` | подписи в дереве нет: `git grep -i "signature"` и `-i "gpg"` пусты; N07 в плане не отмечен |
| N08 | `geomask.sh.in:3400` — `_rs_present` дописывает `$1/present`, `:4020` — `_reinstall_snapshot` создаёт файл; ни `_reinstall_restore`, ни verify его не читают (мёртвое состояние) | `OPEN (R03 residual)` | `git grep -n "/present"` находит только запись; R03-фазы 2b–2f его не используют |
| N09 | `assets/geo-backend.sh:1082` — путь `/tmp/geomask-aidns.state`; `:1131`/`:1141` пишут `ok`/`error`; `:825-826` читает его только `cmd_aidns_apply`; `cmd_status` (`:479-495`) по-прежнему пересчитывает состояние из live nft/netstat, файл не использует | `OPEN (R11 gate; частично)` | `tests/test_status_contract.py` проверяет live-пересчёт, а не повторное использование state-файла |
| N10 | `tests/bin/ip:4-10` — управляемые таблицы стали stateful через `GM_STATE`; `:17-20` — `route show table main` всё ещё отвечает одним и тем же connected-маршрутом во всех сценариях | `OPEN (R06)` | R12-тесты используют stateful-режим (`tests/test_routes_ownership.py`), но сценария «main пуст» нет |
| N11 | `assets/geomask-routes.sh:296-310` — единый ключ `prefix / via / dev / src`, декорации `proto/metric/expires/linkdown` игнорируются; `via`/`src`/нормализованная установленная форма покрыты тестами | `FIXED` | R12, коммит `b061698`; `tests/test_routes_ownership.py:737` (decoration), `:801` (foreign src). Остаток: multipath не проверялся — при нестандартной строке `replace` вернёт 77 (fail-closed), это задокументированная граница `docs/ROUTING.md` |
| N12 | `geomask.sh.in:743-746` — шаг 1 только предупреждает о низком `/root`/`/tmp`; `:1662` `backup_gate` и `:2889` требуют бэкап до записи; явной последовательности «освободите место → повторите» нет ни в панели, ни в `docs/OPERATIONS.md` | `OPEN (R11 release gate)` | `grep -n "освобод" docs assets` и `grep -n "свободн" docs assets` не находят инструкции; N12 в плане не отмечен |

## D14–D18 (вне таблицы G/N)

Это findings разбора `audit-temp/r10-analysis-20261117`, не отдельные ID; строки таблицы на них
не опираются.

- **D14 — периодический dnsguard/dnscheck.** Cron **не добавлялся**: это политика владельца
  (перезапуск dnsmasq/смена upstream и шум в syslog), а не диагностический фикс R10. Ни одна
  строка выше не помечена `FIXED` на основании периодического прогона: G01 закрыт разовым
  контрактным тестом. Если opt-in entrypoint понадобится, его форма — отдельное решение
  владельца (единый timer с `geomask-lock.sh`, ограниченный лог, без автоперезапуска
  сервисов); в R10b-2 не реализовано.
- **D15 — `aidns` not-installed отвечал rc 0.** Закрыто R10b-1: `assets/geo-backend.sh:815-820`
  (`state=not_installed`, rc 1), `tests/test_status_contract.py`.
- **D16 — Doctor `run` всегда exit 0.** Закрыто R10b-1: `assets/geomask-doctor.py:1804-1816`
  (fail → rc 1), `tests/test_diagnostics.py`.
- **D17 — `console_status` по подстрокам.** Закрыто R10b-1: `assets/geo-backend.sh:2394-2460`
  (точное правило/default/LAN/TUN, `state=ok|drift|down|unknown`), коммит `021176d`,
  `tests/test_console_status_contract.py` (13).
- **D18 — init-логи.** Закрыто этой задачей: `assets/geomask-syncd.init` (нет скрипта → syslog +
  rc 1), `assets/geomask-syncd.sh` (rc `inotifywait` + backoff), `assets/dnsmasq-ai.init`
  (нет конфига → syslog + rc 1, `file`, `stderr 1`), `assets/ai-doh.init` (проверка AI TUN и
  `127.0.0.1:7891`, `stderr 1`); контракт — `tests/test_initd_contract.py`, bootstrap-хардкод
  описан в [AIDNS.md](AIDNS.md#bootstrap-значения-осознанный-хардкод).

## Что этот документ не доказывает

Статусы `FIXED` опираются на локальные тесты/код, а не на Linux-рантайм: реальные UCI, procd,
netifd, fw4/nft, BusyBox `ip`, kernel flock и роутер не проверялись (Windows-прогон). Это та же
граница, что в `tests/BOUNDARIES.md` и `docs/ROUTING.md`.
