# Стендовые gates плана массовой готовности — что запускать и что сохранять

Этот файл — рабочий runbook для всего, что **нельзя** доказать на Windows-хосте и что план
явно оставляет за изолированным Linux/OpenWrt-стендом. Всё, что доказуемо локально
(unit/sandbox-тесты, композиция исходников, паритет сборки, каталог модулей, RPC-манифест),
уже закрыто и записано в
[`2026-11-18-execution-log.md`](2026-11-18-execution-log.md).

## Общие правила (из плана, раздел «Требования и ограничения»)

- Никакого маршрута к рабочей LAN: стенд — отдельная сеть, отдельный роутер/VM, без доступа
  к `192.168.*` владельца. Рабочий роутер и `Z:` не используются.
- База стенда: **не** factory-OpenWrt с нуля, а образ с подготовленными `mihomo`,
  `MagiTrickle`, `hev-socks5-tunnel`/`zapret`; «чистый» в матрице = «без geomask».
- Перед любым изменением устройства — полный `sysupgrade -b`, локальная копия и проверенное
  восстановление; после restart — health-check, при отказе rollback.
- Каждый прогон сохраняет: SHA образа и артефакта, список пакетов, версии модулей, точные
  команды, `executed/skipped/reasons/duration/exit`, результат rollback/reboot.

## 1. Изолированный Linux-unit-gate (CI-лог 187)

**Цель:** воспроизвести падение CI и доказать, что оно исчезло, а не замаскировано.

1. Получить из CI артефакт/лог прогона `187` (кто именно падал: модули, тесты, таймауты,
   причины skip). Без этого артефакта пункт остаётся OPEN: локальные 1368 тестов его не
   заменяют.
2. На чистом Linux-хосте (Python 3.11+ и 3.14, PyYAML, bash, без Windows-специфики):
   ```sh
   git clone <repo> && cd geomask
   python3 build.py
   python3 tools/build_module_index.py
   python3 build.py
   python3 tools/check_build.py --verify-only
   python3 tools/gen-geo-rpc.py --check
   python3 -B tools/compose_sources.py --check
   python3 -B tools/repo_check.py --strict
   python3 -B tests/testgate.py run --tests-dir tests --log-dir /tmp/gate-linux
   ```
3. Сохранить `summary.txt`, список падений и причины skip; сравнить с логом 187 и с локальным
   `gate23` (96 модулей, 1398 тестов, 30 allowlisted skip).
4. Повторить на **final SHA** после всех правок; старый успех phase2 не переносится.

## 2. Gate этапа 0 (композиция исходников)

**Цель:** доказать, что `src/**` — единственный authored source, а delivery-файлы — его
точная композиция, на чистом checkout и на Linux.

1. Чистый `git archive`/clone (без рабочей копии Windows), затем:
   ```sh
   python3 -B tools/compose_sources.py --check     # все 16 целей: ok
   python3 build.py && python3 tools/check_build.py --verify-only
   python3 tools/build_module_index.py && python3 build.py
   python3 -B tools/compose_sources.py --check
   python3 -B tests/testgate.py run --tests-dir tests --log-dir /tmp/gate-0
   ```
2. Проверить, что `bash -n geomask.sh` и `node --check` (если есть) проходят на **собранных**
   файлах, и что `tests/test_install_safety.py` извлекает функции из composed шаблона.
3. Записать sha256 трёх delivery-файлов и `geomask.sh` — они должны совпасть с
   [`BASELINE-0a.md`](source-split/BASELINE-0a.md) при неизменном коде.

## 3. R06 / R11 и phase2a–2f на финальном SHA

**Цель:** реальные сценарии переустановки/отката/ACL/reboot, которые локальные fixture
моделируют, но не наблюдают.

1. `tests/test_reinstall_phase2a.py` … `phase2f.py` — прогнать на стенде под Linux, сверить с
   ожидаемыми 22/44/18/… тестами и сохранить логи.
2. R06: права/ACL (`/etc/mihomo`, `/etc/geomask`, `/root/geomask-backups`), symlink-identity,
   владелец файлов после update/rollback.
3. R11: реальный `rpcd`/`ubus` — `ubus call geo-sync …` для read и write методов, роли ACL,
   отказ write-метода read-роли, полный retrieval диагностического архива до 2 MiB с base64.
4. Fault injection: SIGKILL на каждом шаге установки/rollback, ENOSPC на overlay и в `/tmp`,
   отключение питания до публикации архива; после каждого — проверка «последний валидный
   backup на месте и восстанавливается».

## 4. Dataplane и сетевые gates

- **P01** (консольный fail-closed): netns с TUN вниз и WAN-default в таблице 102 — трафик
  клиента не должен выходить через WAN; после поднятия TUN таблица возвращается на туннель.
- **P10**: уже поднятый интерфейс/пустая очередь ifup — таблицы 100/102 и правила
  восстанавливаются, LAN-клиент ходит.
- **G05**: отдельные hev-инстансы/fixed listeners/egress — проверить, что AI/bypass-клиенты
  не делят один туннель и что egress соответствует политике.
- **DNS/DoH** (задача 5): alias down/up, смена UID/listen — nft/DoH/route/status согласованы;
  проверка DNS с LAN-клиента.

## 5. Backup/durability (P06, P08, P09)

- Убийство установки/переустановки на стадиях park/publish/rename/start; после — «последний
  валидный backup остаётся durable до замены», архив восстанавливается с PC-копии.
- Kill/power loss **между парковкой единственной копии и публикацией** (`734b97f`): следующий
  запуск обязан вернуть копию из `/etc/geomask/.backup-park-*` по отметке
  `/etc/geomask/backup-recovery` до создания нового архива; если парк исчез, установка обязана
  отказать с «копия потеряна», а не создать новый архив. Evidence: до/после —
  `ls -l /root/geomask-backups`, содержимое отметки, строка stderr, sha256 старого архива.
- После успешного прогона не должно оставаться ни `.backup-park-*`, ни `backup-recovery`;
  при `BACKUP_KEEP > 1` прежняя копия переживает ротацию.
- Проверка keep-list архива (что реально пишется: `/etc/geomask` не должен попадать в архив) и
  восстановление write-set.
- ENOSPC на overlay при парковке/публикации (P06): отказ без потери прежней копии.
- Volatile snapshot не считается гарантией при power loss — нужен реальный reboot-тест.

## 6. Матрица финального SHA

Заполнить по профилям из плана (все модули / core+path_vpn / core+path_vpn+path_ai /
ai_dns выбором) на чистом образе с базовым стеком: install → verify → reboot → selected
groups/services/listeners/DNS/WAN/LAN/local. В каждом прогоне — SHA, пакеты, модули,
команды, executed/skipped/reasons/duration/exit.

## Что считается закрытием

Пункт закрывается только записанным evidence-файлом (лог + sha256 + команды + результат), а
не «код выглядит правильно». До этого соответствующие строки плана остаются OPEN, и
execution-log помечает их как стендовые.
