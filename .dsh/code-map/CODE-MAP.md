# Карта кода: geomask

- Проект: `Y:\openwrt\geomask`
- Обновлено: 2026-10-30 03:35:31 (за 0.1 с; файлов 73, разобрано 73, переиспользовано 0)
- Стек: config, shell, javascript, python (по расширениям файлов)

## Языки

- config: 41
- shell: 19
- javascript: 9
- python: 4

## Точки входа

- не найдены (смотри манифесты ниже)

## Манифесты

- config, shell, javascript, python (по расширениям файлов): `-`

## Каталоги верхнего уровня

- `tests` — 28 файлов
- `sandbox` — 24 файлов
- `assets` — 13 файлов
- `(корень)` — 5 файлов
- `scripts` — 1 файлов
- `templates` — 1 файлов
- `tools` — 1 файлов

## Самые крупные файлы

- `geomask.sh` — 508 КБ: log (func), info (func), ok (func), bad (func), warn (func), hdr (func), ask (func), P (func)
- `tests\root_apply\etc\geomask\lists\magitrickle-groups.yaml` — 294 КБ: groups (key)
- `assets\magitrickle-groups.yaml` — 294 КБ: groups (key)
- `assets\geo-sync-page.js` — 57 КБ: copyToClipboard (func), flash (func), fallback (func), injectStyle (func), badge (func), badgeKlass (func), cleanName (func), ccBadge (func)
- `tests\root_apply\www\luci-static\resources\view\geo-sync\main.js` — 57 КБ: copyToClipboard (func), flash (func), fallback (func), injectStyle (func), badge (func), badgeKlass (func), cleanName (func), ccBadge (func)
- `assets\geo-backend.sh` — 50 КБ: json_esc (func), _grp_node (func), _count_rules (func), _migrate (func), cmd_status (func), _node_cc (func), cmd_leases (func), cmd_log (func)
- `tests\run_tests.sh` — 26 КБ: t (func), contains (func)
- `sandbox\root_apply\www\luci-static\resources\view\geo-sync\main.js` — 24 КБ: injectStyle (func), badge (func), badgeKlass (func), cleanName (func), ccBadge (func), E (func), subs (func), maskUrl (func)
- `sandbox\run_tests.sh` — 13 КБ: t (func), contains (func)
- `tests\root_apply\root\sync_mtrickle_mihomo.sh` — 7 КБ
- `assets\sync_mtrickle_mihomo.sh` — 7 КБ
- `assets\geomask-guard.sh` — 6 КБ: log (func)
- `assets\geomask-node-scan.sh` — 6 КБ
- `tests\root_apply\etc\mihomo\ruleset\ytdirect.yaml` — 6 КБ: payload (key)
- `assets\ytdirect.yaml` — 6 КБ: payload (key)
- `sandbox\root_apply\root\sync_mtrickle_mihomo.sh` — 6 КБ
- `sandbox\root_apply\etc\mihomo\ruleset\ytdirect.yaml` — 6 КБ: payload (key)
- `assets\geomask-seed-lists.py` — 3 КБ: parse_args (def), read_lines (def), groups_region (def), count_groups (def), main (def)
- `build.py` — 3 КБ: load (def), main (def), repl (def)
- `scripts\export_lists.py` — 3 КБ: log (def), groups_region (def), main (def)
- `tests\root_apply\etc\mihomo\config.yaml` — 3 КБ: mixed-port (key), allow-lan (key), bind-address (key), tcp-concurrent (key), mode (key), log-level (key), ipv6 (key), external-controller (key)
- `sandbox\root_apply\etc\mihomo\config.yaml` — 2 КБ: mixed-port (key), allow-lan (key), bind-address (key), tcp-concurrent (key), mode (key), log-level (key), ipv6 (key), external-controller (key)
- `fetch_backup.ps1` — 2 КБ: Sh (func)
- `deploy_panel.ps1` — 1 КБ
- `tools\check_build.py` — 1 КБ: main (def)

## Как искать (готовые команды)

```powershell
python "C:\Users\Admin\.dsh\bin\dsh-code-nav.py" def <имя>   "Y:\openwrt\geomask"
python "C:\Users\Admin\.dsh\bin\dsh-code-nav.py" refs <имя>  "Y:\openwrt\geomask"
python "C:\Users\Admin\.dsh\bin\dsh-code-nav.py" find <текст> "Y:\openwrt\geomask"
python "C:\Users\Admin\.dsh\bin\dsh-code-nav.py" map          "Y:\openwrt\geomask"
```

Порядок: `def` → `refs` → точечное чтение файла. Карта не заменяет чтение кода.
