# Режимы и эксплуатация

## Режимы запуска

| Режим | Команда | Что делает |
|---|---|---|
| Интерактив | `sh geomask.sh` | Меню: сводка статусов + диагностика/мастер/пересстановка |
| Пересстановка | `sh geomask.sh --reinstall` | Удалить → установить → настроить; mihomo **не** перезапускается |
| Полный авто | `sh geomask.sh --auto` | Все шаги без вопросов; стоп при verify-FAIL |
| Только проверка | `sh geomask.sh --check` | Read-only отчёт по всем шагам |
| Один шаг | `sh geomask.sh --step N` | Только шаг N (1..7) |
| Без вопросов | `+ --yes` | Не спрашивать подтверждений |
| С рестартом | `+ --restart` | Разрешить перезапуск mihomo (обрыв ~3с). Игнорируется при `/root/.geomask-norestart` |
| С подписками | `+ --vpn-url URL --ai-url URL` | Задать URL подписок |
| Бэкап | `sh geomask.sh --backup` | Полный бэкап (`sysupgrade -b`) в `/root/geomask-backups/` |

## Шаги

1. **env** — окружение.
2. **geo** — гео-оверлей LuCI.
3. **subs** — конфиг mihomo (+`mihomo -t`).
4. **aidns** — AI-DNS без утечки.
5. **sync** — sync magitrickle → mihomo + cron + hotplug-роутинг.
6. **services** — старт только упавших сервисов.
7. **verify** — E2E-проверки.

## Безопасность и бэкапы

- Никаких `reboot`, не трогаем `network`/`pppoe`.
- Перед каждой записью файла — копия `.bak_geomask_<timestamp>`.
- Полный бэкап: `sh geomask.sh --backup` (`/root/geomask-backups/`, LuCI-совместимый архив).
- Перед рестартом mihomo (шаг 3 с `--restart`): авто-бэкап → trial-run копии конфига в
  `/tmp/gm-trial-*` на портах 17890/17901/19090 → рестарт → проверка pid + API; при провале —
  **автоматический откат**.
- Pin-блокировка рестартов: `/root/.geomask-norestart`.
- Подписки применяются через API без рестарта; полный рестарт — только по `--restart`.

## Проверка после изменений на роутере

```sh
sh /tmp/geomask.sh --check            # сводка по всем шагам
ip rule show                          # приоритеты 19900/19901/19902 выше magitrickle
netstat -tlnp | grep -E '7890|7891'   # слушатели mihomo
pidof mihomo magitrickled             # живые сервисы
```
