## geomask v1.1.3

Патч диагностики: дубли вывода и счётчики.

### Исправлено

- **Дубли строк в диагностике/проверке**: после успешного check повторно запускался verify (для всех шагов это тот же check) — каждый блок печатался дважды. Verify теперь выполняется только после apply.
- **Счётчики итогов**: прошедшие шаги теперь попадают в `pass=` (раньше всегда `pass=0 fail=0`).
- **Пустая строка `OpenWrt:`** в шаге env: опечатка `DISTRIBUTION_RELEASE` вместо `DISTRIB_RELEASE`.

### Установка

```sh
curl -fL -m 60 -o /tmp/geomask.sh 'https://cdn.jsdelivr.net/gh/kabzon93region/geomask@master/geomask.sh' && sh /tmp/geomask.sh
```

Пин на релиз (если @master в кэше): `https://cdn.jsdelivr.net/gh/kabzon93region/geomask@<commit>/geomask.sh` — commit см. в теге.

Тесты: 31/31 PASS (новые проверки: отсутствие дублей, версия OpenWrt, счётчики pass=7).
