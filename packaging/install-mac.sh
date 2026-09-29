#!/bin/bash
# Установщик «Кадрового агента» для Mac на Apple Silicon. Клиент запускает его одной
# командой в Терминале: curl -fsSL <адрес>/install-mac.sh | bash
# Скачанное через curl не получает карантина, поэтому macOS не спрашивает про каждый файл.
# Ставим в ~/KadrovyAgent: к «Рабочему столу», «Документам» и «Загрузкам» Терминал
# просит отдельный доступ, к домашней папке — нет. Прежнюю версию не удаляем, а переносим
# в ~/KadrovyAgent.old: в ней может остаться база с работой клиента.
URL="__ПОДСТАВИТЬ__"

main() {
  set -e
  if [ "$(uname -m)" != "arm64" ]; then
    echo "Эта версия «Кадрового агента» работает только на Mac с процессором Apple (M1 и новее)."
    exit 1
  fi
  local version
  version=$(sw_vers -productVersion)
  if [ "${version%%.*}" -lt 14 ]; then
    echo "Нужна macOS 14 или новее, а на этом Mac — $version. Обновите систему: меню Apple → «Системные настройки» → «Основные» → «Обновление ПО», затем повторите команду."
    exit 1
  fi
  case "$URL" in
    http*) ;;
    *) echo "В установщике не указан адрес архива."; exit 1 ;;
  esac

  local dir="$HOME/KadrovyAgent" archive
  archive=$(mktemp -t kadrovyi-agent)
  echo "Скачиваем «Кадровый агент», около 900 МБ. Это займёт несколько минут."
  if ! curl -fL --progress-bar "$URL" -o "$archive"; then
    rm -f "$archive"
    echo "Не удалось скачать архив. Проверьте интернет и повторите команду."
    exit 1
  fi
  if [ -d "$dir" ]; then
    rm -rf "$dir.old"
    mv "$dir" "$dir.old"
    echo "Прежняя версия перенесена в папку KadrovyAgent.old."
  fi
  echo "Распаковываем…"
  ditto -x -k "$archive" "$HOME"
  rm -f "$archive"
  xattr -dr com.apple.quarantine "$dir" 2>/dev/null || true

  # Ярлык на рабочем столе. Копия «Запустить.command» там не сработала бы: она ищет
  # программу рядом с собой. Нет доступа к рабочему столу — молча обходимся без ярлыка.
  local shortcut="$HOME/Desktop/Кадровый агент.command"
  { printf '#!/bin/bash\nexec "$HOME/KadrovyAgent/Запустить.command"\n' >"$shortcut" &&
    chmod +x "$shortcut"; } 2>/dev/null || true

  if [ "${TA_OPEN_BROWSER:-1}" != 0 ]; then  # в проверках Finder не открываем
    open "$dir"
  fi
  echo "Готово. В следующий раз дважды нажмите «Запустить» в папке KadrovyAgent или «Кадровый агент» на рабочем столе."
  exec "$dir/Запустить.command"
}

main
