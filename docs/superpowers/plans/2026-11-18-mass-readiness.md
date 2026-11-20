# geomask: план исправлений перед массовым использованием — Implementation Plan

> **Для исполнителя:** это результат аудита, не разрешение начинать исправления или деплой. Выполнять задачи по одной: RED → GREEN, независимое review, отдельный commit. Все repair/acceptance-пункты остаются OPEN.

**Goal:** устранить дефекты существующих установки, обновления, отката и сетевых функций и доказать работоспособность полной и частичной установки.

**Architecture:** сохранить существующие модули/RPC, закрыть обходы lock/CAS, различать сохранённые bytes и применённый runtime. Отказ/неизвестность не считать успехом. Новые продуктовые функции не входят в объём.

**Tech Stack:** OpenWrt, POSIX sh/BusyBox ash, Python/PyYAML, LuCI/rpcd/ubus, UCI/procd/netifd, fw4/nftables, ip rules/routes, mihomo/hev/MagiTrickle.

**Spec:** [требования задачи](<#требования-и-ограничения>), [предыдущая перепроверка](<../../RECHECK-2026-11-17.md>), [границы тестов](<../../../tests/BOUNDARIES.md>).

## Вердикт и базовая ревизия

**НЕ ГОТОВО к массовому использованию или установке на единственный рабочий роутер.** Это не только отсутствие E2E: получены контрпримеры потери конкурентных изменений, ложного успеха сетевых операций и несогласованной минимальной установки. Нельзя обещать успешную полную/любую частичную установку и безопасный откат.

- Дата: 18.11.2026, Europe/Moscow. Проверенный HEAD: `c2e7dffedd227df8d55602d31d0a76751befcd11` (docs-only); продуктовый baseline: `54a504a03e31aa4c96e66f8c02a9e0b356f0f66a`.
- Документация и предшествующая локальная история опубликованы первым push, без тега/релиза/деплоя. Этот план публикуется отдельным docs-only коммитом.
- Сборка `--verify-only` согласована, префикс SHA-256 `89f7789b1fe28a50`; RPC parity: **67 методов, read24 / write43**. Это не проверка авторизации настоящего rpcd.
- Запущен только заранее просмотренный [updater test module](<../../../tests/test_module_update.py>): **47 tests, OK, 0 skipped**, exit0. Зелёный результат не покрывает новые контрпримеры.
- Исторические `497 OK` включали12 skipped; другой spot-check `197 OK` включал8 skipped. Ни один не является полным выполненным suite этого аудита.
- [CI проверенного HEAD](<https://github.com/kabzon93region/geomask/actions/runs/44190360945>) завершился **failure 18.11.2026 01:45 МСК**: unit tests failure, shell-сценарии skipped; build/RPC/syntax/compile success. API загрузки job log вернул HTTP403 (admin rights required), поэтому конкретный failed test/timeout/root cause пока **не установлены**. Не считать это доказательством определённого продуктового дефекта или допустимым зелёным gate. Ubuntu CI также не заменяет OpenWrt E2E.

## Требования и ограничения

- Аудит существующего функционала/конфликтов, подтверждённые и потенциальные проблемы в план. Продукт сейчас не исправлять.
- **Не обращаться к роутерам вообще:** ни SSH/SCP/LuCI/ubus/API, ни диагностика, ни очистка старых файлов/сессий. Не читать/писать сетевое зеркало Z.
- Меняются только документы. Полный installer/uninstall/release publisher и полная discovery-тестов на хосте запрещены.
- Системные suites/failure injection — будущий изолированный Linux/OpenWrt-стенд без маршрута к рабочей LAN.
- Перед будущими разрешёнными изменениями устройства: полный backup, локальная копия и проверенное восстановление; после restart health-check и rollback при отказе.
- Не добавлять новые jobs/автоматические политики/периодический DNS-repair как «bugfix».
- Это **overlay над базовым стеком**, не установка factory OpenWrt с нуля: «чистый root/image» ниже означает отсутствие geomask при заранее подготовленных mihomo/MagiTrickle и необходимом hev stack. Проверить отрицательные случаи отсутствующих prerequisites до mutation.

## Ранний этап 0 — небольшие исходники для разработки через ИИ

**Добавлено по запросу владельца 18.11.2026; разбиение выполнено 19–20.11.2026 (партии 9–13 в [журнале исполнения](2026-11-18-execution-log.md)), статус — [PLAN-STATUS](../source-split/PLAN-STATUS.md).** Цель — читать и изменять одну небольшую область, не загружая каждый раз монолит. Это разрешённый рефакторинг структуры исходников, не новая функция роутера.

### 0.1. Что действительно большое

Срез `c15deb7`, tracked implementation files, без tests/fixtures/docs/pycache. Строки физические, включая комментарии/пустые; порог здесь **≥500**, это индикатор для review, не запрет любой ценой. Найдено **16 исходников** и один конечный generated installer:

| Файл | Строк | Решение |
|---|---:|---|
| [Установщик](<../../../geomask.sh>) | 45 180 | Уже generated. Не дробить/редактировать вручную и не подавать целиком в контекст ИИ. Сохранить единый download/install artifact. |
| [Шаблон установщика](<../../../geomask.sh.in>) | 5 997 | Первая очередь: небольшие installer source-parts, включая отдельные snapshot/restore/verify области. |
| [Backend](<../../../assets/geo-backend.sh>) | 3 565 | Первая очередь: handlers/helpers по ответственности; единый собранный backend/dispatch. |
| [LuCI](<../../../assets/geo-sync-page.js>) | 1 930 | Следом: RPC/shared/view lifecycle и ordered render-parts без смены JS scope. |
| [Doctor](<../../../assets/geomask-doctor.py>) | 1 844 | При задаче5: parsers/rules/checks/domain/live/CLI. |
| [Spool](<../../../assets/geomask-diag-spool.py>) | 1 014 | При задаче7: identity/storage/quota/archive/retrieval/CLI; не разрывать security boundary. |
| [Config TX](<../../../assets/geomask-config-tx.py>) | 943 | При задаче3: lock/preimage/trial/commit/rollback/CLI, один owner транзакции. |
| [Node audit](<../../../assets/geomask-node-audit.py>) | 931 | Отдельный следующий проход: provider parsing/probes/jobs/report/CLI. |
| [Routes](<../../../assets/geomask-routes.sh>) | 897 | При задаче4: discovery/ownership reconciliation/activation/queue; сохранить lock scope. |
| [DNS guard](<../../../assets/geomask-dnsguard.sh>) | 851 | При DNS-работах: observation/desired/apply/revert/dispatch. |
| [Apply](<../../../assets/geomask-apply.py>) | 791 | При задаче3: validation/preview/application/status; сохранить lifecycle owner. |
| [Redactor](<../../../assets/geomask-diag-redact.py>) | 785 | При задаче7: format parsers/key policy/text masking/verifier/CLI. |
| [Module updater](<../../../assets/geomask-module-update.py>) | 712 | При задаче1: catalogue/state/dependencies/staging/install/CLI; lock/CAS не расходятся по независимым процессам. |
| [Module manifest](<../../../tools/module_manifest.py>) | 656 | Позже: schema/ownership/validation/I/O; только при реальной пользе для связности. |
| [Audit state](<../../../assets/geomask-audit-state.py>) | 637 | Позже: storage/revisions/jobs/events; один контракт атомарности. |
| [Node scan shell](<../../../assets/geomask-node-scan.sh>) | 608 | Позже: wrappers/settings/jobs/scheduling; не дробить embedded Python heredoc пополам. |
| [Safe write](<../../../tools/safe_write.py>) | 567 | Небольшой cohesive security helper допустимо оставить с объяснением; не ломать transaction ради счётчика строк. |

Ориентир нового authored-файла: **150–400 строк**, мягкий предел около **500**. Длинную цельную функцию/безопасностный алгоритм не резать механически по N строк: либо отдельный тестируемый design-refactor, либо явно объяснённое исключение. Маленькие entrypoints/generated RPC fragments могут быть короче150. Generated outputs не входят в лимит authored sources. Не обещать автоматическое снижение токенов без проверки реального рабочего сценария.

### 0.2. Выбранная схема: маленькие source-parts → прежние файлы поставки

**Не запускать части через последовательные `sh part1; sh part2`.** Это теряет shell globals/exit status/traps/lock-fds и меняет поведение. На первом проходе части существуют **только при разработке/сборке**: их детерминированная композиция восстанавливает прежний текст в том же порядке, без дополнительных runtime imports/source/процессов/зависимостей. То же для JS: не добавлять Node/Vite/npm на роутер и не заменять LuCI module loader.

Предлагаемые НОВЫЕ build-time пути, пока не существующие: `src/installer/`, `src/backend/`, `src/panel/`, в каждом `order.json`; `tools/compose_sources.py`; `tests/test_source_composition.py`. Manifest — декларативный build-order/список частей, а не вторая CODE_MAP. Расширение `.sh.part`/`.js.part` подчёркивает, что это не отдельно запускаемый скрипт. Проверять синтаксис целого composed-файла, не требовать standalone syntax от части внутри JS render closure.

Первые промежуточные outputs сохраняют существующие имена [installer template](<../../../geomask.sh.in>), [backend](<../../../assets/geo-backend.sh>), [panel](<../../../assets/geo-sync-page.js>). После переноса их разделов единственный authored source — соответствующие parts; outputs становятся generated compatibility views, сохраняются для существующих tests/catalogue/raw updater. Конечный [installer](<../../../geomask.sh>) остаётся одним файлом. В сборке явная цепочка: **RPC render → source composition → catalogue → asset injection/version stamping → итоговый installer**. Если требуется новый catalogue, не создавать цикл self-hash; сохранить существующее исключение catalogue-self-entry.

**Обязательные интеграционные ограничения:**

- [Build](<../../../build.py#L74-L145>) сейчас читает template/assets, инжектирует heredocs и stamp-версии; [catalogue builder](<../../../tools/build_module_index.py#L23-L58>) парсит literal MAP и save_stream-блоки, а [hashing](<../../../tools/build_module_index.py#L93-L106>) зависит от LF/одного final newline. Сохранить их входные формы и installed bytes либо менять всех consumers в одной партии.
- [RPC generator](<../../../tools/gen-geo-rpc.py#L235-L265>) сейчас пишет dispatch/decl прямо в assets. **Не оставлять двух writers:** после переноса renderer пишет выделенные generated source-parts для dispatch/decl, composer собирает asset; manifest RPC остаётся единственным источником методов. `--check` должен обнаруживать drift и source-region, и assembled output, не чинить их молча.
- [Extraction tests](<../../../tests/test_install_safety.py#L23-L59>) читают assembled template; [catalogue tests](<../../../tests/test_module_index.py#L69-L93>) читают старые paths/bytes. Их не ослаблять ради переноса. Добавить check соответствия parts→assembled, чтобы старые тесты не проверяли stale copy.
- Сохранить порядок top-level assignments, вызовов и **повторных определений функций**, даже если они выглядят избыточно; shadowing/cleanup — отдельный bugfix после parity. Не менять quoting, stdin/stdout, heredoc delimiters, `$0`, source-location assumptions, temporary paths, fd inheritance и порядок lock/trap.
- Для Python-кандидатов сначала можно использовать ту же source-only composition с прежним single-file runtime/importlib-path. Настоящее разбиение на runtime packages — отдельная проверяемая партия с полным install/update/rollback ownership manifest, а не скрытая новая зависимость первой фазы.

### 0.3. Конкретные границы первой очереди

Ниже имена будущих parts — предложение, существующие symbols — опорные границы. Shell/Python делить по целым definitions/логическим блокам; ни один heredoc не пересекает файловую границу. Для JS допустимы текстовые render-parts внутри той же closure: они не standalone-модули и проверяются только после точной конкатенации. Композиция не добавляет разделители, wrappers, source-map comments или новые области видимости: UTF-8 без BOM, исходный порядок, LF и точные EOF bytes. Структурно самостоятельные JS modules с explicit context — отдельная следующая партия.

- **Installer / `src/installer/`:** bootstrap+path discovery/controller; config inspection; selection+save/cron helpers; `step1_*`; `step2_*`; `step3_*` и config writer; backup/SMB/trial-restart отдельными областями; `step4_*`; `step5_*`; services; final verify; runner/UI; uninstall; reinstall path-safety/observers; snapshot-files/shared/state; restore-files/shared/runtime; verify-files/UCI/sysctl; nft normalization; verify-net/runtime; reinstall orchestration/preflight/manifests; terminal args/dispatch в конце. Особенно [reinstall область](<../../../geomask.sh.in#L3170-L5364>) не переносить целиком в новый 2 000-строчный файл.
- **Backend / `src/backend/`:** bootstrap/write-gate/controller; status/health; basic actions; IPv6; devices/DHCP; AI-DNS/routes; subscription registry/slot; self-update; scheduling/lists; admission/policy wrappers; console status, state/provisioning, rules/rollback, command orchestration — раздельно; diag collection/redaction-budget wrappers/archive/retrieval; generated dispatch в конце. Граница [console](<../../../assets/geo-backend.sh#L2362-L2956>) сама >500, поэтому недостаточно просто назвать её console.sh.
- **Panel / `src/panel/`:** LuCI directives/generated RPC, shared formatting/styles, view lifecycle и render-open; render sections overview/subscriptions/console/devices/updates/modules/diagnostics/lists/nodes/settings; render-close и `act`/`reload`/`pollJob`. [render](<../../../assets/geo-sync-page.js#L261-L309>) имеет общие `view`, `panels`, `all`, bindings и callbacks: сначала текстовая композиция **внутри прежней closure**, не independent tab-modules с потерянными captures. Вынос в самостоятельные render-функции с explicit context API — только отдельная протестированная следующая партия.

### 0.4. Исполнимые подзадачи и критерии

**Порядок:** 0а → 0б → 0в → задачи1–3; panel/прочие candidates — 0г рядом с задачами4–7. Не откладывать P0/P1 до тотального переписывания всех16 файлов. Если baseline CI красный, сначала получить его причины/границы: структурный перенос не должен маскировать failure. Маленький срочный security-fix может идти до extraction соответствующего компонента отдельным коммитом.

- [x] **0а — baseline и локальная навигация.** Перед переносом зафиксировать текущий SHA/hash всего write-set, source-size inventory, failing/skipped baseline tests и связи generators/tests. Использовать **локальную CODE_MAP, которую создаёт плагин DSH**, обновить её штатным доступным способом после переноса. Отсутствие tracked CODE_MAP.md не означает отсутствия карты; **не создавать/коммитить дубль, cache или DB плагина**. Если индекс недоступен через текущие инструменты — явно указать это, использовать source/manifests и не изобретать API плагина.
- [x] **0б — test-first composer.** Новые `compose_target(target: str) -> bytes` (pure read/compose) и CLI `python -B tools/compose_sources.py --check`/`--write` для трёх разрешённых targets. `--check` немутирующий; `--write` публикует только declared outputs после полной проверки. Tests: exact-order/bytes; changed part → check fails без переписи output; missing/duplicate/unlisted part/unknown target/out-of-root path/cycle → отказ до publish; deterministic LF/EOF и отсутствие необработанных markers. Не использовать eval/shell source при сборке. Manifest использует фиксированный ordered list, без runtime include graph; цикл вложенных includes запрещён/не поддержан.
- [x] **0в — механический перенос installer/backend.** Добавить ordered parts, перевести RPC dispatch writer на свой generated part, подключить composition к build/catalogue/check pipeline. Паритет **байт-в-байт** относительно замороженного текущего baseline для assembled template/backend/panel, всех installed assets/catalogue entries и final installer; в таком коммите нет bugfix/formatting/version bump. Не выполнять installer на устройстве. Новые dev-tools tests — после safety-review, системные только в изоляции. Commit отдельно от дальнейших исправлений.
- [x] **0г — panel и связанные Python/shell.** Panel composition с сохранённой closure/директивами/таймерами; остальные кандидаты извлекать по ответственности непосредственно перед их repair-задачей, с тем же no-behavior-change gate. Не делать из каждого короткого helper новый файл. Если authored part >500, объяснить границу либо пересмотреть её до merge.
- [x] **0д — guards/tooling/docs.** В [development docs](<../../DEVELOPMENT.md>) указать authoritative parts, порядок build и новый способ check; промптам/агентам рекомендовать брать только нужный component+direct contracts, **не generated45k installer и не все parts сразу**. Обновить локальный DSH-index; для поиска через него distinguishing generated outputs настраивать только реально поддерживаемым плагином способом. Repo-check сообщает новые oversized authored files/исключения без ограничения generated outputs. На этапе0 он advisory, не ломает CI из-за ещё не перенесённых16 файлов.
- [ ] **Gate этапа0:** fresh scratch rebuild/compose parity; `python -B tools/compose_sources.py --check` (будущая команда), `python -B tools/check_build.py --verify-only`, `python -B tools/gen-geo-rpc.py --check`; exact catalogue hash/ownership consistency; parser checks composed shell/JS/Python; existing extraction tests не заменены пустышками. Linux/BusyBox/real-runtime gates остаются, не закрываются source-parity. Штатные проверки не должны молча пересобирать tracked output перед сравнением.
- [ ] **Проверка пользы для ИИ:** на трёх типовых задачах (console rollback, installer preflight, domain diagnostics) по локальному индексу открыть только профильные parts+контракты, найти definition/caller/test без чтения всего монолита; записать перечень реально прочитанных файлов/строк. После чистого checkout сборка не требует локальной CODE_MAP и не зависит от профиля DSH. Плагин помогает навигации, но не является build dependency.

**Важно для существующего плана:** ссылки на строки ниже относятся к assembled baseline и после bugfix могут смещаться; при реализации использовать устойчивый symbol/part из локального индекса, не искать старый номер как источник истины. Структурный перенос не исправляет и не закрывает ни один U/I/C/R-дефект сам по себе.

## Review Focus

1. Чужой commit после snapshot до rollback: сохранить чужие bytes, не стирать «восстановлением» (задачи1/3/6).
2. ENOSPC/EACCES/failed reload/failed read: неизвестный runtime не успех RPC/queue (2/4/5).
3. Минимальная выборка не использует случайные leftovers полной установки (2).
4. TUN down/reboot/foreign rules/default: ownership и отсутствие незаметного WAN-egress (4/8).
5. Credential keys/symlinks/подложенный cache: fail closed до диагностики/root-записи (1/7).

## Доказательства и границы

**S** — просмотренный source trace. **F** — контрпример на извлечённых функциях/определениях с синтетическими файлами и точечными stubs. **P** — потенциальный дефект/платформенный gate, не воспроизведённый на OpenWrt.

F доказывает решение логики при заданном отказе, **не** частоту, kernel/flock semantics или успех entrypoint. Git Bash не BusyBox ash. Пробы не использовали сеть, реальные секреты/команды устройства. Родитель проверил ключевые traces; два reviewer отдельно разобрали install/rollback и integration, затем проверили проект плана на overclaims.

Evidence вне Git: [updater](<Y:/openwrt/audit-temp/mass-readiness-20261118/updater-review/results.json>), [install](<Y:/openwrt/audit-temp/mass-readiness-20261118/install-review/probe_results.json>), [integration](<Y:/openwrt/audit-temp/mass-readiness-20261118/integration-review/results.json>), [residual](<Y:/openwrt/audit-temp/mass-readiness-20261118/integration-review/residual-results.json>), [47 tests log](<Y:/openwrt/audit-temp/mass-readiness-20261118/updater-tests-utf8.log>), [install review](<Y:/openwrt/audit-temp/mass-readiness-20261118/install-review/INSTALL-REVIEW.md>), [integration review](<Y:/openwrt/audit-temp/mass-readiness-20261118/integration-review/REPORT.md>). Это ссылки локальному владельцу. GitHub-читателю предназначены source traces/условия ниже; локальные логи не опубликованы.

## Подтверждённые дефекты и несогласованности

**P0** — граница root-записи; **P1** — release-blocker; **P2** — ограниченный workflow/диагностика. Это приоритет перед массовым внедрением, **не CVSS**. Статический риск при trigger не равен наблюдению поломки на устройстве.

| ID | Приоритет / уровень | Причина, контрпример, последствия |
|---|---|---|
| U01 | P0, S+F | [Cache](<../../../assets/geomask-module-update.py#L204-L238>) принимает schema1 JSON из предсказуемого `/tmp` без owner/identity и allowlist destinations. Подложенный synthetic catalogue + matching fetched bytes/hash записал файл вне install root. Требуется локальная подмена cache и последующий root update; пригодность конкретного payload на настоящем updater требует проверки. Hash подложенного catalogue не авторизует root-path. Удалённый exploit не доказан. |
| U02 | P1, S+F | [Staging](<../../../assets/geomask-module-update.py#L380-L406>) проверяет original до fetch; [commit](<../../../assets/geomask-module-update.py#L491-L545>) CAS не повторяет. Owner edit между ними теряется без force. Updater lock не защищает от других writers. |
| U03 | P1, S+F | [Rollback](<../../../assets/geomask-module-update.py#L415-L436>), [copy2/replace](<../../../assets/geomask-module-update.py#L519-L535>): symlink разыменован, заменён файлом. После state-write failure bytes восстановлены, **symlink identity нет**, ответ originals-restored. Native symlink реально создан в пробе. |
| I01 | P1, S+F | [Fresh template](<../../../geomask.sh.in#L1370-L1395>) создаёт VPN-noP2P/AI, VPN обещан позже через панель. [Step7](<../../../geomask.sh.in#L2717-L2719>) требует VPN: CHECK_RC1 только из-за него. Normal auto — ошибка без общего rollback; reinstall — rollback. Это verify failure одно-подписочной топологии, не отказ всего трафика. |
| I02 | P1, S+F | [IPv6 default](<../../../geomask.sh.in#L2808-L2818>) требует backend, отсутствующий при core/path_vpn без luci: IPV6_RC1. Normal падает при нужном apply, [reinstall](<../../../geomask.sh.in#L5354-L5360>) вызывает безусловно даже initially-off. Keep — смена owner policy, не fix. |
| I03 | P1, S+F | [_cron_begin](<../../../geomask.sh.in#L2486-L2491>) отказал, crontab пишется. Также [uninstall](<../../../geomask.sh.in#L3090-L3167>), [restore](<../../../geomask.sh.in#L4686-L4707>), где foreign compare до lock. Fixture LOCK_REFUSED + CRONTAB_WRITE_WITHOUT_LOCK; возможны lost updates. |
| I04 | P1, S+F | [Restore](<../../../geomask.sh.in#L4078-L4145>) lock только при восстановлении, без CAS пишет snapshot поверх завершённого panel commit. Fixture panel_commit:new→snapshot:old, rc0. Lifetime conflict, не прежняя simultaneous-copy race. Restart-preimage имеет аналогичный риск. |
| I05 | P2, S | [Adopt guard](<../../../geomask.sh.in#L1213-L1231>) пропускает, но foreign config не попадает в shaped-only patch; [следующий guard](<../../../geomask.sh.in#L1307-L1314>) отказывает без replace. Static truth table, writer не исполнялся. Обещанный safe-adopt не работает для этого входа; потери конфига не показано. Normal может успеть изменить step2. |
| I06 | P2, S+F | [prompt_url](<../../../geomask.sh.in#L3065-L3084>) печатает prompt в stdout, [caller](<../../../geomask.sh.in#L1135-L1145>) захватывает вместе с URL. Fake-tty fixture вернул `VPN URL [Enter ...]: https://...`. Flags/noninteractive не затронуты. |
| I07 | P2, S+F | [Normalize](<../../../geomask.sh.in#L5929-L5944>) даёт ai_dns/core/path_vpn, [manifest](<../../../modules/ai_dns.json#L10-L22>) требует path_ai. Metadata/selection mismatch доказан; **отказ всех runtime-комбинаций нет**, часть AI создаёт core безусловно. |
| I08 | P2, S+F | [Step2 check](<../../../geomask.sh.in#L797-L855>) статически не требует doctor/актуальности bytes; отдельно real [runner](<../../../geomask.sh.in#L2869-L2878>) с успешным stub check воспроизвёл skip apply. Не запускался whole step2-check с missing doctor. Presence-only logic может пропустить menu update старых bytes; explicit новый URL не участвует step3-check (S). Reinstall — другой путь. |
| I09 | P1, S | Maintenance есть [только в reinstall](<../../../geomask.sh.in#L5834-L5843>), не [normal/step](<../../../geomask.sh.in#L2899-L2922>). [save_stream](<../../../geomask.sh.in#L460-L482>) общий temp `.zmnew`. A:open, B:truncate, A:mv, B:write(fd) допускает изменение опубликованного inode. Linux race не исполнялся. |
| I10 | P1, S | [Stored selection](<../../../geomask.sh.in#L535-L555>) загружается [поздним loader](<../../../geomask.sh.in#L617-L629>), [step6/check](<../../../geomask.sh.in#L5973-L5988>) может не вызвать. [Empty=all](<../../../geomask.sh.in#L399-L407>); damaged state гасится/расширяет выбор. Write truncate/non-atomic, отказ лишь warn. |
| I11 | P1, S | Full→subset: [uninstall](<../../../geomask.sh.in#L3090-L3167>) не убирает guard hooks/cron и AI-DNS services/nft; selected-off шаги skip. Старые активные компоненты могут остаться вне selection. Omission подтверждён; procd/nft эффекты — стенд. Owner data отличать от removable runtime. |
| I12 | P1, S | [Step5](<../../../geomask.sh.in#L2629-L2634>) игнорирует write/seed failures/SAVE_FAIL, return0; presence verify допускает старый asset после отказа update. [Normal/standalone](<../../../geomask.sh.in#L2909-L2922>) без общего reinstall journal; [AI rollback](<../../../geomask.sh.in#L2087-L2103>) чинит только часть UCI/uid, не поздний file/service/nft write-set. |
| C01 | P1, S+F | [Console STALE](<../../../assets/geo-backend.sh#L2902-L2907>) → [restore](<../../../assets/geo-backend.sh#L2719-L2725>) новым unconditional commit без expect-sha256. TX правильно отказал до mutation, caller стёр concurrent bytes preimage. Также late rollback после своего и нового чужого commit. |
| C02 | P1, S+F | [Console state](<../../../assets/geo-backend.sh#L2940-L2950>): четыре `_console_set` отказали; извлечённый cmd_console вернул rc0/ok:true, rule активен без state, preimage удалён. Настоящий RPC transport не запускался. |
| C03 | P1, S+F | [IP switch](<../../../assets/geo-backend.sh#L2687-L2689>) удаляет нового IP, [state](<../../../assets/geo-backend.sh#L2944-L2947>) забывает старый. A→B оставляет обе rules; новый владелец старого DHCP IP получает неожиданный policy. |
| C04 | P1, S+F | [AI-DNS](<../../../assets/geo-backend.sh#L1131-L1155>) сохраняет include/state=ok до reload; оба reload отказали. Identical sync cmp-returns без повторного apply. Persisted include не активный DNAT. |
| C05 | P1, S+F | [Routes100/101](<../../../assets/geomask-routes.sh#L621-L675>) игнорируют rc rule add, return0. Все add отказали, оба apply зелёные; worker может удалить событие без LAN/AI policy. |
| C06 | P1, S+F | [IPv6 save](<../../../assets/geo-backend.sh#L632-L656>) all/default; [restore](<../../../assets/geo-backend.sh#L715-L735>) default всем. Fake proc WAN1 после off→on стал0. Потеря preimage доказана, kernel нет. Остаток вне G04 UCI-fix. |
| C07 | P1, S+F | [Sync bytes](<../../../assets/sync_mtrickle_mihomo.sh#L229-L256>) сохранены, [reload](<../../../assets/sync_mtrickle_mihomo.sh#L408-L425>) отказал. Identical run changed=false, нет retry. Статус c1/c2, не c3/c4. Нет applied/pending proof. |
| C08 | P1, S+F | [Backend on/off](<../../../assets/geo-backend.sh#L2688-L2710>) удаляет from-IP/table102 без owner pref19903; off sweeps **распознанные IPv4-source rules→102**, не любые произвольные rules102. Fixture удалил foreign pref15000. Guard уже narrow, backend нет. |
| C09 | P1, S+F | [_console_rule_off](<../../../assets/geo-backend.sh#L2692-L2711>) failed show считает отсутствием. Все show/delete=1, rc0. Emergency off не должен говорить успех при unknown. |
| C10 | P1, S+F | [Redactor](<../../../assets/geomask-diag-redact.py#L204-L247>) пропускает api_key/access_token/client_secret с short/hyphenated synthetic values; verifier разрешает. Leakage возможен **если поля попали в bundle log**. Реальные секреты не читались, actual device leak не установлен. |
| C11 | P1, S | [Slot writer](<../../../assets/geomask-slot-patch.py#L116-L173>) live open(w) вне config-tx/CAS. [Caller](<../../../assets/geo-backend.sh#L1377-L1394>) общий unchecked bak_slot/mv. Concurrent slots/TX допускают lost update/усечение; [_mihomo_reload](<../../../assets/geo-backend.sh#L1514-L1517>) маскирует curl rc sleep, caller ok:true. |
| R01 | P1, S | [Release](<../../../tools/release.py#L129-L159>) mutating check_build/raw discovery/shell в обход testgate. [Verify](<../../../tools/release.py#L255-L273>) без full говорит «устанавливается» после syntax/list-modules; установки не было. Tooling/evidence gap, не доказанный дефект release asset. |

## Потенциальные риски / gates

| ID | Условие | Проверка до закрытия |
|---|---|---|
| P01 | [Console guard](<../../../assets/geomask-guard.sh#L383-L405>): rule есть, default102 via WAN, TUN отсутствует, replace отказал; WAN остаётся+degraded. Conditional static fail-open, не observed egress. | Netns/OpenWrt: protected traffic не выходит WAN; ownership-safe, не blind delete чужих routes. |
| P02 | [Updater deps](<../../../assets/geomask-module-update.py#L334-L361>): version/existence без hash/protocol generation; same-version helper может пройти новый caller gate. | Реальная compatibility old/new helpers, changed hash, selected/unselected deps. |
| P03 | [Update](<../../../assets/geomask-module-update.py#L648-L680>): bytes/state без общей maintenance/runtime activation/persistent recovery; mixed tree и SIGKILL вне except. | Каждое module, concurrent installer, restart requirement, kill/state/recovery; files-installed не healthy. |
| P04 | Master как update channel не отделяет WIP push; HTTPS/digest в одном trust domain. | Документировать trust/channel/release gate. Новые подписи/ключи не назначать фичей без решения владельца. |
| P05 | [N03/N09](<../../GN-MAPPING.md#L32-L38>): alias absent vs invalid, live/stored status. | Alias down→up без изменения list; actual nft/listener/route; не заменять live check stored flag ради ID. |
| P06 | Low overlay/tmp/inodes, EACCES, backup/package/restart failure, links. Warning не recovery proof. | ENOSPC каждого stage, valid nonnested backup/restore/rotation, owner data, инструкция повторного запуска. |
| P07 | Authentic RPC roles, BusyBox/UCI/fw4/procd/netifd/flock/SIGKILL/fd; whole base64 retrieval до advertised2MiB. | Read не вызывает write, JSON/stdout/secret-safe logs, actual transport с base64 expansion, boot/hotplug. **Новый chunk API не требуется.** |
| P08 | [Restore services](<../../../geomask.sh.in#L4662-L4683>) running boolean без reload живого daemon; [provider API](<../../../geomask.sh.in#L5340-L5341>) и services могли изменить runtime. Disk preimage не graph/registry/argv/cache. | Late failure после runtime change: controller/procd/rpc registry/egress до/после, не PID; mismatch = incomplete. |
| P09 | [Persistent backup→tmp park](<../../../geomask.sh.in#L1593-L1606>), [volatile snapshot](<../../../geomask.sh.in#L5849-L5851>); power loss до publish может убрать recovery. Tar/CRC/etc не actual keep-list. | Kill/reboot park/publish/uninstall/rename/start, last valid durable до replacement, PC-копия и actual restore/write-set. |
| P10 | [Mihomo start](<../../../geomask.sh.in#L1163-L1168>) до [hotplug install](<../../../geomask.sh.in#L2471-L2478>); initial event нет; [final](<../../../geomask.sh.in#L2728-L2740>) files/error, не LAN dataplane. Already-up/no-ifup может оставить no table100/rule. | Clean netifd stand: already-up TUN/empty queue/no100; initial reconcile и LAN-client probe. Reload может случайно скрыть окно, не гарантия. |
| P11 | [Syncd](<../../../assets/geomask-syncd.sh#L24-L44>): blind window debounce/run, no initial sync, fallback ls-l теряет same-size same-minute rewrite. Static schedule. | Linux A→run прочёлA→B в blind window: B применяется без третьего события; startup/directory replace/poll. |
| P12 | [Raw dump](<../../../assets/geo-backend.sh#L3334-L3339>) целиком до [size check](<../../../assets/geo-backend.sh#L2985-L2991>). Cap512KiB не source/peak cap; timeout не byte limit. OOM не воспроизведён. | Bounded stream cap+1/placeholder/cancel, peak concurrent allocation/ENOSPC. Маленький tgz не peak proof. |

## Прежние OPEN и границы scope

[Таблица G/N](<../../GN-MAPPING.md>) не является общей гарантией готовности.

- G02: [doctor AND/OR](<../../../assets/geomask-doctor.py#L505-L548>) any для обоих; network/port/source unknown не false. Задача5.
- G03: [DHCP](<../../../assets/geo-backend.sh#L898-L909>) setters без dhcp./rc; пустой host/ложная ownership. Задача5.
- G05: общий hev pid не отдельный instance/fixed listener/egress. Задача5.
- G06: [широкая presence](<../../../assets/geomask-routes.sh#L628-L652>) pref/from/fwmark, conntrack всех AI/bypass. Задача4.
- N04: missing PyYAML не foreign config; refusal до mutation, не destructive replace.
- N08: unused snapshot-present — cleanup после completeness proof, не самостоятельный blocker.
- N10/N12: empty-main fixture, resource recovery P06 остаются.
- B-020 OPEN: [уточнённый план](<../../DOMAIN-DIAG-PLAN-2026-11-15.md>), deadline/честный probe path, не доказанная ISP/site блокировка; без повторного router access.
- D14 periodic DNS-repair исключён как новая owner policy.
- R02/R03/R07/R11: fixture не target acceptance; повтор phase2a–2f на final SHA, POSIX/ACL/runtime gates. Старые unchecked не закрываются аудитом.

## Исправления: независимые партии

Новые test names — задания, не существующее покрытие. Каждая партия: failing behavioural test → безопасный RED → минимальный fix → GREEN+соседние regressions → review → commit. Системные tests на текущем хосте/роутере не запускать.

### Задача1 — updater trust/integrity (U01–U03/P02–P04)

**Файлы:** [updater](<../../../assets/geomask-module-update.py>), [tests](<../../../tests/test_module_update.py>), [contract](<../../MODULES.md>). Сохранить load_index/download_module/install_staged/rollback_files либо обновить всех callers.

- [ ] `test_cache_untrusted_catalogue_refused`: precreated/future cache, wrong owner/link/cache.tmp, destination вне shipped policy → no write, sentinel цел. Закрепить private-cache trust/identity и destination authorization; Linux UID/nofollow отдельно.
- [ ] `test_owner_edit_between_stage_and_commit`: v1→owner после stagev2, без force отказ/owner сохранён; fingerprint/identity revalidate под нужным общим lock.
- [ ] `test_state_failure_restores_symlink_identity`: real/dangling/parent links → pre-mutation refusal либо exact restore, no false originals-restored.
- [ ] Dependency generation, interruption каждого replace/state, updater-vs-installer, activation contract. Не healthy по hash.
- [ ] Gate: прежние47+новые tests, no unexpected skips, build/RPC parity, commit.

### Задача2 — install lifecycle/selection (I01/I02/I05–I12/N04)

**Файлы:** [template](<../../../geomask.sh.in>), [install tests](<../../../tests/test_install_safety.py>), [module tests](<../../../tests/test_module_index.py>), [metadata](<../../../modules/ai_dns.json>). Generated installer только сборкой после source fix.

- [ ] Fresh VPN-noP2P-only проходит selected-topology verify; genuinely missing required group fails.
- [ ] Core/path_vpn headless выполняет default IPv6-off без luci leftovers; on/off, normal/reinstall, keep отдельно.
- [ ] Stored selection validate до всех entrypoints; corrupt не all, atomic state publish/mandatory rc. Dependency graph совпадает с metadata либо unsupported отказ до mutation.
- [ ] `test_prompt_stdout_is_url`: pseudoTTY Enter/new/retry/EOF; UI stderr/TTY, stdout URL. Flags/noninteractive regressions.
- [ ] Foreign+adopt сохраняет tun/dns/secret/rules либо отказ до записей без false promise. Replace — отдельный consent.
- [ ] Healthy-base add doctor/menu update old assets/changed explicit VPN URL реально применяются; identical URL/run идемпотентен.
- [ ] Full→subset снимает собственные guard/AI-DNS services/links/cron/hotplug/nft/rules с rollback, owner data/foreign resources целы.
- [ ] Каждый save/seed/chmod/start/reload failure nonzero, version/selection не опережают bytes/runtime; normal/standalone recovery отдельно от reinstall, package side effects честно ограничены.
- [ ] Gate: clean full/minimum/add/remove/fault cases, commit.

### Задача3 — все writers/lifetime rollback (C01/C11/I03/I04/I09)

**Файлы:** [backend](<../../../assets/geo-backend.sh>), [slot](<../../../assets/geomask-slot-patch.py>), [TX](<../../../assets/geomask-config-tx.py>), [installer](<../../../geomask.sh.in>), [lock tests](<../../../tests/test_lock_protocol.py>).

- [ ] `test_console_stale_does_not_restore`: no-mutation STALE без restore; late rollback CAS к **собственной postimage**, новый чужойB цел/recovery copies сохранены.
- [ ] Slot выдаёт candidate bytes, единственный live writer existing TX validate/trial/atomic/CAS/reload; убрать open(w)/unchecked bak_slot.
- [ ] `test_reinstall_rollback_preserves_completed_panel_commit`: snapshotA→our write→panelB→late fail оставляетB/conflict; безB возвращаетA. Также restart-preimage.
- [ ] Единый lifetime exclusion всех writers либо journaled postimage/CAS; короткого lock cp недостаточно.
- [ ] Maintenance normal/step/reinstall/backup до первой записи, unique temps; pairwise contention второго останавливает до mutation.
- [ ] `test_cron_lock_refusal_no_write`: install/migration/uninstall/**restore**, foreign compare/read/write внутри успешного lock.
- [ ] Linux parallel/rank/SIGKILL/fd/atomic publish/permissions gate, commit.

### Задача4 — console/routes exact ownership/unknown (C02/C03/C05/C08/C09/P01/G06)

**Файлы:** [backend](<../../../assets/geo-backend.sh#L2670-L2950>), [routes](<../../../assets/geomask-routes.sh>), [guard](<../../../assets/geomask-guard.sh>), [status tests](<../../../tests/test_console_status_contract.py>), [queue tests](<../../../tests/test_route_queue.py>).

- [ ] Complete atomic state/checked marker до удаления recovery; failed write/rename никогда ok:true с rule без state.
- [ ] A→B только ownB; foreign pref15000/table102 живёт через on/off/guard; exact pref/from/iif/table/fwmark.
- [ ] Failed show/delete/add → error/UNKNOWN; queue сохраняет retry, next drain исправляет, не success-removal.
- [ ] Netns TUN down+WAN default102+replace failure: no unnoticed WAN-egress без удаления чужого; reboot/down/up.
- [ ] Conntrack только изменённых клиентов; source/runtime review, commit.

### Задача5 — applied DNS/rulesets/диагностика (C04/C07/G02/G03/G05/B-020/P10/P11)

**Файлы:** [backend](<../../../assets/geo-backend.sh>), [sync](<../../../assets/sync_mtrickle_mihomo.sh>), [syncd](<../../../assets/geomask-syncd.sh>), [doctor](<../../../assets/geomask-doctor.py>), [панель](<../../../assets/geo-sync-page.js>).

- [ ] Failed reload = error/pending, identical next run retries до read-back; persisted не applied; changed/reload status включает c1/c2/c3/c4.
- [ ] Alias down/up, device/UID/listen changes → nft/DoH/route/status согласованы; проверка DNS из LAN.
- [ ] DHCP package.section.option/checked set/commit, no false ownership/foreign lease deletion.
- [ ] AND 00/01/10/11, OR, unknown context; отдельные hev instances/fixed listeners/egress.
- [ ] B-020 deadline15с с residual budget/partial result/router-proxy label; no ISP-blocking inference.
- [ ] Initial reconcile already-up/empty queue; final LAN dataplane. Syncd initial/blind-windowB/same-size rewrite достигает runtime без третьего события.
- [ ] Behavioural+isolated netifd/fw4/nft/rpc/DNS gate, commit.

### Задача6 — IPv6 restore (C06/P06)

**Файлы:** [backend](<../../../assets/geo-backend.sh#L632-L797>), [tests](<../../../tests/test_backend_safety.py>), [reinstall tests](<../../../tests/test_reinstall_phase2b.py>).

- [ ] All/default0, wan1, lan0: off/on точный per-interface roundtrip; new/disappeared interface safe contract.
- [ ] Off-off-on сохраняет UCI/RA/DHCPv6 и nft/sysctl ownership; failed save не заменяет original.
- [ ] Owner edit после save не стирать молча; OpenWrt persisted+kernel/reboot, commit.

### Задача7 — secret-safe/resource-bounded diagnostics (C10/P07/P12/R07)

**Файлы:** [redactor](<../../../assets/geomask-diag-redact.py>), [spool](<../../../assets/geomask-diag-spool.py>), [producers/package](<../../../assets/geo-backend.sh#L3252-L3464>).

- [ ] Synthetic short api_key/access_token/client_secret и case/underscore/dot в YAML/JSON/log/query; независимые adversarial sentinels в packed members, не только redact/verifier self-consistency.
- [ ] Полезные host/SNI сохранены, malformed/oversize/deep fails closed; no real secrets в fixtures.
- [ ] Source bytes/peak ограничены, concurrent archives/cancel/ENOSPC bounded без residue.
- [ ] Linux permissions/links/races, authentic RPC role/expiry/**полный retrieval** до advertised2MiB с base64 expansion, без требования нового chunk API; commit.

### Задача8 — tooling/recovery proof/final gate (R01/P06–P10)

**Файлы:** [release](<../../../tools/release.py>), [CI](<../../../.github/workflows/ci.yml>), [testgate](<../../../tests/testgate.py>), [boundaries](<../../../tests/BOUNDARIES.md>), [README](<../../../README.md>).

- [ ] Сначала получить доступный владельцу unit-gate artifact/log CI187, назвать конкретные failed modules/tests/timeouts и причины skips, воспроизвести на изолированном Linux и исправить подтверждённые причины. Повторить CI на final SHA; текущий failure не закрывать локальными47 tests.
- [ ] Readiness немутирующая, final clean/build/hash после preparation до tag; штатный counts/skips/timeouts gate в изоляции, syntax/list-modules не install proof.
- [ ] Last valid backup durable до replacement; kill/reboot stages, actual archive write-set/restore с PC-копии. Volatile snapshot не power-loss guarantee.
- [ ] Late rollback actual graph/registry/argv/egress либо incomplete; bytes/PID недостаточны.
- [ ] **Final product SHA**: матрица ниже, logs SHA/image/packages/modules/commands/executed/skipped/reasons/duration/exit/rollback/reboot; не переносить старый phase2 успех.
- [ ] Independent review/build/RPC/no unexplained skips, docs по evidence. Release/tag отдельно от docs push.

## Матрица полной/частичной приёмки

Все строки **NOT RUN на OpenWrt в текущем аудите**. «Чистый» = без geomask, **с базовым mihomo/MagiTrickle/hev**. Это не все2^15 сочетаний.

| Профиль | Вход / переходы | Результат |
|---|---|---|
| Все modules | чистый image, VPN-noP2P, затем второйVPN | install/verify/reboot, selected groups/services/listeners/DNS/WAN/LAN/local |
| core/path_vpn | без luci/backend leftovers, off/keep | headless core и выбранная IPv6 policy |
| core/path_vpn/path_ai | без ai_dns, add ai_dns | согласованные AI/DNS/graph/rollback отказа |
| ai_dns выбором | чистый root | closure либо pre-mutation refusal; metadata/runtime/readiness согласованы |
| Без luci/backup_share | dependency-valid selections | нет UI/share ожиданий; **обязательный core sync остаётся**, отдельного selectable sync-модуля нет |
| Partial→full→partial | leftovers присутствуют | cleanup/state/runtime/owner data согласованы |
| Managed/foreign | PyYAML absent/present, adopt/replace | no destructive fallback, consent/dependency refusal до записей |
| Reinstall | phase2a–2f+I/C/U | failpoints/completed competing commit/exact files-UCI-runtime-ownership restore |
| Каждое module update | same-version/hash, old/new deps | bytes/state/activation/local edits/interruption/updater-vs-installer |
| Lifecycle | reboot/ifup storm/PPPoE down/TUN late/alias flap | no lost events, LAN/local доступны, no unnoticed WAN-egress |
| Resources/security | tmp/overlay/inodes/EACCES/tmp attacker/roles | bounded/no false green/secret-safe/durable recovery |

Dependency-valid pairwise optional combinations — тестовые данные, не feature. Проверить заявленные OpenWrt24.10/opkg и25.x/apk/BusyBox ash; другие архитектуры не выводить из aarch64-fixture. Каждый standalone step — negative dependency tests до mutation, не зависимость от ранее выполненного step1.

## Закрытие плана

- [ ] P0/P1 regression+review; P2 исправлены или явно согласованы как ограничения.
- [ ] P-risks: reproduced/fixed, disproved с evidence либо явный support/release gate; не FIXED по предположению.
- [ ] Full/minimum/transitions/update/rollback на одном final SHA, crash/reboot/DNS/routes/ACL gates без скрытых skips.
- [ ] Backup/restore доказан disposable stand, не просто tar/backup-ok.
- [ ] Independent final review и опубликованные trust/ограничения. Это не гарантия отсутствия всех ошибок.

**Сейчас:** документы исправлены/публикуются; продукт не исправлялся, допуска к массовой установке нет. Обращений к роутерам и Z в этом аудите не было.
