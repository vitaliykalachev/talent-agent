#!/bin/bash
# Архив из Telegram или браузера распакован с карантином на каждом файле. Разрешение
# macOS клиент даёт один раз, на этот файл; с остальных карантин снимаем сами, иначе
# система спрашивала бы про python и каждую библиотеку внутри.
xattr -dr com.apple.quarantine "$(dirname "$0")" 2>/dev/null || true
cd "$(dirname "$0")" || exit 1
if [ ! -x "python/bin/python3.12" ]; then
  echo "Сначала распакуйте архив целиком, затем запустите этот файл из распакованной папки KadrovyAgent."
  exit 1
fi
version=$(sw_vers -productVersion)
if [ "${version%%.*}" -lt 14 ]; then
  echo "Нужна macOS 14 или новее, а на этом Mac — $version. Обновите систему и запустите снова."
  exit 1
fi
export PYTHONUTF8=1
export TA_DATA_DIR="$PWD/data"
export TA_MODELS_DIR="$PWD/data/models"
export HF_HUB_OFFLINE=1
: "${TA_OPEN_BROWSER:=1}"
export TA_OPEN_BROWSER
echo "Кадровый агент запускается, первый запуск занимает до минуты. Не закрывайте это окно, пока работаете."
exec ./python/bin/python3.12 -m app.main
