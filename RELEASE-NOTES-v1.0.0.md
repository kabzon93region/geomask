## geomask v1.0.0

Автоматизированный установщик домашнего стека обхода блокировок на OpenWrt: панель Zapret-Manager + гео-оверлей + mihomo с 2 подписками (VPN/AI) + magitrickle sync.

### Установка на роутере

```sh
sh <(wget -qO - 'https://raw.githubusercontent.com/kabzon93region/geomask/master/geomask.sh')
```

### Возможности

- **7 шагов**: env → panel → geo → subs(2 подписки) → sync → services → verify; каждый шаг: check → apply (с бэкапом) → verify
- **Режимы**: интерактивный, `--auto`, `--check` (read-only), `--step N`, `--yes`, `--restart`, `--vpn-url/--ai-url`
- **Безопасность**: без reboot, работающие сервисы не перезапускаются, бэкапы перед записью, `mihomo -t` перед установкой конфига
- **Self-contained**: гео-оверлей (rpcd geo-sync, LuCI-страница), sync-скрипт и hotplug вшиты в установщик
- **Песочница**: `bash sandbox/run_tests.sh` — 13/13 PASS без роутера (fake root + стабы команд)

### Что входит в шаг subs

Два proxy-provider (`sub` + `de5`), группы `VPN` (exclude РФ/моб.) и `AI` (немецкая нода), правила `ytdirect→DIRECT`, `cloudflare-ech→AI`, `ai→AI`, `mtrickle→VPN`, sniffer QUIC/TLS/HTTP.

### Требования

OpenWrt 24.10+ (проверено 25.12, aarch64), python3, curl, ubus/rpcd/luci, mihomo (ставится панелью).
