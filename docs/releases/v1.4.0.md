## geomask v1.4.0

Критический security/safety-релиз: инцидент 13.10 (некорректный `listeners`
в конфиге mihomo + рестарт без проверки) вскрыл три дыры — исправлены все.

### Исправлено

- **Синтаксис `listeners`**: было `listeners: {ai-devices: {...}}` (map),
  стало `listeners: [{name: ai-devices, type: mixed, port: 7891,
  listen: 127.0.0.1, proxy: AI}]` — прежний формат ронял mihomo при старте,
  хотя `mihomo -t` его пропускал.

### Добавлено

- `sh geomask.sh --backup` — полный бэкап настроек роутера
  (`sysupgrade -b`, LuCI-совместимый архив в `/root/geomask-backups/`).
- **Перед рестартом mihomo теперь обязательно**: полный бэкап → **trial-run**
  копии конфига в `/tmp/gm-trial-*` на портах 17890/17901/19090 → рестарт →
  проверка pid + API → **автоматический откат** предыдущего конфига при
  неуспехе. Живой сервис не трогается, пока trial не прошёл.
- Тесты песочницы: 37/37 PASS (проверяется весь safety-путь:
  «trial run: OK» → «restarted and verified» → бэкап создан).

### Установка

```sh
curl -fL -m 60 -o /tmp/geomask.sh 'https://cdn.jsdelivr.net/gh/kabzon93region/geomask@master/geomask.sh' && sh /tmp/geomask.sh
```

Перед изменениями на роутере: `sh /tmp/geomask.sh --backup`.
