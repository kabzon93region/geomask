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

## Терминал (ttyd) недоступен после ребута

Проверьте, что ttyd слушает нужный интерфейс:
```sh
netstat -tlnp | grep 7681
```
Если слушает только `br-lan`-адрес, а нужен доступ со всех интерфейсов — в `/etc/config/ttyd`
задайте `option interface '0.0.0.0'` и `sh /etc/init.d/ttyd restart`.

## Общая проверка

```sh
sh /tmp/geomask.sh --check
```
Раздел `SUMMARY` покажет `pass/fail` по шагам.
