# Разработка

## Сборка

Установщик `geomask.sh` **генерируется** из шаблона и ассетов:

```sh
python build.py                     # geomask.sh.in + assets/* -> geomask.sh
python tools/build_module_index.py  # modules/index.json (каталог модулей)
python build.py                     # ещё раз: каталог встраивается в установщик
python tools/check_build.py         # geomask.sh == сборка из исходников
```

Порядок важен: `build_module_index.py` встраивает каталог в `geomask.sh`, поэтому после него
нужна повторная сборка. `check_build.py` пересобирает во временный файл и сравнивает sha256.

`build.py` подставляет содержимое файлов `assets/*` в плейсхолдеры `@@ASSET:<name>@@` шаблона
(сейчас 45 ассетов: панель, бэкенд, rpcd, ACL, меню, sync/guard, аудит узлов, диагностика,
правка списков, обновление модулей, init-скрипты AI-DNS, списки и правила). Соответствие
«ассет → файл» задаётся словарём `MAP` в `build.py`.

**Правило:** править `geomask.sh.in` и `assets/*`, затем пересобирать `geomask.sh`; не
редактировать `geomask.sh` вручную.

**Инвариант байтов:** установщик пишет ассет через heredoc — текст (LF) без хвостовых
переводов строк плюс ровно один в конце. Каталог модулей хеширует те же байты; расхождение
на один байт делает все модули «изменёнными» после обновления. Держится тестами
`tests/test_module_index.py`.

## Стиль кода

- Скрипты на роутере — POSIX `sh` (busybox), без bash-измов.
- Идемпотентность: повторный запуск не ломает состояние.
- Никаких `reboot` и изменений `network`; полный бэкап перед записью.
- Секреты не попадают в репозиторий (только аргументами, остаются на роутере).

## Тесты

```sh
python -m unittest discover -s tests -p 'test_*.py'   # 521 unit-тест
bash tests/run_tests.sh                               # песочница: 430 проверок
```

Песочница — fake root со стабами `pidof/ubus/mihomo/curl/crontab/nslookup/ip/wget/apk`:
работает без роутера и без VM. `tests/` — публикуемая часть (в git, гоняется в CI);
`sandbox/` — локальная копия, в git не входит. Runtime-артефакты (`last_out.txt`, `state/`,
`root_*`, `freshbin/`) не коммитятся.

Сценарий `S54` проверяет установку на голом роутере: пустой корень с `apk`, `mihomo` и
`magitrickle`, но без `python3`/`curl` — шаг 1 должен поставить их сам.

Сценарии безопасности установки (v1.30.15):

- `S55` — на роутере чужой конфиг mihomo: `--auto` останавливается и файл не меняется;
- `S56` — `https-dns-proxy` не ставится: шаг 4 не трогает DNS, сеть и nft;
- `S57` — `--modules=core,path_vpn` без панели: verify проходит без `geo-sync`;
- `S58` — `sysupgrade -b` не работает: apply не начинается, файлы не пишутся.

Заглушки, которые нельзя считать доказательством: `mihomo` отвечает по state-файлу и не
читает YAML, `netstat` всегда показывает слушателей AI-DNS, `sysupgrade` пишет пустой
архив. Поэтому поведение проверяется ещё и по извлечённым функциям и по содержимому
файлов, а не только по коду возврата.

Перед добавлением файлов в `tests/` — проверка на секреты:
`grep -riE 'x-hwid|subscription|proxy-provider|http://sub|token|key' tests/`.

## CI (GitHub Actions)

`.github/workflows/ci.yml`, `ubuntu-latest`, триггеры `push`/`pull_request` в `master`:

1. `python tools/check_build.py` — сборка == закоммиченный `geomask.sh`;
2. `python tools/gen-geo-rpc.py --check` — четыре точки интеграции RPC совпадают с манифестом;
3. `sh -n geomask.sh` и `node --check assets/geo-sync-page.js` — синтаксис;
4. `python -m py_compile` для всех поставляемых python-модулей;
5. `python -m unittest discover -s tests -p 'test_*.py'`;
6. `bash tests/run_tests.sh`.

## Релиз

Полный порядок — в [RELEASING.md](RELEASING.md). Коротко:

```sh
python build.py && python tools/build_module_index.py && python build.py
python tools/sync_module_versions.py --write     # версии модулей из истории
# поднять GM_VERSION в geomask.sh.in, пересобрать
python tools/release.py publish                  # тесты -> тег -> релиз -> сверка digest
python tools/release.py verify --full            # скачать ассет и прогнать песочницу на нём
```

Роутер принимает релиз только когда `GM_VERSION` = тег и sha256 = digest ассета.

## Структура

```
geomask.sh.in     шаблон установщика
geomask.sh        собранный артефакт (в git)
assets/           встраиваемые файлы (45)
build.py          сборщик
modules/          манифесты модулей + index.json
tools/            build_module_index, check_build, gen-geo-rpc, release, sync_module_versions
tests/            публичная песочница + unit-тесты
sandbox/          локальная песочница (не в git)
docs/             справки, release notes, исторические планы
templates/        метаданные шаблона списков
scripts/          export_lists.py
.github/          workflows/ci.yml
```
