# Разработка

## Сборка

Установщик `geomask.sh` **генерируется** из шаблона и ассетов:

```sh
python build.py        # geomask.sh.in + assets/* -> geomask.sh (chmod 755)
```

`build.py` подставляет содержимое файлов `assets/*` в плейсхолдеры `@@ASSET:<name>@@`:

| Плейсхолдер | Файл в `assets/` |
|---|---|
| `geo-backend.sh` | `geo-backend.sh` |
| `geo-sync` | `geo-sync` |
| `acl.json` | `acl-geo-sync.json` |
| `menu.json` | `menu-geo-sync.json` |
| `geo-main.js` | `geo-sync-page.js` |
| `sync_mtrickle_mihomo.sh` | `sync_mtrickle_mihomo.sh` |
| `ytdirect.yaml` | `ytdirect.yaml` |
| `99-default-vpn-route` | `99-default-vpn-route` |

**Правило:** править `geomask.sh.in` и `assets/*`, затем пересобирать `geomask.sh`; не редактировать
`geomask.sh` вручную.

**Проверка соответствия сборки** (TD-05): `python tools/check_build.py` — пересобирает и сравнивает
с закоммиченным `geomask.sh` (sha256), exit 1 при расхождении. Запускать перед каждым коммитом;
в CI — автоматически.

## Стиль кода

- Скрипты на роутере — POSIX `sh` (busybox), без bash-измов.
- Идемпотентность: повторный запуск не ломает состояние.
- Никаких `reboot` и изменений `network`; бэкап перед записью.
- Секреты не попадают в репозиторий (только аргументами, остаются на роутере).

## Песочница и тесты

Локальная песочница `sandbox/` (fake root + стабы `pidof/ubus/mihomo/curl/crontab/nslookup/ip/wget/apk`)
запускается без роутера и без VM/WSL:

```sh
bash sandbox/run_tests.sh     # ожидаемо: все PASS (S1..S11)
```

`tests/` — публикуемая часть той же песочницы (тот же `run_tests.sh` + стабы `bin/`): входит в git
и гоняется в CI. `sandbox/` остаётся локальным (`.gitignore`); runtime-артефакты (`last_out.txt`,
`state/`, `root_*`) не коммитятся ни оттуда, ни отсюда.

## CI (GitHub Actions)

`.github/workflows/ci.yml`, `ubuntu-latest`, триггеры `push`/`pull_request` в `master`:

1. `python tools/check_build.py` — сборка из исходников == закоммиченный `geomask.sh`;
2. `sh -n geomask.sh` — синтаксис;
3. `bash tests/run_tests.sh` — песочница (S1..S11).

Перед добавлением файлов в `tests/` — обязательная проверка на секреты:
`grep -riE 'x-hwid|subscription|proxy-provider|http://sub|token|key' tests/`.

## Релиз

1. Внести изменения в `geomask.sh.in`/`assets/*`.
2. `GM_VERSION` в `geomask.sh.in` → новая версия.
3. `python build.py`, затем `python tools/check_build.py` (соответствие сборки).
4. Прогнать песочницу.
5. Добавить `docs/releases/vX.Y.Z.md`.
6. Закоммитить и запушить; создать GitHub release с тегом `vX.Y.Z` (артефакт `geomask.sh`).

## Структура

```
geomask.sh.in     шаблон установщика
assets/           встраиваемые файлы
build.py          сборщик
tools/            check_build.py — проверка «сборка == коммит»
tests/            публичная песочница (в git, гоняется в CI)
.github/          workflows/ci.yml
docs/             документация
docs/releases/    release notes
sandbox/          локальная песочница (не в git)
```
