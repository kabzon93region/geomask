## geomask v1.1.2

Патч меню: два визуальных бага v1.1.x.

### Исправлено

- **MagiTrickle в сводке**: проверялся init-скрипт `magitrickled`, а реальный файл — `/etc/init.d/magitrickle` — строка всегда показывала «не установлен». Теперь служба определяется корректно.
- **Дубль пункта меню «3) Гео-панель LuCI»**: у строки формата было 5 плейсхолдеров `%s`, но передавалось 6 аргументов — busybox printf повторно применял формат к лишнему аргументу и печатал строку дважды.
- Внутри: проверка init-файлов по существованию (`-f`) вместо exec-бита.

### Установка

```sh
curl -fL -m 60 -o /tmp/geomask.sh 'https://cdn.jsdelivr.net/gh/kabzon93region/geomask@master/geomask.sh' && sh /tmp/geomask.sh
```

Если jsDelivr ещё отдаёт старую версию (кэш @master обновляется несколько минут) — возьмите пин на этот релиз:

```sh
curl -fL -m 60 -o /tmp/geomask.sh 'https://cdn.jsdelivr.net/gh/kabzon93region/geomask@<commit>/geomask.sh' && sh /tmp/geomask.sh
```

(commit см. в теге; либо скачайте `geomask.sh` из ассетов релиза.)

Тесты песочницы: 27/27 PASS (добавлены проверки однократного вывода пункта меню и статуса MagiTrickle).
