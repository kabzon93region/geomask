# Исследование: достоверное геоизмерение Google для node-audit

**Дата:** 08.11.2026, MSK; **проверено на живом узле 09.11.2026.** **Статус:** исследовательский отчёт; не разрешение на внедрение.

## Проверка на живом узле (09.11.2026)

Пробы выполнялись через изолированный mihomo с реальным узлом `🇺🇸 USA 2` (freedom-узел из
AI-снапшота), remote DNS через SOCKS5h. Скрипты: `scripts/checks/probe_google_geo.py`,
`scripts/checks/google_geo_signals.py`.

| Запрос | Результат |
|---|---|
| `https://www.google.com/search?q=test&hl=en` | HTTP 200, 93 155 байт, без согласия и без CAPTCHA |
| `https://www.google.com/` | HTTP 200, 90 656 байт |
| `https://www.google.com/generate_204` | HTTP 204, 1 байт |

Поиск в теле SERP по машинно-читаемым признакам региона:

```
uule parameter         none
region json            none
countryCode json       none
country json           none
gl json                none
data-country           none
offered in             none
footer country link    1 hit  -> google.js/  (путь к скрипту, не ccTLD)
consent                none
captcha                none
```

**Вывод:** Google отдаёт страницу выдачи, не раскрывая страну ни в одном виде, который может
прочитать скрипт: ни `uule`, ни JSON-полей региона, ни `data-country`, ни строки «Google offered
in». Единственное совпадение регулярного выражения оказалось путём к файлу скрипта.

Значит, программного признака «Google считает вашу страну такой-то» без браузерной сессии,
согласия и исполнения JS получить не удалось. Это подтверждает прежний вывод исследования и
оставляет gate открытым: **полный AI admission не может опираться на Google**.

## Проблема

Текущий `IsolatedProbe` проверяет Google через `https://www.google.com/generate_204` — HTTP 204 No Content.
- HTTP 204 подтверждает **доступность** сервиса, не страну выхода.
- Нет тела ответа, нет заголовков с геоинформацией.
- `cdn-cgi/trace` на Google возвращает 404.
- `google.com` может отдавать CAPTCHA или редирект на локальный домен (google.ru и т.д.).

**Результат:** `classify` для AI всегда возвращает `unknown` для Google, блокируя полный AI admission.

## Исследованные варианты

### 1. Google Search footer "From your internet address"
**Источник:** [Google Help](https://support.google.com/websearch/answer/179386?hl=en)
> "Sometimes when you search on Google, we show your internet address at the bottom of the search results page... This is based on your IP address."

**Метод:** GET `https://www.google.com/search?q=test` с headers браузера, парсинг HTML футера.

**Проблемы:**
- Требует полноценный User-Agent, cookies, иногда JS.
- CAPTCHA при автоматизированных запросах.
- HTML может меняться; парсинг хрупкий.
- Региональные домены (google.ru, google.de) редиректят.
- Неофициальный API — может сломаться в любой момент.

### 2. Google OAuth / UserInfo endpoint
**Метод:** `https://www.googleapis.com/oauth2/v3/userinfo` с access_token.

**Проблемы:**
- Требует OAuth flow, не подходит для серверного аудита без пользователя.
- Не возвращает IP/страну без профиля пользователя.

### 3. Google DNS / `dns.google/resolve`
**Метод:** `https://dns.google/resolve?name=google.com&type=A`

**Проблемы:**
- Показывает резолвинг, не страну egress.
- Аналогично Cloudflare trace, но для DNS.

### 4. Google Cloud Metadata / `metadata.google.internal`
**Метод:** Доступен только внутри GCP.

**Проблемы:** Не применимо на OpenWrt роутере.

### 5. YouTube / `youtube.com/generate_204`
**Метод:** Аналогично Google generate_204.

**Проблемы:** Тоже HTTP 204 без тела.

### 6. Google Maps Geolocation API
**Метод:** POST `https://www.googleapis.com/geolocation/v1/geolocate?key=...`

**Проблемы:**
- Требует API ключ.
- Оплачиваемый сервис.
- Возвращает примерное местоположение по Wi-Fi/Cell, не IP egress.

### 7. IP-based geolocation services (ip-api.com, ipinfo.io, etc.)
**Метод:** GET внешнего сервиса через изолированный тракт.

**Проблемы:**
- **Нарушает архитектуру node-audit**: проверка должна быть *service-specific* (к самому сервису), не через третью сторону.
- Внешний сервис видит IP выхода прокси, но это не подтверждает, что Google видит тот же IP.
- Зависимость от третьей стороны, rate limits, платные тарифы.

## Вывод

На текущий момент **нет достоверного автоматизированного способа** получить страну выхода именно для Google без:
1. Полноценного браузера (Playwright/Selenium) — тяжело для роутера.
2. Неофициального парсинга Search — хрупко, CAPTCHA.
3. Официального API Google — не существует для этой цели.

## Рекомендации для node-audit

### Вариант А (реалистичный): Признать Google UNKNOWN как gate
- AI admission требует **всех** обязательных сервисов (Cloudflare + ChatGPT + Google).
- Если Google UNKNOWN — admission blocked.
- Это честное поведение: мы не можем доказать страну, поэтому не разрешаем.
- План уже содержит это: "Google geography UNKNOWN => Apply blocked honestly unless separately implemented verified Google-specific evidence" (§7F).

### Вариант Б (экспериментальный): Headless браузер для Google Search
- Использовать Playwright/Chromium в изолированном контейнере на роутере (если ресурсы позволяют) или на ПК-агенте.
- Загрузить `https://www.google.com/search?q=my+ip`, извлечь футер.
- Риски: CAPTCHA, изменение HTML, ресурсы роутера (RAM/CPU).
- Можно сделать опционально: включить только если пользователь согласен на риск фейлсов.

### Вариант В: Документировать ограничение и не блокировать VPN/noP2P
- Для VPN/noP2P достаточно Cloudflare trace (работает надёжно).
- Для AI — явное предупреждение: "Google geography cannot be verified automatically; AI admission requires manual confirmation or accepted UNKNOWN".
- Не блокировать весь функционал из-за одной недоступной проверки.

## Принятое решение (по плану)

**D3 gate не блокирует VPN/noP2P completion.** 
- Реализация VPN/noP2P Apply продолжается независимо.
- AI Apply требует явного решения владельца после этого отчёта.
- В UI показывать: "Google geography: UNKNOWN (automated verification not available)".

## Следующие шаги

1. Зафиксировать этот отчёт в `docs/RESEARCH-GOOGLE-GEO-2026-11-08.md`.
2. Не тратить время на неработающие автоматические методы.
3. Перейти к E1 (config transaction) — критически важная инфраструктура для Apply.
4. Вернуться к Google-proof только если владелец утвердит Вариант Б с Playwright.