## geomask v1.1.1

Патч установки: обход медленного raw.githubusercontent.com.

### Проблема

Классический one-liner `sh <(wget -qO - 'https://raw.githubusercontent.com/...')` при медленном канале до GitHub raw (проверено на роутере: ~2 КБ/с) выглядит как зависание — пустой экран без прогресса, скрипт не стартует. Дополнительно исправлен дубль строки в хедере скрипта.

### Решение

Рекомендуемый one-liner теперь через **jsDelivr** (проверено: 82 КБ за ~2 секунды), с прогрессом скачивания и явным таймаутом:

```sh
curl -fL -m 60 -o /tmp/geomask.sh 'https://cdn.jsdelivr.net/gh/kabzon93region/geomask@master/geomask.sh' && sh /tmp/geomask.sh
```

wget-фолбэк и GitHub raw (как медленный вариант) — в README.

### Что нового

- Установка через jsDelivr CDN (файл тот же, синхронизируется с master)
- Прогресс скачивания и таймауты вместо молчаливого `wget -q`
- Исправлен дубль строки в шапке скрипта

### Установка на роутере

```sh
curl -fL -m 60 -o /tmp/geomask.sh 'https://cdn.jsdelivr.net/gh/kabzon93region/geomask@master/geomask.sh' && sh /tmp/geomask.sh
```
