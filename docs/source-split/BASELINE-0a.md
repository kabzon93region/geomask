# Этап 0а — baseline и навигация (зафиксировано)

Снимок для будущего переноса исходников (этап 0). Паритет после переноса проверяется
**байт-в-байт** относительно этих чисел; сами числа не «истина» после правок — источник
истины всегда текущие файлы и их sha256.

- HEAD: `5d3fde953df1343045be711fb69f45cac64f8dcc`
- Снято: инструменты этого репозитория (`python -B tools/compose_sources.py --inventory`,
  `git rev-parse`, sha256sum в Python), без доступа к рабочему роутеру и `Z:`.

## Write-set: delivery-пути, которые переносятся в parts

| Файл | Байт | sha256 |
|---|---:|---|
| `geomask.sh.in` (installer template) | 300 072 | `d639b54239fefaeb749ecdae52a1c6debdcefae3faaea3d9b907b7fe9b733fb2` |
| `assets/geo-backend.sh` (backend) | 179 479 | `18ee09baed348da1f3102b6b7a305009dead24d073d43786058ec2a3b40eafe4` |
| `assets/geo-sync-page.js` (panel) | 121 345 | `6fd025b7d8f6e86d093ed5d0e8ff8ef44a9720c187ae0c038943dd0756c34297` |
| `geomask.sh` (generated installer) | 1 671 765 | `ecdb81065121fe826efbed790617bb703e7b490582b6c0e304cff8120fdd5a75` |

Первые три — будущие *generated compatibility views*: после 0в их содержимое обязано
совпасть с композицией parts. `geomask.sh` остаётся единственным download/install artifact и
не дробится.

## Source-size inventory (authored, ≥500 строк — индикатор, не запрет)

| Файл | Строк | Роль |
|---|---:|---|
| `geomask.sh` | 46 964 | generated |
| `geomask.sh.in` | 6 386 | installer (первая очередь) |
| `assets/geo-backend.sh` | 3 847 | backend (первая очередь) |
| `assets/geo-sync-page.js` | 1 930 | panel (следом) |
| `assets/geomask-doctor.py` | 1 977 | задача 5 |
| `assets/geomask-diag-spool.py` | 1 014 | задача 7 |
| `assets/geomask-module-update.py` | 973 | задача 1 |
| `assets/geomask-config-tx.py` | 943 | задача 3 |
| `assets/geomask-node-audit.py` | 931 | следующий проход |
| `assets/geomask-routes.sh` | 917 | задача 4 |
| `assets/geomask-dnsguard.sh` | 851 | DNS-работы |
| `assets/geomask-diag-redact.py` | 808 | задача 7 |
| `assets/geomask-apply.py` | 791 | задача 3 |
| `tools/module_manifest.py` | 656 | позже |
| `assets/geomask-audit-state.py` | 637 | позже |
| `assets/geomask-node-scan.sh` | 608 | позже |
| `tools/safe_write.py` | 567 | допустимо оставить целым |

## Baseline проверок (перед переносом)

- Полный gate `gate17` на этом SHA: 94 модуля, 1365 тестов (1335 выполнено, 30 allowlisted
  skip), 0 fail, 0 timeout, PASS; подробности — `docs/superpowers/plans/2026-11-18-execution-log.md`.
- Allowlisted skip-причины gate: `docs-absent, linux-only, needs-root, no-bash,
  no-native-symlink, no-python, no-pyyaml, posix-only, router-only, windows-acl`.
- Парность сборки: `tools/check_build.py --verify-only` → OK (sha256 `ecdb81065121fe82`),
  `gen-geo-rpc.py --check` → OK, `bash -n geomask.sh` → 0.
- Структурный перенос не должен ни исправлять, ни скрывать ни один U/I/C/R-дефект: в коммите
  0в нет bugfix/formatting/version bump.

## Generators и consumers, которых нельзя разъединить

| Инструмент | На что опирается |
|---|---|
| `build.py` | читает `geomask.sh.in` + `assets/*`, подставляет `@@ASSET:...@@`, ставит версии |
| `tools/build_module_index.py` | парсит literal MAP и блоки `save_stream`, хеширует байты (LF, один финальный newline) |
| `tools/gen-geo-rpc.py` | пишет dispatch/decl **прямо в assets**; после 0в должен писать выделенные parts, иначе появится второй writer |
| `tests/test_install_safety.py` | извлекает функции из собранного `geomask.sh.in` |
| `tests/test_module_index.py` | читает старые пути/байты каталога |

`python -B tools/compose_sources.py --check` — будущий немутирующий контроль: сейчас все три
target'а в режиме `monolith` (parts ещё не заведены), после 0в он сравнивает композицию с
delivery-файлом и называет первую разошедшуюся часть.

## Локальная навигация

Локальный CODE_MAP/index, который строит плагин DSH, из инструментов этой сессии
**программно недоступен** (нет поддерживаемого способа запросить его без самого плагина),
поэтому навигация шла по исходникам, манифестам (`modules/*.json`, `tests/reinstall_write_set.json`)
и `docs/DEVELOPMENT.md`. Дубль карты, cache или DB плагина не создавались и не коммитились —
это требование плана и сохраняется. После переноса (0д) карта обновляется штатным способом
плагина, а сборка от неё не зависит.
