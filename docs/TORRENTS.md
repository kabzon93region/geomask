# Торренты и P2P: как они маршрутизируются в geomask

Задача владельца (30.10): **ни одно торрент-соединение не должно идти через mihomo-подписки**
(ни VPN, ни AI). Разница с прошлой политикой принципиальная — раньше порты трекеров
принудительно отправлялись в VPN.

## Почему нельзя «по типу трафика»

Классификатора «это BitTorrent» в стеке нет:

| Слой | Что умеет матчить | Чего не видит |
|---|---|---|
| mihomo | домен, IP, порт, процесс (только локальный трафик роутера), GEOIP/SNI | сам протокол BT |
| magitrickle | домены из DNS-ответов (ipset) + подсети | пиры, пришедшие из DHT/PEX без DNS |
| zapret | протоколы/TLS/SNI | — |

DHT, PEX и пиры — это голые TCP/UDP на случайные адреса без сигнатуры, поэтому «системно по
типу соединения» реализуется только **по source IP** (отдельное устройство/адрес) или
**по порту/списку** (то, что сделано здесь).

## Три механизма, которые действуют сейчас

### 1. Слушатель `torrent-direct` (главный)

```yaml
  - name: torrent-direct
    type: mixed          # SOCKS5 + HTTP
    port: 7894
    listen: 192.168.0.1
    proxy: DIRECT        # всё, что запросит клиент, выходит напрямую
```

В qBittorrent: Настройки → Соединение → Прокси-сервер → SOCKS5, `192.168.0.1:7894`,
включить «Использовать прокси для работы BitTorrent» и «…для соединения с пирами»,
«Выполнять поиск имени хоста через прокси» оставить (DNS резолвится на роутере).

Проверено, что libtorrent гоняет через слушатель не только TCP, но и **UDP (DHT/µTP)** —
в conntrack видно поток `sport=41552 dport=7894` по UDP.

Поле `proxy:` у слушателя **не проверяется** `mihomo -t` (валидация пропускает даже заведомо
мусорное значение), поэтому корректность подтверждалась рантаймом: запросы через слушатель дают
`using DIRECT`, а `proxy: NOSUCHPOLICY` возвращает 502.

### 2. Порты P2P/трекеров → DIRECT

```
- DST-PORT,6969,DIRECT   - DST-PORT,1337,DIRECT   - DST-PORT,2710,DIRECT
- DST-PORT,8081,DIRECT   - DST-PORT,451,DIRECT    - DST-PORT,5944,DIRECT
```

Страховка на случай, если что-то пойдёт мимо прокси (другое устройство, клиент без прокси,
входящее соединение). Рядом остаются явные DIRECT для `rutracker.*` и `thepiratebay.org`.

### 3. Ruleset `torrentdirect` — списки трекеров

`/etc/mihomo/ruleset/torrentdirect.yaml`, 108 записей: 103 домена + 5 трекеров, у которых в
объявлении стоит голый IP. Собирается из публичных списков
[ngosang/trackerslist](https://github.com/ngosang/trackerslist) (`trackers_all.txt`) и
[XIU2/TrackersListCollection](https://github.com/XIU2/TrackersListCollection) (`all.txt`);
поддомены, покрытые родителем, сворачиваются.

Правило стоит **после** `RULE-SET,ai,AI` и **до** `RULE-SET,mtrickle,VPN`:

```
- RULE-SET,ytvpn,VPN
- RULE-SET,ytdirect,DIRECT
- RULE-SET,ai,AI                # политика Google/AI всегда выигрывает
- RULE-SET,torrentdirect,DIRECT # трекеры не могут быть засосаны VPN-списками
- RULE-SET,mtrickle,VPN         # 2769 правил, из них 1300 IP-CIDR хостинга
```

Это важно, потому что `mtrickle` содержит 1300 `IP-CIDR` правил (Ovh 622, Cloudfront 243,
Digitalocean 180, Meta 117, Hetzner 84) — именно туда попадали трекеры и seedbox-пиры.

## Что по-прежнему не закрыто

- **Входящие пиры** (те, кто стучится на порт 41552) не проходят через прокси: они приходят на
  адрес клиента, и ответы уходят обычным путём. Их защищает механизм 2/3 (порт и списки), но
  произвольный IP пира списком не накрывается — он и не нужен: не совпав ни с одним правилом,
  он уходит в `MATCH,GLOBAL` = DIRECT.
- Если нужна 100% гарантия «вообще не касаться mihomo» — только отдельное устройство в режиме
  `bypass` (правило `ip rule priority 19900 from <ip> lookup main`, обходит перехват TUN на 32760).
  Вариант «второй IP на рабочем ПК» отклонён: `netsh add address` на DHCP-интерфейсе переводит
  адаптер в статический режим, а в qBittorrent 5.x нет привязки к интерфейсу.
- Слушатель открыт всей `br-lan` — любое устройство сможет получить DIRECT через `192.168.0.1:7894`.
  Закрывается ограничением по source IP (нужен статический адрес клиента).

## Диагностика

```sh
logread | grep -E 'tracker|RuleSet\(torrentdirect\)|using DIRECT' | tail -20
curl -s -H "Authorization: Bearer $(sed -n 's/^secret:[[:space:]]*//p' /etc/mihomo/config.yaml)" \
     http://127.0.0.1:9090/providers/rules | grep torrentdirect   # ruleCount=108
grep -nE 'DST-PORT|torrentdirect' /etc/mihomo/config.yaml
```
