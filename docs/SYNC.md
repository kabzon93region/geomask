# Синхронизация magitrickle → mihomo

## Зачем

`magitrickle` умеет маршрутизировать по доменам на уровне DNS, но «настоящее» решение о маршруте
принимает mihomo (по SNI/домену). Чтобы списки доменов magitrickle попадали в mihomo, их переносят
в `rule-providers`.

## Как работает

`/root/sync_mtrickle_mihomo.sh` (cron `*/5`):

1. читает группы magitrickle (`/etc/magitrickle/state/config.yaml`);
2. формирует `/etc/mihomo/ruleset/mtrickle.yaml` (домены группы → VPN) и `ai.yaml` (AI-группа);
3. `mihomo` перечитывает конфиг через API (`PUT /configs`) — без перезапуска.

## Правила mihomo

```yaml
rules:
  - RULE-SET,ytdirect,DIRECT            # DIRECT-домены (youtube и т.п.)
  - DOMAIN-SUFFIX,cloudflare-ech.com,AI
  - RULE-SET,ai,AI                      # AI-группа magitrickle
  - RULE-SET,mtrickle,VPN               # прочие группы → VPN
  - AND,((NETWORK,udp),(DST-PORT,3478)),VPN     # STUN
  - AND,((NETWORK,udp),(DST-PORT,19302)),VPN    # STUN (Google)
  - MATCH,GLOBAL
```

`rule-providers` — `type: file`, `behavior: classical`, пути `/etc/mihomo/ruleset/*.yaml`.

## Важно

- Синк влияет на **обычные** устройства (путь через `7890`). AI-устройства (путь `7891`, `proxy: AI`)
  синком не управляются — у них весь трафик и так в AI.
- Синк только читает состояние magitrickle и пишет ruleset; конфиг mihomo не перезапускается.
- Проверка: `sh /tmp/geomask.sh --step 5`.
