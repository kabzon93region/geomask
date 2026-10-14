# geomask v1.5.5 — понятные подписи шагов в мастере

## Улучшено
- Пошаговый мастер больше не показывает голые `env / geo / subs / sync /
  services / verify` — у каждого шага теперь человекочитаемое описание
  прямо в списке:

  ```
  1) env       проверка окружения: версия OpenWrt, место на диске, зависимости (только чтение)
  2) geo       гео-оверлей: панель "Гео-маскировка и AI" в LuCI + бэкенд geo-sync
  3) subs      подписки: ссылки VPN/AI и группы VPN/AI в mihomo (проверка доступности)
  4) sync      sync: списки доменов magitrickle для mihomo + cron каждые 5 минут
  5) services  сервисы: mihomo, magitrickle, hev-туннели, dnsmasq, веб-интерфейс
  6) verify    сквозная проверка: выходы VPN/AI, DNS, группа AI, интернет (ничего не меняет)
  ```
- Статус («выполнено» / «требует внимания») сохранён и не меняет смысл;
  песочница: 37/37 PASS.

## Обновление
```sh
opkg install https://github.com/kabzon93region/geomask/releases/download/v1.5.5/geomask.sh
```
