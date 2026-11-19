# Промпт для агента на Linux/WSL-машине

Скопируй текст ниже целиком в нового агента DSH на той машине (воркспейс — копия проекта
geomask). Ничего не меняй в этом файле перед отправкой, кроме пути к воркспейсу, если он
отличается.

---

Ты работаешь в Linux (WSL) с копией репозитория **geomask** (OpenWrt-оверлей, Python/shell,
тесты — `tests/test_*.py`, единый раннер `tests/testgate.py`). Твоя задача — прогнать полный
Linux unit-gate на текущем HEAD и отчитаться. **Ничего не исправляй и не коммить.**

1. Определи корень репозитория (там, где лежат `build.py` и каталог `tests/`). Выполни:
   - `git rev-parse --short HEAD` и `git merge-base --is-ancestor 734b97f HEAD && echo OK`
     (ожидается OK: это коммит с фиксом парковки бэкапа; если его нет — остановись и
     сообщи, какой SHA в воркспейсе).
   - `git status --porcelain` — должно быть пусто (если нет, зафиксируй вывод и продолжай).
2. Поставь зависимости, если их нет: `python3 --version`, `python3 -c "import yaml"`,
   `bash --version`, `tar --version`. На Ubuntu/Debian при необходимости:
   `sudo apt-get update && sudo apt-get install -y python3 python3-yaml`. Ничего больше не
   устанавливай, системные пакеты роутера не трогай.
3. Проверь сборку/паритет (быстро, до тестов):
   - `python3 build.py`
   - `python3 tools/build_module_index.py`
   - `python3 build.py`
   - `python3 tools/check_build.py --verify-only` (ожидается `OK: geomask.sh matches build`)
   - `python3 tools/compose_sources.py --check` (ожидается без расхождений)
   - `python3 tools/gen-geo-rpc.py --check`
   - `bash -n geomask.sh` (или `sh -n geomask.sh`)
   Сохрани вывод каждого шага в `gate-linux/static.log`.
4. Полный gate (главное). Запусти из корня репозитория:
   `python3 tests/testgate.py run --tests-dir tests --log-dir gate-linux --timeout 4200`
   Он идёт долго (самый медленный модуль `test_reinstall_phase2b` ~30 мин, всего ~60–90 мин).
   Не убивай его раньше 90 минут; если твой инструмент обрывает долгие команды — запусти в
   фоне (`nohup ... > gate-linux/run.log 2>&1 &`) и периодически смотри
   `gate-linux/summary.txt` и `gate-linux/*.log`.
5. Разбери результат:
   - Итог: `gate-linux/summary.txt` (modules run/tests run/executed/pass/fail/skip/timeout,
     `verdict: PASS|FAIL`).
   - Для каждого FAIL: имя модуля, имя теста, первые 40 строк трейсбека (из
     `gate-linux/<module>.log`), и одну фразу-гипотезу причины (без правок кода).
   - Отдельно перечисли skip'ы и их `reason` (на Linux часть Windows-скипов должна
     исчезнуть; если остались `posix-only`/`no-native-symlink` — это подозрительно, укажи).
6. Ограничения:
   - не редактируй продукт, тесты, docs и план; не делай `git add/commit/push`;
   - не запускай установщик на каком-либо роутере, не ходи по SSH к устройствам;
   - работай только внутри воркспейса; логи — в `gate-linux/` внутри него;
   - не удаляй чужие файлы и не меняй `git config`.
7. Отчёт (одним сообщением, по-русски, кратко):
   - HEAD SHA, ОС/ядро, версия python3, длительность прогона;
   - таблица: модуль → tests/pass/fail/skip/timeout для проблемных модулей (полную таблицу
     не нужно, только итог + всё, что не `ok`);
   - список FAIL с именами тестов и короткими трейсбеками;
   - skip'и и причины;
   - вердикт: PASS (все модули `ok`) или FAIL (есть падения/таймауты);
   - пути к `gate-linux/summary.txt` и логам, которые надо забрать.

Главный результат — падения, которых нет на Windows: это и есть Linux-only дефекты. Не
пытайся их «починить» — только собери доказательства.

---

Что делать с результатом: прислать итог и `gate-linux/` (или `summary.txt` + логи упавших
модулей) в основной чат — по ним заводится партия починки и повторный прогон.
