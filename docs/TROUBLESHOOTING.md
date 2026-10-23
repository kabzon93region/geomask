# Диагностика

## AI-устройство показывает домашний IP (не AI)

**Симптом.** Устройство в режиме «весь трафик через AI», но сайт проверки IP показывает IP
провайдера/основного VPN, а не AI-подписки.

**Причина.** `ip rule` для устройства оказался **ниже** базы fwmark-правил magitrickle (которая
динамическая и смещается вниз). Помеченный magitrickle трафик уходит в основной туннель (`7890`)
вместо AI (`7891`). В логах mihomo при этом видно `using GLOBAL[DIRECT]` или `MT`-маршруты вместо AI.

**Проверка.**

```sh
ip rule show | head -6
# плохо: from <ip> lookup 101 на 32xxx, а fwmark magitrickle (0x4d6167xx) на меньших номерах
ip route get 8.8.8.8 mark 0x64        # должно: dev AI table 101
conntrack -L 2>/dev/null | grep <ip>  # dst=198.19.0.1 → AI; dst=198.18.0.1 → основной
```

**Решение.** Per-device правила должны иметь приоритет `<` базы magitrickle (используется `19900`).
Обновить hotplug/`geo-backend.sh` и переприменить:

```sh
ip rule del priority 32580 from <ip> 2>/dev/null
ACTION=ifup /etc/hotplug.d/iface/99-default-vpn-route
```

Подробнее — [ROUTING.md](ROUTING.md), [AI-DEVICES.md](AI-DEVICES.md).

## WebRTC/STUN показывает реальный IP

Сайты могут детектировать IP через WebRTC. STUN-трафик (UDP 3478/19302) направляется через VPN
правилами mihomo; для AI-устройств — через AI. Проверьте наличие правил:

```yaml
- AND,((NETWORK,udp),(DST-PORT,3478)),VPN
- AND,((NETWORK,udp),(DST-PORT,19302)),VPN
```

Ломать звонки (WebRTC) блокировкой STUN не рекомендуется.

## AI-DNS: утечка («страна AI, DNS из ISP»)

```sh
nft list table inet geomask_ai_dns
ip rule show | grep 0x64
netstat -tlnp | grep -E '5055|5353'
```
Если чего-то нет — `sh /tmp/geomask.sh --step 4`. Если AI-DNS отключён — `ls /etc/geomask-aidns.off`.

## Слушатель AI не активен

После включения режима AI нужно один раз `sh /etc/init.d/mihomo restart`, иначе `7891` не слушает:
`netstat -tlnp | grep 7891`.

## Подписка не применилась / `mihomo -t` FAIL

- Проверьте `mihomo -d /etc/mihomo -t`.
- Панель хранит бэкапы `config.yaml.bak_subswitch_*` и `.bak_geomask_*` — откат возможен вручную.
- Полный бэкап: `sh geomask.sh --backup`.

## Сменил подписку — «интернет пропал на всех устройствах»

**Так и бывает — это не поломка.** Применение подписки делает `PUT /configs` (reload mihomo):
все активные соединения рвутся одновременно (~1-3 с), затем перечитываются провайдеры и заново
проходят health-check'и узлов (до ~1-2 мин). Устройства в этот момент показывают «нет интернета»
на всех сетях одновременно.

**Проверка (на роутере):**

```sh
mihomo -d /etc/mihomo -t                                   # конфиг валиден?
curl -s http://127.0.0.1:9090/providers/proxies | grep -o '"updatedAt":"[^"]*"'
curl -s http://127.0.0.1:9090/proxies | grep -o '"now":"[^"]*"'   # у групп не пустой now
logread -e mihomo | tail                                   # ошибки набора узлов?
```

Если конфиг валиден и узлы подтянулись — подождать 1-2 минуты, восстановится само.
Если и через 2 минуты плохо — разбирать, [через что реально идёт соединение](#через-что-реально-идёт-соединение).

**Совет:** переключать подписки в спокойное окно — reload всё равно кратко рвёт потоки
(видео-звонки, загрузки переподключатся).

## Терминал (ttyd) недоступен после ребута

Проверьте, что ttyd слушает нужный интерфейс:
```sh
netstat -tlnp | grep 7681
```
Если слушает только `br-lan`-адрес, а нужен доступ со всех интерфейсов — в `/etc/config/ttyd`
задайте `option interface '0.0.0.0'` и `sh /etc/init.d/ttyd restart`.

## Правило magitrickle добавлено, но трафик идёт напрямую

**Симптом.** Домен добавлен в группу magitrickle (в т.ч. «To VPN»), а соединения всё ещё идут
напрямую / с домашним IP.

**Причина.** Конфиг magitrickle конвертируется в `mtrickle.yaml` **cron-синком раз в 5 минут**
(`*/5 * * * * /root/sync_mtrickle_mihomo.sh`). До первого выполнения синка mihomo о новом правиле
не знает. Второй нюанс: magitrickle наполняет свои ipset **по DNS-запросам** — после добавления
правила нужен новый запрос к домену (перезапуск приложения/очистка DNS-кэша).

**Проверка.**

```sh
grep -c '<домен>' /etc/mihomo/ruleset/mtrickle.yaml   # >0 → синк уже прогнался
logread -e mihomo | grep -i '<домен>'                 # match RuleSet(mtrickle) using ...
```

**Решение.** Не ждать — запустить синк вручную (mihomo перезагружается через API, без рестарта):

```sh
sh /root/sync_mtrickle_mihomo.sh
```

Затем открыть сайт заново (новое соединение + свежий DNS-ответ).

## Через что реально идёт соединение

**Проверка (на роутере):**

```sh
# 1) история матчей (log-level: info в конфиге mihomo):
logread -e mihomo | grep -i '<домен>'
# примеры: match RuleSet(mtrickle) using VPN[...] | match RuleSet(ytdirect) using DIRECT
#          dial GLOBAL (match Match/) ...        → ушло в MATCH,GLOBAL (обычно DIRECT)

# 2) живые соединения с цепочкой выходов (mihomo API):
curl -s http://127.0.0.1:9090/connections | grep -o '"host":"[^"]*"\|"chains":\[[^]]*\]'
```

**Атрибуция по устройству:** в цепочках TUN-соединений источник виден как `127.0.0.1`,
поэтому устройство определяется через conntrack: `conntrack -L | grep <ip-устройства>` →
ответ `dst=198.18.0.1` = основной туннель, `dst=198.19.0.1` = AI.

## Устройство идёт мимо VPN / сервис определяет «Россию»

Чек-лист (стандартное устройство должно идти как все: `dev Mihomo table 100`):

```sh
cat /etc/geo-devices.list                              # режимы: bypass/ai; для стандарта записи нет
ip route get 8.8.8.8 from <ip> iif br-lan             # стандарт: dev Mihomo table 100
                                                      # ai: dev AI table 101; bypass: lookup main
```

1. **DNS устройства** должен смотреть на роутер. На Android/TV — Private DNS выключен:
   `settings get global private_dns_mode` → `null`/`off` (иначе запросы мимо magitrickle).
2. **Домены приложения:** собрать список (`logread -e mihomo`, захват на `br-lan`) и сверить с
   группами magitrickle — недостающие домены добавить в нужную группу (см. синк выше).
3. Приложение может определять регион по **другим** доменам (API/стриминг-хосты), не тем, что
   вы добавляли — локализовать по п.2, а не по главному домену.

## Общая проверка

```sh
sh /tmp/geomask.sh --check
```
Раздел `SUMMARY` покажет `pass/fail` по шагам.
