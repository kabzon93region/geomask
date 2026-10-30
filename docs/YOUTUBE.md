# YouTube: как маршрутизируется в geomask

Три разных потока, три разных пути — это частый источник путаницы.

| Что | Куда | Почему |
|---|---|---|
| Страница и API: `youtube.com`, `i.ytimg.com`, `*.ggpht.com`, `s.youtube.com` | **DIRECT** (запрет/DPI) | работает напрямую, VPN только замедлит |
| Видео-рёбра `*.googlevideo.com` | **DIRECT по умолчанию**, сбойные — **VPN** | часть рёбер блокируется по IP, имена ротируются |
| Картинки превью в мессенджерах и прочий Google-софт | по общим правилам (`ai`/`mtrickle`) | это не YouTube-поток |

## Порядок правил в mihomo (важен)

```
- AND,((NETWORK,UDP),(DST-PORT,443),(DOMAIN-SUFFIX,googlevideo.com)),REJECT   # QUIC -> TCP
- RULE-SET,ytvpn,VPN          # <- сюда попадают сбойные рёбра
- RULE-SET,ytdirect,DIRECT    # остальные youtube-домены
- RULE-SET,ai,AI
- RULE-SET,mtrickle,VPN
- MATCH,GLOBAL                # = DIRECT
```

`ytvpn` обязан стоять **до** `ytdirect`, иначе суффикс `googlevideo.com` в ytdirect перехватит
рёбра раньше, и добавление хоста в «To VPN» ничего не даст (это и была первоначальная загадка).

## Кто чем управляет

- `ytvpn.yaml` **генерируется** синком из MagiTrickle-группы **«GoogleVideo VPN»** — правится только
  в MagiTrickle (`:8080`), вручную файл трогать не нужно.- `ytdirect.yaml` — статичный ассет geomask (список youtube-доменов для DIRECT).
- Автоматикой заведует `geomask-ytwatch.sh` (cron `*/5`, ночной `--reset` в 04:20): читает ошибки
  диалов mihomo и пинит падающие рёбра в VPN. Режимы `auto`/`blanket` — в карточке панели
  «Списки и синк» → «YouTube: рёбра googlevideo»; конфиг `/etc/geomask-ytwatch.conf`.
- QUIC-рёбра (UDP 443) принудительно гасятся `REJECT`, чтобы плеер уходил на TCP (B-016).

## Диагностика

```sh
logread | grep -E 'googlevideo' | tail -20        # чем ядро решило каждый edge
sh /usr/bin/geomask-ytwatch.sh --status           # режим и активные пины (JSON)
cat /etc/mihomo/ruleset/ytvpn.yaml                # что сейчас в VPN
```

Если видео не грузится, а в логе `error: ... i/o timeout` по `rrN---sn-...googlevideo.com` —
watchdog запинит его в течение ~5-10 минут. Если хост нужен срочно — добавьте домен в группу
«GoogleVideo VPN» в `:8080` вручную.
