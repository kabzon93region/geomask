# Релизы geomask

Правило одно: **роутер и GitHub не должны расходиться по версии.** Роутер сравнивает себя
с последним релизом на GitHub (`releases/latest`), поэтому «локально новее» — это не
нормальное состояние, а забытый релиз.

## Что читает роутер

Обновление (`upd_apply`) берёт из GitHub:

1. `tag_name` последнего релиза — например `v1.30.4`;
2. файл `releases/download/v1.30.4/geomask.sh`;
3. `digest` этого файла (`sha256:…`, GitHub считает его при загрузке).

И отказывается ставить, если не сходится хотя бы одно: `sh -n`, `GM_VERSION` внутри файла
равен тегу, sha256 файла равен `digest`. Поэтому релиз — это три согласованных вещи:
коммит, тег и ассет.

## Выпуск

```powershell
cd Y:\openwrt\geomask
python tools/release.py status     # что установлено, что на GitHub, где расхождение
python tools/release.py publish    # тесты -> тег -> релиз -> сверка digest
python tools/release.py verify --full   # скачать ассет и прогнать на нём всю песочницу
```

`publish` сам не выпустит релиз, если:

- в рабочем дереве есть незакоммиченные изменения;
- ветка не `master` или `master` не совпадает с `origin/master` (сначала push);
- `geomask.sh` не совпадает со сборкой (`tools/check_build.py`);
- тег уже существует;
- не проходят `check_build`, RPC-манифест, python-тесты или песочница.

Порядок работы над версией:

1. правки + `python build.py`;
2. `python tools/build_module_index.py` (каталог модулей) и снова `python build.py`;
3. `python tools/sync_module_versions.py --write` (версии модулей из истории);
4. поднять `GM_VERSION` в `geomask.sh.in`;
5. `python build.py`, `python tools/build_module_index.py`, `python build.py`;
6. тесты: `python -m unittest discover -s tests -p 'test_*.py'` и `bash tests/run_tests.sh`;
7. коммит и push;
8. `python tools/release.py publish`;
9. `python tools/release.py verify --full`.

## Развёртывание на роутер

Обычный путь — из панели: «Модули» → «Релиз geomask» → «Обновить до X». Он делает полный
бэкап, качает релиз, проверяет sha256 и ставит без перезапуска сервисов. Проверить, что
путь живой, можно на роутере:

```sh
ubus call geo-sync upd_check '{"force":"force"}'
ubus call geo-sync upd_apply          # бэкап -> скачивание -> проверка -> установка
ubus call geo-sync upd_status         # фазы: queued/backup/download/validate/apply/done
```

После установки `ubus call geo-sync mod_list` должен показать **все модули `current`**.
Если хоть один `modified` — значит сборка и каталог разошлись по байтам (см. ниже).

## Инвариант байтов

Установщик пишет файлы через heredoc: `build.py` читает ассет как текст (LF), срезает
хвостовые переводы строк, а heredoc добавляет ровно один. Каталог
(`modules/index.json`) обязан хешировать **те же байты**, иначе после официального
обновления каждый модуль выглядит «изменён локально».

Это закреплено тестами `tests/test_module_index.py`:

- хеш каталога == хеш ассета (с подставленной версией, LF, один `\n`);
- хеш каталога == тело heredoc в собранном `geomask.sh`;
- каждый ассет заканчивается ровно одним переводом строки (иначе `build.py` падает).

## Если что-то пошло не так

- `release.py status` показывает, где расхождение: тег, релиз, digest.
- Ассет не совпал с локальной сборкой — значит релиз собран не из HEAD: пересоберите,
  удалите тег/релиз (`gh release delete vX.Y.Z --yes`, `git tag -d vX.Y.Z`,
  `git push origin :refs/tags/vX.Y.Z`) и выпустите заново.
- Роутер показывает «локальная новее» — это и есть дрейф: нужен релиз текущей версии.
