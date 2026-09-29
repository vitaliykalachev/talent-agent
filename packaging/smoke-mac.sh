#!/bin/bash
# Дымовой тест распакованной сборки для Mac: запуск через «Запустить.command», как у клиента,
# в чистом окружении — без uv, brew, ~/.cache и venv проекта. Браузер не открывается.
#   packaging/smoke-mac.sh <папка KadrovyAgent> <пустая папка вместо HOME> [порт]
set -u
dir=$(cd "$1" && pwd) home=$2 port=${3:-8101}
mkdir -p "$home"
log="$home/agent.log"
env -i HOME="$home" PATH=/usr/bin:/bin:/usr/sbin:/sbin TA_OPEN_BROWSER=0 TA_PORT="$port" \
  bash "$dir/Запустить.command" >"$log" 2>&1 &
agent=$!  # exec в «Запустить.command» оставляет тот же процесс, это python
peak="$home/peak-rss"
echo 0 >"$peak"
(while kill -0 "$agent" 2>/dev/null; do
  rss=$(ps -o rss= -p "$agent" | tr -d ' ')
  [ -n "$rss" ] && [ "$rss" -gt "$(cat "$peak")" ] && echo "$rss" >"$peak"
  sleep 0.5
done) &
# smoke.py — только стандартная библиотека; гоняем его Python сборки, чтобы не зависеть от своего
"$dir/python/bin/python3.12" -B "$(dirname "$0")/smoke.py" "$dir" "$log"
code=$?
echo "Пик памяти python: $(($(cat "$peak") / 1024)) МБ"
kill "$agent" 2>/dev/null
wait "$agent" 2>/dev/null
echo "--- вывод окна агента ---"
cat "$log"
exit $code
