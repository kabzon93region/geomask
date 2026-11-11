# Этап 1: Надёжные изменения и восстановление — спецификация

> **DRAFT / НЕ ДОПУЩЕН К ВНЕДРЕНИЮ — 08.11.2026, MSK.** Частичные локальные helper-ы не закрывают этап 1; текущий код всё ещё содержит P12 и неполный PC-backup gate. Приоритет имеет [утверждённый основной план](PLAN-2026-11-06-geomask-v2-rework.md). Текст ниже — сохраняемый эскиз, а не согласованный исполнимый контракт; противоречащие примеры не исполнять.

## Обязательные исправления перед реализацией/приёмкой

- Разделить candidate `mihomo -f ... -t` и настоящий изолированный runtime trial со всеми другими портами/ресурсами: таблица и фаза TRIAL ниже подменяют второе первым (основной план §3).
- Snapshot в памяти и синхронный rollback не обеспечивают восстановление при обрыве процесса/питания/SSH. Нужны durable journal и заранее запущенный независимый откат с проверкой фактического runtime (основной план этап 1; контракты этапа 0).
- Непереданный editable key нельзя удалять: меняются только запрошенные ключи, остальные значения/комментарии сохраняются. Рендерер ниже пока нарушает этот контракт.
- MagiTrickle-группы меняются через существующий API, не новым прямым YAML/SIGHUP writer. Не предполагать наличие `magitrickled --dry-run` или `ip rule dry-run` без проверки.
- Backup gate обязан безусловно подтвердить полный архив на ПК, сумму и покрытие восстановления. «MD5 если есть SSH», один mtime или архив на роутере недостаточны. 300 секунд — предложенное число, не утверждённый SLA.
- Нельзя выдавать SKIP без бэкапа за выполненное применение. Различать BLOCKED/не выполнено и успех. Согласовать порядок lock/revision/gate (два раздела ниже расходятся), включить всех writers одного ресурса и пары conf/cron, не объявлять два replace одной атомарной транзакцией.

Эти замечания не исправляют production-код. Live apply, проверки и приёмка в этой документационной задаче не выполнялись.

**Основание:** [основной план](PLAN-2026-11-06-geomask-v2-rework.md) §6 этап 1, [отчёт этапа 0](STAGE-0-2026-11-07.md).  
**Цель:** один протокол записи для всех общих ресурсов (mihomo config, nodes.conf, rulesets, device rules, cron, zapret), с full backup gate, candidate validation, trial, atomic install, verified rollback.  
**Срок:** локальная реализация и тесты в этой сессии; live apply — отдельным согласованным шагом после preflight.

## 1. Объекты защиты (shared resources)

| Ресурс | Текущие writers | Файл/путь | Риск |
|---|---|---|---|
| mihomo config.yaml | geomask.sh.in::write_mihomo_config, geo-backend.sh::_slot_apply, node-scan, lane, torrent | /etc/mihomo/config.yaml | перезапись live, нет trial всех портов |
| nodes.conf | geo-backend.sh::cmd_nodes_set, installer seed, cron | /etc/geomask-nodes.conf | P12: затирает ключи, нет atomic pair с cron |
| rulesets (mt/ai/ytvpn/torrent) | sync_mtrickle_mihomo.sh, ytwatch, torrent, lane | /etc/mihomo/ruleset/*.yaml | отдельные atomic replaces не транзакция |
| device rules (source IP, fwmark, tables) | geo-backend.sh::_apply_routing, hotplug, guard | ip rule, nft, table 100/101 | delete/add без owned-reconcile, conntrack flush |
| zapret exclude | sync_mtrickle_mihomo.sh::_zapret_sync | /opt/zapret/ipset/zapret-hosts-user-exclude.txt | нет backup, hostlist не версия |
| cron (root) | installer, geo-backend.sh (nodes, syncd, yt, torrent, lane), guard | /etc/crontabs/root | нет snapshot, частные markers |
| MagiTrickle groups | seed-lists.py, ytwatch | /etc/magitrickle/state/config.yaml | stop→write→start не атомарен |

## 2. Единый протокол записи (Writer Protocol)

Каждый writer вызывает **централизованный helper** `safe_write(resource, candidate_factory, validators, rollback_check)`.

### 2.1 Контракт helper`а

```sh
# Вызов из shell: safe_write <resource> <candidate_factory_fn> <validator_fn> <rollback_check_fn>
# Возвращает: 0=успех, 1=validation failed, 2=backup failed, 3=trial failed, 4=install failed, 5=rollback failed
```

```python
# Python variant for embedded writers
def safe_write(resource: str,
               make_candidate: Callable[[bytes], bytes],
               validate: Callable[[bytes], Tuple[bool, str]],
               rollback_check: Callable[[bytes, bytes], bool]) -> int:
```

### 2.2 Ресурсы и их параметры

| Resource | Lock file | Revision file | Backup scope | Candidate path | Trial command | Rollback check |
|---|---|---|---|---|---|---|
| mihomo_config | /tmp/mihomo_config.lock | /etc/mihomo/config.yaml.rev | full sysupgrade | /etc/mihomo/config.yaml.cand | mihomo -f .cand -d /etc/mihomo -t | sha256(pre) == sha256(post) + pid/API |
| nodes_conf | /tmp/nodes_conf.lock | /etc/geomask-nodes.conf.rev | full sysupgrade + /etc/geomask-nodes.conf | .cand | (config only) | keys preserved + cron pair |
| ruleset_<name> | /tmp/ruleset_<name>.lock | /etc/mihomo/ruleset/<name>.yaml.rev | full sysupgrade + ruleset dir | .cand | mihomo -f config -t | ruleset hash + dependent groups |
| device_rules | /tmp/device_rules.lock | /etc/geo-devices.list.rev | full sysupgrade + device list | N/A (multi-file) | nft -c + ip rule dry-run | owned IP set == expected |
| zapret_exclude | /tmp/zapret_exclude.lock | /opt/zapret/ipset/zapret-hosts-user-exclude.txt.rev | full sysupgrade + zapret ipset dir | .cand | nft -c (set elements) | nozapret set == candidate |
| cron_root | /tmp/cron_root.lock | /etc/crontabs/root.rev | full sysupgrade + crontab | .cand | crontab -l validate | crontab diff == expected |
| mtr_groups | /tmp/mtr_groups.lock | /etc/magitrickle/state/config.yaml.rev | full sysupgrade + mtr state | .cand | magitrickled --dry-run? | group hash + API roundtrip |

### 2.3 Фазы helper`а (атомарны относительно ресурса)

1. **LOCK** — flock на lock file (timeout 10s), записать pid+timestamp
2. **REVISION** — прочитать/создать revision (sha256 текущего live)
3. **BACKUP_GATE** — свежий full sysupgrade на ПК (MD5 verified) за последние 300с; если нет — abort(2)
4. **SNAPSHOT** — прочитать live bytes + mode/owner, сохранить в память
5. **CANDIDATE** — вызвать `make_candidate(live_bytes) -> candidate_bytes`
6. **VALIDATE** — вызвать `validate(candidate_bytes) -> (ok, error_msg)`; если not ok — abort(1)
7. **TRIAL** — ресурсозависимая dry-run проверка (mihomo -t, nft -c, crontab validate); abort(3)
8. **ATOMIC_INSTALL** — os.replace(candidate, live) с сохранением mode/owner
9. **RELOAD** — ресурсозависимый reload (mihomo API reload, nft flush+load, crontab reload, magitrickle SIGHUP)
10. **POST_CHECK** — вызвать `rollback_check(pre_snapshot, post_live) -> bool`; если false — ROLLBACK
11. **SUCCESS** — обновить revision (sha256 нового live),释放 lock, return 0

### 2.4 Rollback (при любой ошибке на фазах 8–10)

```python
def rollback(resource, pre_snapshot, pre_revision):
    # 1. Restore live from pre_snapshot (atomic replace)
    # 2. Run reload
    # 3. Verify rollback_check(pre_snapshot, current_live) == True
    # 4. Restore revision file
    # 5. Release lock
    # Return 0 if rollback verified, else 5 (manual recovery needed)
```

## 3. P12: nodes.conf — сохранять все ключи

### 3.1 Текущий дефект (geo-backend.sh cmd_nodes_set L1166–1195)
- Валидирует только 4 поля, затем `truncates` файл до этих 4 ключей
- `ai_guard=0` исчезает → scanner default `cfgd.get('ai_guard','1')` включает защиту обратно
- Custom `ai_*`, `name_filter_groups`, `sub_user_agent`, комментарии, unknown keys — теряются
- cron пишется ПОСЛЕ conf — при ошибке cron получается partial apply

### 3.2 Новый формат хранения (canonical store)

```ini
# /etc/geomask-nodes.conf — INI-подобный, сохраняет ВСЁ побайтово
# Editable keys (managed by UI/RPC):
enabled=1
schedule=03:00
bad_countries=RU,BY,CN
bad_asns=AS210976,AS12345
base_filter=Turkey|United Kingdom|🇹🇷|🇬🇧
sub_provider=sub
# Protected keys (preserved verbatim, never defaulted):
ai_guard=1
ai_group=AI
ai_egress_url=https://chatgpt.com/cdn-cgi/trace
ai_expect=US
ai_listener=http://127.0.0.1:7891
ai_exclude_base=HY2
name_filter_groups=VPN,VPN-noP2P
sub_user_agent=clash-meta
# Unknown/custom keys preserved:
custom_field=value
# Comments preserved:
# This is a comment
```

### 3.3 Реализация (в geo-backend.sh)

```bash
_nodes_render_candidate() {
    # $1=live_bytes, $2=editable_kv_json (only 4 editable keys + enabled/schedule)
    # Returns candidate_bytes preserving everything else
    python3 - <<'PYEOF'
import sys, json, re
live = sys.argv[1]
editable = json.loads(sys.argv[2])
lines = live.splitlines(True)  # keepends
out = []
seen_editable = set()
for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith('#'):
        out.append(line); continue
    if '=' not in line:
        out.append(line); continue
    k, v = line.split('=', 1)
    k = k.strip()
    if k in ('enabled','schedule','bad_countries','bad_asns','base_filter','sub_provider'):
        if k in editable:
            out.append(f'{k}={editable[k]}\n')
            seen_editable.add(k)
        # else: drop this editable key (should not happen if UI sends all)
    else:
        out.append(line)  # preserve protected/unknown/comment verbatim
# Append any editable keys not seen (new keys)
for k in ('enabled','schedule','bad_countries','bad_asns','base_filter','sub_provider'):
    if k in editable and k not in seen_editable:
        out.append(f'{k}={editable[k]}\n')
sys.stdout.write(''.join(out))
PYEOF
}

_nodes_validate_request() {
    # Validate all 6 values BEFORE staging
    # enabled: exact 0|1
    # schedule: ^([01]?[0-9]|2[0-3]):[0-5][0-9]$
    # bad_countries: ^([A-Z]{2})(,[A-Z]{2})*$
    # bad_asns: ^(AS)?[0-9]+(,(AS)?[0-9]+)*$
    # base_filter: no quotes/backslash (existing rule)
    # sub_provider: ^[a-z0-9_-]+$
    # REJECT any LF/CR/NUL/C0/C1 in ANY value
}

_nodes_apply_pair() {
    # 1. Validate request (all 6 fields)
    # 2. Acquire lock /tmp/nodes_conf.lock
    # 3. Check backup gate (fresh sysupgrade on PC within 300s)
    # 4. Snapshot live conf + cron
    # 5. Render candidate conf via _nodes_render_candidate
    # 5. Validate candidate (syntax only for conf)
    # 6. Prepare candidate cron (only managed marker line)
    # 7. Atomic replace conf, then cron (both or neither)
    # 8. Verify both readback
    # 9. Release lock, update revision
}
```

## 4. Обновление всех writers

### 4.1 geo-backend.sh
- `cmd_nodes_set` → использует `_nodes_apply_pair`
- `cmd_nodes_get` → читает canonical формат, JSON-escape всех значений
- `_slot_apply` → использует `safe_write` для mihomo_config
- `cmd_sync_set`, `cmd_syncd_set`, `cmd_yt_mode`, `cmd_torrent_*` → через `safe_write` для соответствующих ресурсов

### 4.2 geomask.sh.in
- `write_mihomo_config` → `safe_write` mihomo_config
- `step3_apply`, `step5_apply` (seed) → `safe_write` для всех затронутых ресурсов
- `ensure_backup` → проверяет freshness перед любой записью (shared)

### 4.3 sync_mtrickle_mihomo.sh
- `_zapret_sync`, `_zapret_force_sync` → `safe_write` zapret_exclude
- `write_if_changed` для rulesets → `safe_write` ruleset_<name>

### 4.4 Сканеры/Guard
- `geomask-node-scan.sh` (apply_filter_all, ai-guard) → `safe_write` mihomo_config
- `geomask-guard.sh` → `safe_write` device_rules (multi-file snapshot)
- `geomask-lane.sh` → `safe_write` mihomo_config + device_rules + network (требует согласования)

### 4.5 Автоматические (cron/syncd)
- Перед каждой записью проверяют backup gate (fresh sysupgrade ≤300s)
- Если gate не пройден — логируют `SKIP no fresh backup` и выходят с rc=0 (не FAIL)

## 5. Backup Gate — единая точка входа

```bash
# В geomask.sh.in и geo-backend.sh
_backup_gate() {
    # 1. Найти последний локальный бэкап в Y:\openwrt\backup\router-backup-*.tar.gz
    # 2. Проверить MTIME ≤ 300s (5 минут)
    # 3. Проверить MD5 совпадает с удалённым (если есть SSH)
    # 4. Вернуть 0 если OK, 1 если нет
}
```

Все writers вызывают `_backup_gate` ПЕРЕД фазой LOCK. Без прохождения — не берут lock, не пишут.

## 6. Тесты (unit + integration)

| Тест | Описание |
|---|---|
| `test_safe_write_protocol.py` | Мокает fs/lock/trial, проверяет все фазы, rollback при ошибках |
| `test_nodes_conf_p12.py` | Render candidate сохраняет protected/unknown/comments; validate отвергает control chars; apply_pair atomic |
| `test_backup_gate.py` | Fresh/expired/missing backup; MD5 mismatch; race condition |
| `test_mihomo_config_writer.py` | Candidate validation, trial -f, reload verification, rollback |
| `test_ruleset_writer.py` | Multi-ruleset atomic install, dependent group check |
| `test_device_rules_writer.py` | Owned IP reconcile, no foreign deletion, AI path not fallback to main |
| `test_cron_writer.py` | Snapshot/restore, marker preservation, concurrent edit safety |

## 7. Файлы для создания/изменения

### Новые файлы:
- `assets/safe_write.sh` — shell helper (flock, snapshot, atomic replace, rollback)
- `tools/safe_write.py` — Python helper для embedded writers
- `tests/test_safe_write_protocol.py`
- `tests/test_nodes_conf_p12.py`
- `tests/test_backup_gate.py`
- `tests/test_mihomo_config_writer.py`
- `tests/test_ruleset_writer.py`
- `tests/test_device_rules_writer.py`
- `tests/test_cron_writer.py`

### Изменяемые файлы:
- `assets/geo-backend.sh` — P12 rewrite, all writers → safe_write
- `geomask.sh.in` — writers → safe_write, ensure_backup freshness gate
- `assets/sync_mtrickle_mihomo.sh` — writers → safe_write
- `assets/geomask-node-scan.sh` — apply → safe_write
- `assets/geomask-guard.sh` — device rules → safe_write
- `assets/geomask-lane.sh` — writers → safe_write (network части требуют согласования)
- `tools/geo-rpc.json` — добавить методы safe_write status, backup_gate_status
- `tools/gen-geo-rpc.py` — регенерация RPC

## 8. Критерии приёмки этапа 1 (local verification)

- [ ] Все unit tests PASS (включая новые)
- [ ] Sandbox tests PASS (309/0)
- [ ] Build parity PASS
- [ ] RPC parity PASS
- [ ] Конкурентный writer (2 параллельных safe_write) — сериализация через lock, нет corruption
- [ ] Невалидный кандидат — отвергается на VALIDATE, live не меняется
- [ ] Отказ API на RELOAD — rollback к pre_snapshot, post_check подтверждает восстановление
- [ ] Backup gate: expired/missing/corrupt — все writers skip с логом, не FAIL
- [ ] P12: ai_guard=0 сохраняется, custom keys сохраняются, повторное сохранение не теряет данные
- [ ] Owned-state reconcile: device rules удаляют только свои IP, чужие не трогают, AI path не падает в main
- [ ] Power-fail simulation: прервать между INSTALL и RELOAD → rollback на следующем запуске (journal)

## 9. Границы этапа 1

**Входит:** P12, safe_write protocol, backup gate, все writers переведены, unit/integration тесты.  
**НЕ входит:** Live deploy на роутер, миграция политик (это этап 3), UI изменения (этап 4), модульный манифест (этап 5).

## 10. Следующий шаг

После локального прохождения всех тестов — подготовить preflight скрипт для live apply, согласовать с владельцем свежий бэкап и окно применения. Live apply — отдельной задачей после этапа 1.