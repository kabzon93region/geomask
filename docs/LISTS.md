# Списки и шаблоны (v1.13.0)

geomask хранит **снимок всех групп MagiTrickle** (доменные списки маршрутизации) как
версионируемый шаблон, чтобы переустановка или сбой конфига (инцидент B-018) не теряли
настроенную схему. Секретов в шаблонах нет — только домены/IP/regex и имена групп.

## Что шаблонизируется

| Артефакт | Где | Что содержит |
|---|---|---|
| `assets/magitrickle-groups.yaml` | в git | блок `groups:` MagiTrickle: 25 групп, ~3300 правил (namespace/subnet/domain/regex) |
| `templates/lists.meta.json` | в git | версия шаблона, дата экспорта, число групп/правил, sha256 |
| `/etc/geomask/lists/magitrickle-groups.yaml` | на роутере | установленная копия шаблона (пишет шаг 5) |
| `/usr/bin/geomask-seed-lists.py` | на роутере | сшиватель блоков `groups:` |

Подписки (`sub`/`de5`) и их URL/HWID сюда **не** входят — они в mihomo-конфиге и задаются
при установке (см. [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md)).

## Как устроено восстановление

`geomask-seed-lists.py` берёт живой `/etc/magitrickle/state/config.yaml`, сохраняет хостовые
`app:`-настройки и хвостовые ключи (`subscriptions:`), а блок `groups:` заменяет на шаблонный.
Перед записью — бэкап `config.yaml.bak_geomask_<ts>`.

Режимы:
- `--check-empty` — код 0, если в MagiTrickle 0 групп (безопасно авто-сидить), иначе 1.
- запись (по умолчанию) — вставляет шаблонные группы, печатает `CHANGED before=.. after=.. rules=..`.

## Когда сид применяется

- **Авто (шаг 5 установки):** только если `config.yaml` существует И в нём 0 групп (свежий
  роутер). Если группы уже есть — авто-сид пропускается (не затирает runtime-правки владельца).
- **Явно:** `sh geomask.sh --seed-lists` — перезаписать текущие группы шаблоном (с бэкапом).
- **Панель:** вкладка «Списки и синк» → карточка «Шаблоны списков» → «Восстановить из шаблона».

При явном сиде magitrickled останавливается, конфиг сшивается, запускается, затем
`sync_mtrickle_mihomo.sh` перегенерирует `ai.yaml`/`mtrickle.yaml`/`ytdirect.yaml` и mihomo
перечитывает конфиг через API (без рестарта mihomo, VPN не рвётся).

## Обновление шаблона (для владельца)

Шаблон — это снимок рабочего конфига на момент сборки geomask. Когда список доменов в
MagiTrickle меняют (добавили AI-провайдер и т.п.) и хотят зафиксировать его в git:

1. снять `config.yaml` с роутера;
2. прогнать `scripts/export_lists.py` (пишет `assets/magitrickle-groups.yaml` + `templates/lists.meta.json`);
3. CI-тест S17 проверяет, что в шаблоне нет URL/HWID/32-hex секретов;
4. коммит + релиз.

## Правка одного домена (v1.30.1)

Шаблон догоняет роутер только при явном сиде, поэтому добавленный в git домен на живом
роутере сам не появится — и наоборот, домен может быть в группе, но с выключенным
правилом (`enable: false`), и тогда MagiTrickle его не маршрутизирует.

Для точечных правок есть `geomask-lists-edit.py` (на роутере
`/usr/lib/geomask/geomask-lists-edit.py`):

```sh
python3 /usr/lib/geomask/geomask-lists-edit.py groups
python3 /usr/lib/geomask/geomask-lists-edit.py add    --group "To VPN" --domain plati.market
python3 /usr/lib/geomask/geomask-lists-edit.py move   --group "To VPN" --domain plati.market
python3 /usr/lib/geomask/geomask-lists-edit.py remove --domain plati.market
```

`move` — это одна транзакция: домен вынимается из старой группы и кладётся в новую, чтобы
он не оказался сразу в двух (тогда путь не определён). Перед записью инструмент считает
группы и правила и отказывается от правки, которую не может разобрать обратно; держит
pre-image в `/tmp`; останавливает magitrickled, пишет, поднимает и ждёт сервис; при
неудаче возвращает файл; в конце запускает синк mihomo. `--dry-run` показывает план.

Интерфейс группы задаёт путь: `Mihomo` → в туннель (дальше решают правила mihomo,
`RULE-SET,mtrickle,VPN`), `VPN-noP2P`/`AI` → сразу в этот туннель, `pppoe-wan` → напрямую.

## Связанные проверки

- Тесты: `tests/run_tests.sh` → сценарий **S17** (wiring + скан секретов + контракт ситейлера).
- `geomask.sh --check` (шаг 5) не требует наличия шаблона — авто-сид молча откладывается.
