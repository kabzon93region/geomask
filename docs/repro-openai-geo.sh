#!/bin/sh
# repro-openai-geo.sh — показать, что ChatGPT видит не ту страну, что «обычный» IP.
#
# Реальный пример. PROXY = локальный порт вашего клиента (mihomo/Clash mixed-port),
# К КОТОРОМУ ПОДКЛЮЧЕН USA-сервер подписки un1.pro. По умолчанию это 127.0.0.1:7890
# (у Clash Verge/mihomo это 7890; у v2rayN — 10808; у других клиентов — свой порт).
#
# Запуск:   sh repro-openai-geo.sh
# Свой порт: PROXY="http://127.0.0.1:10808" sh repro-openai-geo.sh
PROXY="${PROXY:-http://127.0.0.1:7890}"

echo "proxy = $PROXY"
echo "time  = $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo

echo "--- chatgpt.com/cdn-cgi/trace  (это видит OpenAI) ---"
curl -s -m 20 -x "$PROXY" https://chatgpt.com/cdn-cgi/trace | grep -E '^(ip|colo|loc)='
echo
echo "--- www.cloudflare.com/cdn-cgi/trace  (это видят iplocation/whoer) ---"
curl -s -m 20 -x "$PROXY" https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(ip|colo|loc)='
echo
echo "Ожидание при подключении к USA-серверу un1.pro:"
echo "  cloudflare -> ip=<реальный US-адрес узла>  loc=US   colo=LAX/SEA/DFW/EWR/SLC"
echo "  chatgpt    -> ip=144.31.129.189            loc=FI   colo=HEL   <-- вот проблема"
