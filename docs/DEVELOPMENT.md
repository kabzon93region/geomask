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

## Стиль кода

- Скрипты на роутере — POSIX `sh` (busybox), без bash-измов.
- Идемпотентность: повторный запуск не ломает состояние.
- Никаких `reboot` и изменений `network`; бэкап перед записью.
- Секреты не попадают в репозиторий (только аргументами, остаются на роутере).

## Песочница и тесты

Локальная песочница `sandbox/` (fake root + стабы `pidof/ubus/mihomo/curl/crontab/nslookup/ip/wget/apk`)
запускается без роутера и без VM/WSL:

```sh
bash sandbox/run_tests.sh     # ожидаемо: все PASS
```

Песочница — инструмент разработки, **в репозиторий не входит** (`.gitignore`).

## Релиз

1. Внести изменения в `geomask.sh.in`/`assets/*`.
2. `GM_VERSION` в `geomask.sh.in` → новая версия.
3. `python build.py`.
4. Прогнать песочницу.
5. Добавить `docs/releases/vX.Y.Z.md`.
6. Закоммитить и запушить; создать GitHub release с тегом `vX.Y.Z` (артефакт `geomask.sh`).

## Структура

```
geomask.sh.in     шаблон установщика
assets/           встраиваемые файлы
build.py          сборщик
docs/             документация
docs/releases/    release notes
sandbox/          локальные тесты (не в git)
```
