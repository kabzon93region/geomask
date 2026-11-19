# Статус плана массовой готовности (18.11.2026) — матрица доказательств

Состояние на 19.11.2026, ветка `master` (52 коммита впереди `origin/master`). Каждая строка
плана сопоставлена с кодом/тестом/коммитом. **CLOSED-LOCAL** = исправлено и доказано на
Windows-хосте (unit/sandbox/композиция, полный gate); **OPEN-STAND** = нужен изолированный
Linux/OpenWrt-стенд; **OPEN-OWNER** = нужно решение владельца или его артефакт. Ни один
стендовый пункт не считается закрытым по локальным тестам.

## Подтверждённые дефекты

| ID | Статус | Доказательство |
|---|---|---|
| U01 | CLOSED-LOCAL | `ff51cc7`: приватный кэш, allowlist destinations, отказ подложенному каталогу (`test_module_update` 64) |
| U02 | CLOSED-LOCAL | `553ae7a`: CAS повторно проверяется в commit, edit между stage/commit без `force` не теряется |
| U03 | CLOSED-LOCAL | `553ae7a`: symlink identity → pre-mutation отказ либо точное восстановление |
| I01 | CLOSED-LOCAL | `2296e3f`: single-tunnel verify проходит; `_step7_groups` требует группу только если она объявлена |
| I02 | CLOSED-LOCAL | `2296e3f`: backend IPv6 переехал в `core`; reinstall не вызывает apply при initially-off |
| I03 | CLOSED-LOCAL | `f8f8c56`: все crontab-записи под локом, restore читает внутри лока |
| I04 | CLOSED-LOCAL | `f8f8c56`: restore сравнивает внутри лока, отказ лока = незавершённый откат |
| I05 | CLOSED-LOCAL | `4a22dc4`: `--adopt-config` = реальный merge + `mihomo -t` + отказ до записи (`test_adopt_config` 13) |
| I06 | CLOSED-LOCAL | `23371a5`: prompt в stderr/TTY, stdout только URL |
| I07 | CLOSED-LOCAL | `1c564d2`: dependency closure по манифесту (`module_deps` + тест) |
| I08 | CLOSED-LOCAL | `7ab907b`: `STEP2_FILES` = ровно 27 путей, шаг 2 всегда применяется (`test_step_freshness` 4) |
| I09 | CLOSED-LOCAL | `2848547`/батч 1: unique mkstemp-времена, размер проверяется после publish |
| I10 | CLOSED-LOCAL | `bd2a250`: повреждённый выбор останавливает entrypoints, атомарная запись |
| I11 | CLOSED-LOCAL | `cc4c14c`: prune по `owned_resources` (файлы/сервисы/cron/nft), uninstall использует тот же помощник |
| I12 | CLOSED-LOCAL | `bd2a250`/`23371a5`: сбой шага больше не возвращает 0, `SAVE_FAIL` учитывается |
| C01 | CLOSED-LOCAL | `42c64c7`: rollback только при собственном postimage, `--expect-sha256` |
| C02 | CLOSED-LOCAL | `bd81548`: state до правила, сбой записи снимает правило и сообщает шаг |
| C03 | CLOSED-LOCAL | `bd81548`: A→B удаляет собственные прежние правила |
| C04 | CLOSED-LOCAL | `16c3ed7`: include → reload → read-back, `state=pending`, RPC `ok:false`, повтор на идентичном прогоне |
| C05 | CLOSED-LOCAL | `bd81548`: `_gm_route_rule_ensure`, rc каждого add, `77` при неудаче |
| C06 | CLOSED-LOCAL | `c0eb516`: сохранение/восстановление по интерфейсам, исчезнувший интерфейс пропускается |
| C07 | CLOSED-LOCAL | `16c3ed7`: pending-маркер, повтор, rc 1, все четыре флага в state |
| C08 | CLOSED-LOCAL | `bd81548`: удаление только по `pref`+`from`+`lookup`, чужое правило живёт |
| C09 | CLOSED-LOCAL | `bd81548`: нечитаемая таблица = unknown, не «успех» |
| C10 | CLOSED-LOCAL | `ce45ff1`: короткие `api_key`/`access_token`/`client_secret`, camelCase/разделители (`test_diag_redact` 37) |
| C11 | CLOSED-LOCAL | `42c64c7`: slot отдаёт кандидата, живой файл пишет только TX с CAS |
| R01 | CLOSED-LOCAL | `fd28828`: publish через testgate, `--verify-only`, `verify` без `--full` не обещает установку |

## Потенциальные риски / gates

| ID | Статус | Что осталось |
|---|---|---|
| P01 | CLOSED-LOCAL (код) + OPEN-STAND | `5cd5112`: fail-closed таблица, conntrack, восстановление; netns-egress — стенд |
| P02 | OPEN-STAND | совместимость old/new helper и changed hash на реальных модулях |
| P03 | CLOSED-LOCAL (код) + OPEN-STAND | `2d6ad2a`: maintenance-lock + `activation`; SIGKILL/parallel installer — стенд |
| P04 | CLOSED-LOCAL (документ) + OPEN-OWNER | [`docs/decisions/P04-trust-channel.md`](../decisions/P04-trust-channel.md): каналы, проверки, release gate; подпись каталога/канал по умолчанию — решение владельца |
| P05 | OPEN-STAND | alias absent vs invalid на netifd |
| P06 | CLOSED-LOCAL (код) + OPEN-STAND | `734b97f`: persistent парк + отметка до первого `mv`, возврат до ротации; ENOSPC/owner data по этапам — стенд |
| P07 | OPEN-STAND | настоящий rpcd/ubus, роли ACL, полный retrieval до 2 MiB с base64 |
| P08 | OPEN-STAND | late failure после смены runtime: graph/registry/argv/egress |
| P09 | CLOSED-LOCAL (код) + OPEN-STAND | `734b97f`: kill после парковки восстанавливается, исчезнувший парк = отказ; reboot/power loss — стенд |
| P10 | CLOSED-LOCAL (порядок) + OPEN-STAND | `1aef885`: hook до старта mihomo; already-up/empty queue и LAN dataplane — стенд |
| P11 | CLOSED-LOCAL | `c0bf739`: startup reconcile, catch-up по digest содержимого |
| P12 | CLOSED-LOCAL | `1f7baba`: `head -c cap+1`, placeholder «response too large» |

## Прежние OPEN (таблица G/N)

G02 — `1f7baba` (трёхзначный AND/OR, unknown≠false). G03 — `3c14bfd` (проверяемые dhcp-записи).
G05 — **OPEN-STAND/OWNER**: отдельные hev-инстансы/fixed listeners/egress — архитектурное
решение, требует стенда и согласия владельца. G06 — `29c886f` (точное владение правилами).
N04 — `32db49f` (нечитаемый конфиг ≠ чужой). N08/N10/N12 — не блокеры, остаются как есть.
B-020 — `2422295` (бюджет 15 с, partial, probe path). D14 — исключён планом (новая политика).

## Задачи плана

| Задача | Статус |
|---|---|
| 1 updater trust/integrity (U01–U03/P02–P04) | код CLOSED-LOCAL; P04 закрыт документом; P02/P03 — стенд |
| 2 install lifecycle (I01/I02/I05–I12/N04) | CLOSED-LOCAL целиком |
| 3 writers/lifetime rollback (C01/C11/I03/I04/I09) | CLOSED-LOCAL |
| 4 console/routes (C02/C03/C05/C08/C09/P01/G06) | CLOSED-LOCAL; P01-egress — стенд |
| 5 DNS/rulesets (C04/C07/G02/G03/G05/B-020/P10/P11) | CLOSED-LOCAL, кроме G05 и P10-dataplane — стенд |
| 6 IPv6 (C06/P06) | код CLOSED-LOCAL; kernel/reboot — стенд |
| 7 diagnostics (C10/P07/P12/R07) | C10/P12 CLOSED-LOCAL; P07 — стенд; R07 — стенд |
| 8 tooling/recovery (R01/P06–P10) | R01/P06/P09 CLOSED-LOCAL; P06/P09-power-loss, P08, CI-187, матрица SHA — стенд/владелец |
| Этап 0 (0а–0д) | 0а/0б/0в/0г/0д CLOSED-LOCAL; gate этапа 0 — стенд |

## Доказательный baseline

- Полный gate (`testgate --timeout 4200`): **97 модулей, 1427 тестов, 1397 выполнено,
  30 allowlisted skip, 0 fail, 0 timeout** — лог `gate35`, запись в
  [execution log](<superpowers/plans/2026-11-18-execution-log.md>).
- Сборка/паритет (после P04/P06/P09, HEAD `5a55c93`): `check_build --verify-only` → ok,
  `geomask.sh` sha256 `12e94ff9980ecf0a498325e655d2ac46b8bb712362483c3faa3d063d2e1d1fc1`
  (продуктовый коммит `734b97f`), `gen-geo-rpc --check` → ok, `compose_sources --check` → ok
  для 16 целей, `repo_check --strict` → 0 authored oversize (2 объяснённых исключения),
  композиция из чистого `git archive` → байт-в-байт (партия 19: 1 676 554 байта,
  тот же sha256, `check_build --verify-only` ok). Актуальная сборка после правок
  19.11.2026 — `705eea0d…` (`geomask.sh` 1 682 670 байт, `check_build` ok).
- Стендовые проверки и порядок их выполнения — [STAND-GATES-RUNBOOK](<../STAND-GATES-RUNBOOK.md>).
## Живой прогон 19.11.2026 (канарейка)

[ROUTER-CANARY-EVIDENCE-2026-11-19](../stand/ROUTER-CANARY-EVIDENCE-2026-11-19.md): три
итерации на реальном OpenWrt 25.12.5, финальная — установка 1.31.0 завершилась rc=0,
`--check` после — `pass=6 fail=0`, монитор 10/10 здоров. Найдены и исправлены два дефекта,
которые не мог поймать Windows-хост: padding mark в nft (`eb6cd55`) и отсутствие `stat(1)`
в busybox (`e3f787d`). Дополнительно 19.11 выполнена **переустановка `--reinstall`** на этом же роутере
(`canary-20261119-194522`): четыре ложных отказа preflight найдены и исправлены
(`0def5d0`, `f26a941`, `e8b5af8`), успешный прогон 70 с, `verify: PASS`, монитор 10/10,
после reboot — всё здорово. Это закрывает «реальную переустановку» для текущего профиля,
но не заменяет failpoint-матрицу phase2a–2f. Живой прогон **не закрывает** стендовые строки:
power loss, ENOSPC, G05, реальный rpcd/ACL, установка обновлений модулей и матрица профилей
остаются OPEN-STAND.

- Порядок закрытия стендовых пунктов без собственного стенда:
  [ROUTER-CANARY-PLAN](../stand/ROUTER-CANARY-PLAN.md) (канареечный прогон на живом роутере
  с авто-откатом) и [AGENT-PROMPT-LINUX](../stand/AGENT-PROMPT-LINUX.md) (Linux unit-gate на
  отдельной машине). Это планы, не evidence: строки выше остаются OPEN до логов прогонов.
