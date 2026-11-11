# Полное завершение раздельных фильтров путей — план реализации

**Дата:** 08.11.2026, MSK. **Статус:** проект плана для утверждения; не разрешение на реализацию/применение. В этой задаче production-код и роутер не меняются.

**Цель:** законченный автономный цикл для VPN, VPN-noP2P и AI: настройки → отдельная проверка фактического выхода → предпросмотр → безопасное применение → подтверждение рабочего состояния/откат → расписание и история действий.

**Архитектура:** продолжить установленный node-audit, не включать незавершённые policy/admission-прототипы. Один источник настроек путей, явные версии настроек/источников/измерений, один владелец управляемого пула каждого пути и один протокол commit/reload общего mihomo-ресурса. Для применённого пула использовать зафиксированное поколение проверенных узлов, а не один regex над самопроизвольно обновляемой HTTP-подпиской.

**Стек:** OpenWrt/BusyBox sh, Python3/PyYAML, mihomo controller API, LuCI JavaScript/rpcd/ubus, procd/cron; локальные unittest, Git-bash sandbox и Chromium-тесты.

**Основания:** [основной план v2](PLAN-2026-11-06-geomask-v2-rework.md), [спецификация первой поставки](SPEC-PATH-EGRESS-AUDIT-2026-11-08.md), [текущая инструкция](NODE-AUDIT.md), прямые уточнения владельца 08.11: страны выхода к сайту, проверка отдельно от применения, автономность geomask без PC receipt/помощника, бэкап на ПК — обязанность агента перед вмешательством.

> Исполнение — отдельными законченными этапами с тестами и независимой рецензией. Файлы/методы с пометкой «новый» ниже ещё не существуют и не должны выдаваться за готовые API. Коммит/push/release — только по отдельному запросу.

## 1. Что уже есть, что отсутствует

Проверено по исходникам и сохранённому read-only снимку [19:35](../../audit/2026-11-08/apply-log-check/inspection.json):

- `assets/geomask-node-audit.py`: `save()` проверяет страны, берёт flock, сравнивает общий SHA settings-store, атомарно сохраняет желаемое; возвращает `applied:false`. PC-gate из пользовательского Save удалён.
- `start()/check()/IsolatedProbe` запускают изолированный mihomo, проверяют источник узлов и пишут последний отчёт пути в /tmp; отдельного применения нет.
- RPC `nodes_paths_get`, `nodes_path_save`, `nodes_path_check` существуют. `nodes_path_apply` отсутствует; нельзя переиспользовать одноимённые по смыслу черновые `policy_apply`/`policy_preview` как проверенный механизм.
- UI показывает три карточки, но нет полноценного polling/cancel/history/preview/apply/runtime reconciliation. Надпись «Применить» у другого блока управляет интервалом синка, не фильтром пути.
- Ревизия настроек общая: изменение VPN-noP2P делает отчёт VPN устаревшим. Текущий config digest слишком широк для независимости путей и недостаточен для идентичности обновлённой выдачи по прежнему URL.
- В новом `save()` нет отдельного события журнала. Отчёты перезаписываются; общий `/root/geo-sync.log` содержит старую автоматику, не полный журнал новых действий.
- `geomask-node-scan.sh::apply_filter_all` обновляет группы по общему provider, а `--ai-guard` переключает production AI-группу и самостоятельно меняет фильтр. Их нельзя оставить конкурирующими владельцами нового пула.
- Последний подтверждённый snapshot: желаемые VPN=DE,FR,NL,PL; VPN-noP2P=DE; AI=US. Конфиг mihomo при сохранениях не изменялся; в live VPN-noP2P оставались NL/FR/SE. Это снимок времени, не постоянная база миграции.
- Google probe сейчас всегда UNKNOWN по стране. ChatGPT/Cloudflare trace работает; Google `/cdn-cgi/trace` — 404, Search может вернуть CAPTCHA. HTTP204 не доказывает US.

## 2. Границы и уточнение безопасности

### Обязательно

- Рабочий каталог Y, Z не трогать. Существующий dirty tree, пользовательские настройки и исторические доказательства сохранять.
- Перед агентским внедрением/правкой на роутере — свежий штатный полный sysupgrade-бэкап на ПК с проверенной суммой и составом + точные snapshots кода, не покрытого архивом. Предыдущее состояние не удалять до проверки нового архива.
- Geomask автономен: нет PC daemon, Windows-автозапуска, сетевого порта на ПК или PC receipt для Save/Check/Apply/ночной работы.
- Для будущего изменяющего runtime Apply — локальные проверенные предобразы/журнал восстановления; полный локальный архив в рамках существующей функции geomask с одной последней копией, если его создание входит в транзакцию. История на ПК остаётся агентской задачей. Не создавать вложенные архивы, не расширять гостевую SMB-доступность, не обещать покрытие всего service-кода sysupgrade-архивом.
- Старое требование PC-gate для каждой автоматической записи из основного плана §§3/6 должно быть явно согласовано с последующим уточнением владельца ДО внедрения новой автоматики. Не оставить два противоречащих обязательных контракта; это точечная актуализация документа, не ослабление агентского бэкапа.
- No-op не делает reload, не переписывает persistent-файлы и не меняет ревизии. Событие no-op допустимо в ограниченном журнале.
- «Применено» означает проверенный активный пул, а не только запись YAML или HTTP204 controller.
- Запрет чужих стран/UNKNOWN не превращается в DIRECT/другую подписку/соседний путь. Пустой пул — явно BLOCKED, с проверенным рабочим запретом; не пустая группа, которую движок может заполнить DIRECT/COMPATIBLE.
- Ручная проверка не меняет production-группу/рабочий узел, не закрывает клиентские соединения. Никаких `-k`, доверия именам USA или замены service-specific географии чужим IP-checker.

### Не включать попутно

Перепрошивку, базовую сеть/WAN/PPPoE, полную модуляризацию v2, переустройство P2P всего LAN, произвольный сервис мониторинга сайтов, изменение подписок или удаление hosts. Влияние Apply на существующие ingress/устройства проверить; если обеспечить выбранный путь без такой перестройки невозможно, остановиться на конкретной границе, а не делать скрытую миграцию.

## 3. Предлагаемые модули и порядок

| ID | Ответственность | Зависимости |
|---|---|---|
| audit-state | Настройки, per-path версии, связь с источником, фактическое applied-состояние | — |
| audit-history | События, immutable job-отчёты, bounded retention, экспорт | audit-state |
| audit-probes | Изолированные service-specific измерения, DNS-контекст, ресурсы | audit-state, audit-history |
| config-transaction | Общая запись/reload/rollback mihomo и координация writers | audit-history |
| audit-apply | Предпросмотр, immutable pool, commit, reconciliation | все выше |
| audit-ui | Внятные состояния и полный пользовательский цикл | state/history/probes/apply |
| audit-schedule | Расписание, очередь, замена legacy автоматики | probes/apply/history |

Порядок поставки: **A база → B состояние → C журнал → D проверки → E транзакция → F Apply → G UI → H расписание/миграция → I приёмка/выпуск**. UI можно разрабатывать на fixture параллельно, но не объявлять готовым до live-контрактов. Полную feature нельзя считать завершённой после B/C/D.

## 4. Конечный пользовательский сценарий

У каждой карточки пути:

1. Имя пути и человекочитаемое имя назначенной подписки; не только `sub2`/`slot`.
2. Разрешённые страны, дополнительные явные ограничения и выбранный контекст проверки.
3. **Сохранить настройки** — сохраняет желаемое, ничего не применяет.
4. **Проверить путь** — отдельное задание со стадиями, прогрессом, временем, очередью и отменой.
5. Результаты узлов: eligible / excluded / unknown, отдельно страна каждого сервиса, причина, источник факта и время. Ошибка проверки не называется «страна запрещена».
6. **Предпросмотр применения** — точный состав будущего рабочего пула, удаляемые/добавляемые узлы, что случится с текущим выбранным узлом, конфликты legacy-ограничений и предупреждение о существующих соединениях.
7. **Применить проверенный результат** — отдельное подтверждение. На пустой пул отдельное явное действие «заблокировать путь», без разрешающего fallback.
8. Состояние после Apply: «Применено и проверено», active countries, версия/время, фактический пул и выбранный узел; при провале — «Ошибка, откат подтверждён» или «Нужно восстановление», не зелёный успех.
9. **История** и просмотр подробного отчёта/транзакции, включая кто/из какого entry point запустил действие (без публикации session token).

Различать состояния: unsaved draft, saved-not-checked, queued/running/cancelling, checked, stale, preview-ready, applying, applied-verified, runtime-drift, blocked-policy, error, rolled-back, needs-recovery. Одно поле `ok` этого не заменяет.

Форма должна явно показывать «желаемые» и «действующие» страны, когда они различаются. Сохранение в двух карточках не должно терять ввод второй карточки при reload всей страницы.

## 5. Данные и новые контракты

### 5.1 Хранилища

- `/etc/geomask/node-audit.json`: один canonical store желаемых настроек и стабильных source bindings; миграция schema1→schema2, без автоматического изменения стран/применения.
- У каждого пути своя `path_revision`, кроме общей storage revision под flock. Сохранение одного пути не инвалидирует отчёт другого без действительной общей зависимости.
- `source_identity`: стабильный registry ID при наличии + digest приватного endpoint/header-конфига; не URL в UI/журнале. Запоминается исходная подписка, не вычисляется после Apply из нового managed file provider как будто он HTTP-источник.
- `node_identity`: provider/source identity + transport/server/port/важные параметры узла + контекст probes. Одно имя узла недостаточно. Дубли имён/несовпадающие endpoints не сливать.
- Отчёт имеет immutable `job_id`, started/finished, path/settings/source/nodes fingerprints, probe version/context, каждый сервисный факт и время. «Последний отчёт» — указатель, не единственная копия.
- Applied manifest отдельно описывает **подтверждённое** поколение: path revision, preview/job IDs, источник, approved node identities, generated pool digest, runtime verification time, block/recovery status. Не равнять desired generation с фактически применённой.
- Нужен root-only immutable snapshot **самих проверенных proxy definitions**, keyed по job/source digest, а не только имён/хеша отчёта. Compiler берёт ровно этот материал. Если выбран повторный fetch вместо хранения — обязательное полное совпадение relevant fingerprints; несовпадение=STALE. Cleanup не удаляет поколения/материал, на которые ссылаются active/applied/rollback/pending-preview manifests; привязанные snapshots входят в отдельный ограниченный budget, при нехватке места новая операция отказывается, а не удаляет действующий pool.
- Source rebind имеет отдельный контракт `rebind(path,new_source,expected_path_revision,expected_config_sha)`: обновляет desired binding, инвалидирует preview/report, **не подключает managed group напрямую к непроверенному HTTP provider**. Предлагаемое безопасное поведение: атомарный переход пути в BLOCKED до аудита и Apply нового источника; preview предупреждает об этом до подтверждения переназначения. Старый active source не выдавать за новый. `cmd_sub_del` отвергает удаление referenced source без явного unbind/rebind. Это изменение пользовательского поведения утверждается вместе с планом.
- Поколения managed-pool файлов приватные, содержат секретные proxy definitions. Их не включать в JSON history/export/public fixtures; права 0600, директория0700.
- Предлагаемый storage budget (проектное значение, утвердить при приёмке): event-журнал 4×256KiB, подробные обезличенные отчёты максимум20 и4MiB суммарно/7дней; превышение любого лимита удаляет старейшие непривязанные отчёты, но не сведения/предобразы текущей незавершённой транзакции. Не писать progress на overlay для каждого TCP-запроса; live progress в /tmp, durable terminal summary ограниченно.

### 5.2 RPC — новые методы, не существующие сегодня

Сохранить текущие get/save/check с явной миграционной совместимостью ревизий; расширять только через `tools/geo-rpc.json` и генератор четырёх точек интеграции.

- `nodes_path_source_refresh(path_id, expected_source_revision)` — ограниченно загрузить новую выдачу в приватное хранилище аудита, выдать source generation; не обновлять live approved pool.
- `nodes_audit_job_get(job_id)` — единый status для audit/apply/recovery с `kind`, стадией, прогрессом, конечным исходом и ссылкой на отчёт/transaction. Можно продолжить наблюдение после reconnect; не отдельная скрытая история Apply.
- `nodes_audit_cancel(job_id)` — audit отменяется с проверкой принадлежности процесса. Для Apply до install допустима отмена; после начала commit API либо отвергает cancel как too-late, либо запрашивает управляемый rollback и ждёт проверенного конечного статуса. Нельзя просто убить worker/production mihomo.
- `nodes_path_preview(path_id, job_id, expected_path_revision)` → immutable `preview_id`, digest кандидата и diff.
- `nodes_path_apply(path_id, preview_id, expected_path_revision)` → transaction/job ID; не произвольный YAML из браузера.
- `nodes_audit_history(path_id, cursor, limit)` — bounded pagination.
- `nodes_audit_schedule_get(path_id)` / `nodes_audit_schedule_set(path_id, enabled, time, mode, expected_revision)` — точный формат утвердить в локальной спецификации этапа H; mode check-only или check-and-apply, без неявного автоприменения.

`nodes_paths_get` возвращает api_schema=2 и path-specific revisions. Сигнатура Save сохраняется, но `expected_revision` становится ревизией выбранного пути, а не всего файла; старый клиент с общим SHA получает STALE/RELOAD_REQUIRED, а не обход CAS. Для короткой совместимости UI+backend выкатываются согласованно и запрашивают обновление старой вкладки. Check сохраняет текущую сигнатуру, но job явно возвращает фактически зафиксированные path/source revisions; если они отличаются от показанных в UI, это видно и не разрешает Apply. Очередь проверяет snapshot перед запуском и публикацией, а Apply повторно сверяет зависимости под transaction lock.

Все mutation entry points имеют write ACL; GET/history — read. Raw JSON types/NUL/controls/duplicate keys проверять до jshn; standard valid ubus_rpc_session metadata допускается, в журнал не пишется. Новые JS RPC используют стандартные positional params.

### 5.3 События

Одна структурированная запись на событие: schema/event_id, timestamp+timezone, path_id, actor (`luci/manual/cron/installer` и необязательное безопасное отображаемое имя), action, job/transaction IDs, before/after revisions, stage, outcome/error_code, counts, config/runtime digests, rollback outcome.

События: save success/noop/rejected, check queued/start/progress-summary/complete/failed/cancelled, preview created/rejected/stale, apply requested/validated/trial/committed/runtime-verified/rollback, subscription refreshed/rebound, schedule changed/run/skipped, runtime drift/recovery.

До первой persistent-мутации Apply — надёжный intent/journal; если его нельзя сохранить, отказ до изменений. Если отказал финальный журнал после commit, восстановить статус из transaction journal/readback и показать предупреждение, не заявлять «не применено», если runtime уже изменён. События старой даты не являются свежим подтверждением.

## 6. Карта файлов

### Существующие — изменить по этапам

- `assets/geomask-node-audit.py`: сохранить CLI фасад; state/save/job/revision/preview/apply/history/schedule dispatch. Не раздувать дальше весь алгоритм в одном файле.
- `assets/geo-backend.sh`: `cmd_nodes_paths_get`, `cmd_nodes_path_save`, `cmd_nodes_path_check`; добавить owner-side handlers новых API. `_slot_apply`, `cmd_sub_del` и связанные назначения источников обязаны согласовать source binding/applied invalidation с новым владельцем пула.
- `assets/geo-sync-page.js`: карточки state/progress/preview/apply/history/schedule; старый ввод/кнопки убрать или однозначно назвать после миграции.
- `tools/geo-rpc.json`, `tools/gen-geo-rpc.py` → generated regions `assets/geo-sync`, `assets/acl-geo-sync.json`, backend dispatch, JS declarations. Generated sections вручную не править.
- `assets/geomask-node-scan.sh`: managed paths больше не меняет через `apply_filter_all`; old `--ai-guard` не вращает production и не пишет независимо owned filter. Legacy mode только для явно ещё не мигрированных путей, со сроком удаления.
- `assets/geomask-lane.sh`, `assets/geomask-torrent.sh`, `assets/sync_mtrickle_mihomo.sh`, `assets/geomask-ytwatch.sh`: фактические записи общего config/зависимых rulesets/reload подключить к одному transaction coordinator. Необходим окончательный grep writers/open/replace/curl перед кодированием этапа E.
- `geomask.sh.in`: установка новых модулей/приватных директорий, backup include, миграции, managed ownership/cron/procd, fresh-install/reinstall/uninstall с сохранением пользовательского.
- `build.py`: встраивание новых assets, никаких ссылок на отсутствующие файлы; `geomask.sh` только generated.
- `tools/safe_write.py`, `assets/safe_write.sh`, `assets/safe_write_cli.py`: неполные прототипы не подключать «потому что уже есть». Для реального mihomo-ресурса привести к adapter/одному owner либо оставить явно dormant вне production; никаких двух competing helpers с разными locks.
- `.github/workflows/ci.yml`: build-drift check до build, полный unit/RPC/embedded-Python/JS/shell/sandbox; зависимости явно. Автотесты не обращаются к живым подпискам/роутеру.
- `docs/NODE-AUDIT.md`, `docs/SPEC-PATH-EGRESS-AUDIT-2026-11-08.md`, основной v2 и общие `../docs/PROGRESS.md`, `../docs/project/OWNERSHIP.md`: фактические контракты, эксплуатация, ограничения, результаты приёмки.

### Новые source assets — предлагаемые границы

- `assets/geomask-audit-state.py`: schema migration, per-path/source/node identities, manifests/CAS.
- `assets/geomask-audit-events.py`: redaction, append/history/retention/export.
- `assets/geomask-audit-probes.py`: adapter registry, evidence parse, isolated context/resource lifecycle.
- `assets/geomask-audit-apply.py`: pool compiler, preview, exact-target ownership, admission/fail-empty strategy.
- `assets/geomask-config-tx.py`: единственный owner config/pool/ruleset transaction, readback/recovery/lock/rollback.
- `assets/geomask-audit-scheduler` и при необходимости procd init/recovery hook: очередь, расписание, startup recovery, ограниченное наблюдение активного applied manifest. Конкретный process model выбрать после resource trial на целевом роутере, не добавлять daemon ради одного Save.

Способ загрузки hyphen-named модулей и install paths зафиксировать в локальной спецификации до реализации (existing source naming не даёт права выдумать import). Новые модули не меняют shared source API без адаптеров и тестов.

### Тесты

Продолжить `tests/test_node_audit.py`, `tests/test_node_audit_lifecycle.py`; новые focused suites для state migration/history/probes/preview/apply/transaction/schedule/RPC/UI. Старые sandbox regex-проверки заменить поведенческими только с сохранением их существенного покрытия. Browser fixture не выдавать за проверку настоящей авторизованной LuCI-сессии.

## 7. Этапы выполнения и приёмка

Для каждого этапа: сначала контракт и отрицательные тесты → воспроизводимый FAIL → минимальная реализация → PASS + независимая рецензия. Никакого live Apply до E/F. Во время планирования эти шаги не выполнялись.

### A. База, авторитетность и миграционная спецификация

- [ ] Снять новый read-only inventory исходников и установленного кода, сохранить diff dirty tree и хеши; не предполагать, что номер1.25.3 означает parity.
- [ ] Зафиксировать в отдельном коротком контракте schema2, owner changes, автономность и все входы выбранных путей. Окончательно перечислить writers/reloaders общего ресурса.
- [ ] Не переносить старые тестовые/агентские значения стран поверх того, что пользователь сохранил к моменту миграции.
- [ ] Согласовать границу обязательных AI-сервисов: минимум Google+ChatGPT; остальные существующие обязательные сервисы из политики не исчезают молча. Cloudflare — контроль, не подмена Google.

**Приёмка:** есть однозначный контракт, перечень migration preimages и карта зависимостей, без новой живой политики.

### B. Независимое состояние путей

- [ ] Миграция schema1→2 сохраняет все три текущих значения/неизвестные совместимые поля, комментарии иных конфигов не затрагиваются.
- [ ] Ввести path_revision и fingerprints только действительных зависимостей. Whole config hash остаётся safety CAS для общего commit, но не единственным ключом валидности отчёта.
- [ ] Разделить desired/report/applied/runtime-state; GET вычисляет drift, не постоянно hardcoded applied:false/true.
- [ ] Уметь обнаружить переназначение источника, удаление подписки, тот же node name с новым endpoint, смену shared provider и ручные изменения config.

**Тесты:** save VPN не инвалидирует noP2P report; общая source смена инвалидирует все действительно зависимые пути; rebind не переносит approval; no-op/STALE/atomic-error/duplicate-json/unknown-key preservation сохраняются.

### C. История и диагностика действий

- [ ] До расширения функций добавить события Save и существующих Check, включая отказ и no-op; сейчас именно их не хватает для ответа «что я нажал».
- [ ] У каждого job immutable report и устойчивый ID; последний указатель не уничтожает историю.
- [ ] Ограничения хранения, permissions, редактирование секретов, пагинация, экспорт. Новые endpoints не читают произвольные файлы по пути из браузера. Убрать запись полного subscription URL из `_slot_apply` и проверить аналогичные существующие log sinks; старые приватные журналы не копировать в public export, не уничтожать задним числом без отдельного решения.
- [ ] При reboot running job становится interrupted/recovered, не бесконечно «идёт»; история терминальных операций доступна в пределах retention.

**Приёмка:** после сохранения/проверки двух путей видны две отдельные цепочки событий с временем/изменениями/результатом; секретов нет, flash budget соблюдён.

### D. Достоверные проверки и жизненный цикл job

- [ ] Adapter contract: `probe(node_identity, service_id, context) -> status/country/observed_ip/evidence/method/version/observed_at/limit`, без выдуманных полей.
- [ ] Отдельно endpoint/provider-fetch failure, transport failure, service-geography UNKNOWN и подтверждённая запрещённая страна. Повтор/таймаут/лимиты не делают узел допустимым.
- [ ] DNS context измерения должен соответствовать объявленному контексту применения: remote DNS тест не доказывает, что actual client destination не подменён hosts/локальным resolver. Предпросмотр помечает PATH_DNS_MISMATCH и требует устранить конкретное несоответствие до подтверждения production-пути, а не банит все здоровые узлы/не меняет DNS сам.
- [ ] Для Google исследовать и проверить Google-owned геопризнак. Документированный кандидат — чистый Search footer с указанием IP-derived location, browser geolocation denied/cookies absent; CAPTCHA/отсутствие country source = UNKNOWN. Не принуждать gl/uule/регион и не принимать список preferences как факт.
- [ ] Если достоверный автоматический Google adapter на целевом стенде не получается, это явно незакрытый технический gate **полного AI Apply**, а не повод заменить US на HTTP204. Для VPN реализация продолжается независимо. Возможное изменение AI-критерия — только отдельным решением владельца.
- [ ] Жёсткий общий бюджет задания включая fetch/startup/probes/cleanup; heartbeat, cancel, queued/busy и restored-after-reboot. Сначала последовательное выполнение; повторная загрузка общей подписки в одном цикле не должна давать разным путям разные «источники» без явного объяснения.
- [ ] Отчёт фиксирует payload digest и соответствие runtime source. Если подписка поменялась, перепроверка/новый preview, а не применение по совпавшему имени.

**Приёмка:** отдельные real egress результаты и причины воспроизводимы; production node/config unchanged during Check; все ресурсы/дочерние процессы очищаются при cancel/timeout/ошибке; никакого service-wide US по одному чужому trace.

### E. Единая транзакция общего config-ресурса

- [ ] Один lock/revision/transaction journal для всех участвующих config writers/reloaders. Исторический отдельный lock нового Apply не защищает от старого scanner/slot/lane/torrent.
- [ ] Кандидат строится вне критического участка по immutable preimage; внутри lock повторно сверяется актуальность. Старый scan/preview не перетирает ручную правку.
- [ ] Уникальные candidate paths и restrictive permissions; validate именно кандидат `mihomo -f <candidate> -t`, не старый live config.
- [ ] Runtime trial в /tmp: все listeners/controller/TUN/cache/rulesets/pool files изолированы, порты свободны, нет обращений к production credentials/control endpoint или записи production cache.
- [ ] Локальные предобразы существования/байтов/режима config+pool+manifest+journal до первого commit; независимый rollback с исключением гонки commit-vs-watchdog. Порядок multi-file commit и startup recovery — часть контракта, а не надежда на один os.replace.
- [ ] Reload с Bearer без секретов в argv/log, проверкой HTTP и deadline; затем readback config/hash + actual group members/selection + ограниченные active probes через production listener + соседние пути.
- [ ] При ошибке восстановить предобразы, reload, проверить runtime и modes/hashes; если восстановление не доказано, needs_recovery, не SUCCESS. Прерывание SSH/worker не отменяет восстановление. Durable journal/startup recovery покрывает перезагрузку; volatile /tmp-watchdog не называть powerfail safe.
- [ ] До нового commit каждый writer разрешает существующую pending transaction. Watchdog берёт тот же permanent lock, сверяет transaction ID и установленное поколение/hash; не восстанавливает предобраз поверх более нового успешного commit. Несовпадение — reconcile/needs_recovery. Staged files **и parent directories fsync** до durable intent/commit markers; для каждой точки падения определено авторитетное поколение.
- [ ] Boot recovery/integrity/expiry gate выполняется **до открытия managed listeners/групп**: не только поздний procd postcheck после запуска. Неопределённая транзакция, неподтверждённая целостность, недостоверное время после boot/откат часов/NTP unavailable → BLOCKED до безопасной reconciliation. Проверить отсутствие раннего fail-open окна, а не только конечное состояние через минуту после boot.
- [ ] Убедиться, что rollback не возвращает заведомо запрещённый путь: политика исходной транзакции остаётся действующей до проверенного commit новой; если исходное состояние уже нарушено, использовать проверенный blocked-state/needs_recovery, не раскрывать DIRECT.

**Приёмка:** воспроизводимые interleavings всех writers, API401/reload fail/trial fail/diskfull/process death/reboot recovery; ни один failure не оставляет ложный applied-state. Без этого этапа F не включать.

### F. Предпросмотр и настоящее применение выбранного пути

- [ ] Первый live Apply разрешён только после **атомарного включения ownership gate E3–E5**: старые scanner/ai-guard/slot и другие writers либо переданы coordinator, либо явно отклоняют запись owned fields. Нельзя выпустить промежуточный F с конкурирующим старым владельцем; H меняет расписания, а не впервые устраняет эту гонку.
- [ ] Скомпилировать exact set допустимых node identities из свежего report и текущей политики. UNKNOWN не входит; пользователь не может включить его обходным raw JSON.
- [ ] Предпочесть **immutable managed file provider на путь/поколение**: копия только проверенных proxy definitions с текущими endpoints и digest, а не regex над auto-updating HTTP source. Исходная подписка остаётся источником для следующего аудита, но новые/изменённые endpoints не попадают в active pool до проверки и явного/разрешённого autoapply.
- [ ] В контролируемой группе изменить только её owned source/filter composition. Другие группы на той же исходной подписке, rules и listeners неизменны. Если совпали имена разных endpoints, доказать корректный namespace/mapping на mihomo или отказать, а не сливать по имени.
- [ ] Явно разобрать existing filter/exclude-filter/ASN/name bans: unmanaged ограничения сохраняются; legacy scanner-owned ограничения мигрируют в объяснимый per-path owner по preview. Если они исключают часть допустимых стран, показать отдельную причину и effective final pool. Не смешивать regex произвольным split('|').
- [ ] Preview содержит old/new path state, source digest, список изменений, current-node impact, количества и предупреждение о соединениях; сохраняется сервером под preview_id с digest, сроком и зависимостями. Браузер не присылает готовый YAML.
- [ ] Apply обязан отклонить stale preview, изменившуюся подписку/политику/endpoint, другой path ID, повтор старой транзакции. `preview_id` — серверный idempotency key, связанный с path/source/candidate и одной transaction. Double-click/retry возвращает тот же job/исход, а не второй commit; уже устаревший preview нельзя повторно превратить в новую транзакцию.
- [ ] Пустой пул: не создавать пустой provider с неизвестным fallback. Подготовить, trial и фактически проверить блокировку выбранного logical path (например, явная REJECT-политика группы, если подтверждена на целевой версии). Первый переход к блокировке — явное подтверждение в preview. Соседние пути не блокируются.
- [ ] При исключении текущего узла выбрать только допустимый узел того же источника/пути; старые недопустимые активные соединения выявить и закрыть scoped, не глобально flush conntrack. При blocked path убедиться в прекращении уже открытых недопустимых соединений.
- [ ] Подтвердить соответствие disk+runtime+applied manifest. Сменить applied_revision только после проверки. Отдельно выявлять drift от внешних правок/controller update/restart.
- [ ] Определить сроки годности service proofs и действия при истечении/отзыве ДО live. Freeze provider предотвращает непроверенные новые endpoints, но не гарантирует неизменность egress у провайдера. Нельзя обещать вечную US/географию по одному trace. Заранее установленный автономный enforcement при expiry должен блокировать поддерживаемый путь без ПК/агента; если logical groups на общем ingress нельзя так безопасно разделить, это отдельный обязательный технический gate/контракт маршрутизации, не скрытая правка WAN и не «best effort» SUCCESS.

**Приёмка:** после Apply live members точно соответствуют preview approved set; source auto-refresh не добавляет непроверенных узлов; запрещённый текущий узел/соединения не остаются; empty/all-UNKNOWN не раскрывает другой путь. В UI есть фактическое время runtime verification.

### G. Завершённая панель

- [ ] Человекочитаемые источники, desired vs active, времена и явные ошибки/unknown/stale.
- [ ] Исправить сохранение двух карточек: не перерисовывать весь view с потерей несохранённого ввода; обновлять ревизию и состояние нужного пути.
- [ ] Poll job-status вместо фиксированного setTimeout/ручного угадывания; восстановить polling после reload страницы; reconnect не запускает новый job.
- [ ] Добавить отмену и историю; Apply доступен только для годного preview, понятные disabled reasons. Технический unsupported Google не выглядит как «все узлы хорошие, нажмите применить».
- [ ] Кнопка «Применить» интервала cron называется «Сохранить интервал синка», чтобы не путать с применением node policy.
- [ ] Показать различие «изменения сохранены», «проверка завершена», «фильтр действует», «путь заблокирован», «откат проверен» без двусмысленного зелёного сообщения.

**Приёмка:** Chromium реального LuCI view с проверкой console/network/payloads, затем авторизованная live LuCI-сессия: Save→Check→Preview→Apply→Refresh действительно показывает тот же active pool. Fixture отдельно обозначается как fixture. Не считать чтение nodes_paths_get тестом Save.

### H. Ночной цикл и вывод старой конфликтующей автоматики

- [ ] Настройки расписания по путям; общий scheduler последовательно ставит выбранные paths в очередь и не стартует второй mihomo параллельно. По умолчанию сохранить нынешнее время как предложение и **check-only**; check-and-apply включается отдельно явно после F.
- [ ] Ручной и cron-входы вызывают один код/модель/журнал, не два разных алгоритма.
- [ ] Source refresh, проверка, preview и (если разрешено) применение — одна последовательность с явными результатами per path; провал одного пути не делает весь цикл зелёным и не стирает успешные отчёты других.
- [ ] Стратегию при новом failed/expired evidence привязать к активной политике: check-only не добавляет новые узлы, но обязательный revocation/expiry guard не может сохранять заведомо недопустимый активный узел только потому, что autoapply выключен. Разделить «не расширять пул без подтверждения» и «прекратить запрещённую передачу».
- [ ] Перевести managed paths на нового владельца; удалить/отключить только его старые cron строки и `--ai-guard` entry points после сохранения snapshot/согласованной миграции. Legacy groups вне scope не ломать. Installer/reinstall не воскрешает старые writers.
- [ ] Procd/startup recovery восстанавливают scheduler/applied-state и показывают interrupted jobs. UI честно сообщает недоступные cron/service, следующее время запуска, last run и mode.

**Приёмка:** контролируемый ночной запуск + manual overlap + restart/reinstall: один owner, один runtime job, три самостоятельных результата, старый фильтр не возвращается.

### I. Полная проверка, документация и поставка

- [ ] Полный unit набор, RPC parity, embedded-Python compilation, shell/JS checks, sandbox, CI с явной PyYAML зависимостью; build drift проверить до regeneration в отдельной копии.
- [ ] Секреты/URLs/HWID/ключи не попали в event logs, manifests exports, snapshots публичных тестов или diff; proxy payloads остаются приватными.
- [ ] Fresh install/reinstall/upgrade/uninstall tests: migration идемпотентна, user policies retained, provider sources/managed generations coherent, no stale ownership resurrection.
- [ ] Независимая рецензия контрактов и негативных сценариев; фактические результаты/непроверенные условия записать, не переносить старые158 PASS как доказательство новой работы.
- [ ] Только затем агент делает свежий PC backup + code snapshots и согласованный поэтапный rollout, с паузой/проверкой каждого пути и готовым откатом. Не устанавливать весь dirty tree вслепую.
- [ ] Обновить реестр owners, инструкцию, прогресс и карту реальных интеграций; старую report-only спецификацию пометить историческим этапом, не держать два противоречащих актуальных плана одной функции.

## 8. Сквозная матрица приёмки

| Сценарий | Что считается успехом |
|---|---|
| Сохранены разные страны двух путей | Нет потери ввода/чужих значений, оба Save видны в истории; чужой report не stale без реальной зависимости |
| Shared subscription, разные политики | Разные approved pools, один путь не перезаписывает другой |
| Нажата «Проверить» | Production group/node/config не меняются; прогресс/отмена/история работают |
| Нажата «Применить» | Видимый preview → verified transaction → фактически ограниченный live pool; не только desired JSON |
| Тот же node name, изменён endpoint | Старое approval недействительно; новый endpoint не появляется в active pool |
| В подписке появился новый узел | До проверки/разрешённого Apply в active pool не попадает |
| Старый cron/AI guard стартовал | Не переписал managed state, нет production rotation при ручной диагностике |
| Google UNKNOWN, ChatGPT US | Нет общего AI admission; UI показывает конкретно неизвестную Google geography |
| Local DNS отличается от test resolver | Диагноз контекста, не ложное «AI US гарантирован» |
| Все узлы запрещены/UNKNOWN | Явный BLOCKED или отказ до первого commit с предупреждением; после явной активации строгой политики — проверенное отсутствие fallback/старых недопустимых соединений |
| API401/timeout/reload failure | Нет ложного success; rollback/runtime restoration проверены |
| Два Apply/смена подписки/save во время scan | Сериализация/CAS/STALE, новая версия не потеряна |
| Закрыли страницу/оборвался SSH/перезапуск питания | Независимое завершение/откат/recovery, понятный конечный статус |
| Log diskfull/retention | Нет бесконечного overlay growth; apply intent durable или отказ до записи; финальный статус восстанавливаем |
| ПК выключен | Save/Check/Apply/schedule не требуют его; агентское внедрение без PC backup не выполняется |

## 9. Исполнимые задачи и контрольные точки

Этапы A–I — milestones, не одна огромная coding-задача каждый. Ниже единицы для отдельной реализации/ревью. Обычно 2–5 рукописных файлов на задачу; generated RPC-файлы меняются вместе в одном контракте. Сначала focused regression, затем реализация и PASS. До согласования общих контрактов нельзя параллельно менять state schema и потребителей.

| Задача | Зависит от | Основные изменения | Проверяемый результат |
|---|---|---|---|
| A1 Инвентаризация и migration spec | — | Документы, snapshots/test fixtures без секретов | Owners/writers/ingress перечислены, scopes совпадают с источниками |
| B1 Per-path schema/CAS | A1 | state-модуль, фасад, focused state tests | Два независимых Save сохраняют соседние данные, stale того же пути отвергнут |
| B2 Source binding/node identity | B1 | state, фасад resolver, source tests | Managed provider не теряет исходную подписку, same-name/new-endpoint approval не наследует |
| C1 События Save и история в UI | B1 | events, фасад hooks, backend/RPC manifest, минимальная UI history | Два Save видны отдельно, пагинация/ACL/redaction проверены |
| C2 Immutable отчёты и bounded retention | C1 | events/state, job публикация, storage tests | Последние jobs не затираются, лимит overlay и reboot соблюдены |
| D1 Job API и polling/cancel | B2,C2 | фасад lifecycle, job RPC, UI progress, lifecycle tests | Отмена/перезагрузка страницы/зависший процесс дают понятный терминальный статус |
| D2 Изоляция adapters/DNS evidence | D1 | probes-модуль, фасад adapter hooks, probe fixtures | Remote DNS не выдан за actual client context, TLS/таймаут/cleanup отрицательные тесты |
| D3 Google-proof feasibility gate | D2 | Исследовательский отчёт и adapter-fixtures, при подтверждении adapter | Доказан Google-owned IP country либо конкретный незакрытый gate, без превращения204 в US |
| E1 Протокол config transaction | A1,C1 | config-tx, focused tx tests | Permanent flock/CAS/validate/trial/intent и rollback без live |
| E2 Runtime postcheck/watchdog/recovery | E1 | config-tx, recovery hook, failure-injection tests | Reload401/kill/reboot не оставляют ложный applied; rollback действительно проверен |
| E3 Slot и legacy scanner/AI guard ownership | B2,E2 | backend source setter, old scanner, ownership tests | Переназначение не обходит admission, старый scanner не трогает managed groups |
| E4 Lane/torrent writers | E2 | lane/torrent wrappers, tx adapters, overlap tests | Изменения listeners/rulesets не гоняются с Apply и не раскрывают restricted путь |
| E5 Sync/reload/installer ownership | E3,E4 | sync/ytwatch relevant reload hooks, installer, integration tests | Reload не активирует половину транзакции, reinstall не возвращает competing writer |
| F1 Managed pool compiler и Preview | B2,D2,E2 | apply-модуль, preview RPC/UI, compiler tests | Точный per-path snapshot/diff, сохранены unmanaged restrictions, shared provider независим |
| F2 Apply/blocked-state и runtime status | F1,E5 | apply↔tx orchestration, apply/status RPC, apply tests | Actual runtime pool совпал, empty/allUNKNOWN не раскрывают DIRECT, статус truthful |
| F3 Expiry/revocation и connections | F2 | applied reconciliation/guard, tx scoped connection handling, enforcement tests | Expired/отозванный узел не остаётся разрешённым; смерть guard/boot проверены в поддерживаемой матрице |
| G1 Полный UI цикл | C2,D1,F2 | view/UI fixtures, браузерный сценарий | Save→Check→Preview→Apply→Refresh, две карточки без потери draft |
| G2 UX ошибок/истории/drift | G1,F3 | view и E2E tests | Offline/rollback/UNKNOWN/blocked/history различимы, нет fake success |
| H1 Check-only расписание | D1,E5,G1 | scheduler, schedule RPC/UI, scheduler tests | Одна очередь, явные per-path времена/результаты, без скрытого Apply |
| H2 Явный autoapply и старые cron migration | H1,F3,G2 | scheduler, installer, миграционные тесты | Только разрешённые paths/modes; новое недопустимое доказательство не игнорируется |
| I1 Fresh/reinstall/upgrade/uninstall | H2 | installer/build integration, sandbox fixtures | Stateful миграция и sysupgrade/recovery покрывают новые данные |
| I2 Общая регрессия и независимый audit | I1 | CI/test integration, актуальные документы | Всё проверено свежим запуском, секреты не утекли |
| I3 Поэтапный rollout и настоящая LuCI приёмка | I2 | Существующие deployment/check scripts и evidence | Новый PC backup; реально заданный active pool/реальный браузер/соседние пути/откат |

D3 и F3 — высокорисковые gates: feasibility/trial выполнять рано после минимальных зависимостей; не откладывать неизвестность до последнего дня UI-полировки. Отрицательный результат D3 не блокирует готовность VPN/noP2P, но блокирует обещание законченного полного AI. Нельзя считать исчерпание исследовательской сессии решением проблемы.

**Контрольные точки:** после B2/C2 — сохранение+история; после D2/D3 — достоверность измерений; после E5/F3 — безопасность реального Apply; после H2/I2 — end-to-end/автоматика; после I3 — пользовательская приёмка. На каждой независимое чтение diff/контрактов и подтверждение нужного тестового вывода; live rollout не совмещать с неподтверждённым исследованием.

### Команды локальной проверки (для будущего исполнения, не запускались в задаче планирования)

Из корня geomask на текущем ПК:

```powershell
& 'C:\Python\Python314\python.exe' -m unittest discover -s tests -p 'test_node_audit*.py' -v
# Добавлять соответствующий focused suite из новой задачи, затем весь набор:
& 'C:\Python\Python314\python.exe' -m unittest discover -s tests -p 'test_*.py' -v
& 'C:\Python\Python314\python.exe' tools/gen-geo-rpc.py --check
& 'C:\Python\Python314\python.exe' tools/check_build.py
& 'C:\Program Files\Git\bin\bash.exe' -n geomask.sh
node --check assets/geo-sync-page.js
& 'C:\Program Files\Git\bin\bash.exe' tests/run_tests.sh
```

Проверять `$LASTEXITCODE` отдельно сразу после каждой программы, без `tail`/bash-quoting в PowerShell. Failed проверку расследовать, не маскировать следующим успешным действием. `check_build.py` сверяет generated installer; штатный `build.py` выполняется при подготовке изменённой поставки, а не тайно перед доказательством отсутствия drift. Browser tests использовать существующий workspace venv/Edge; реальный authenticated LuCI тест отдельно. Эти команды не заменяют failure injection и live отрицательные сценарии F/E.

## 10. Когда функцию можно назвать законченной

Все A–I пройдены в поддерживаемой матрице. Владелец в админке самостоятельно сохраняет разные страны, проверяет каждый путь, видит причины/историю, применяет результат и после обновления видит именно этот рабочий пул. Ночная работа не возвращает старые фильтры. Нельзя поставить «готово», если Apply всегда BLOCKED из-за незавершённой транзакции, Google всё ещё неподтверждён для заявленного полного AI admission, история отсутствует, или проверен лишь fixture.

**Явные технические gates, не скрывать:** Google-specific достоверное измерение; supported fail-closed/expiry enforcement всех входов выбранного пути; координация всех config/reload writers и надёжное восстановление; доказанная поддержка immutable file provider/имён/групп на используемой версии mihomo. Пока gate не пройден, остальные этапы можно реализовывать и проверять локально, но соответствующий live Apply не включается и весь функционал не объявляется завершённым.

**Вопросы перед live (не повод менять код уже в плановой задаче):** утвердить сроки годности измерений и retention budget; финальный обязательный реестр AI-сервисов; окно применения строгой политики и допустимое прерывание соединений. Уже принятые решения об автономности, отдельном Check/Apply и странах по фактическому выходу заново не спрашивать.
