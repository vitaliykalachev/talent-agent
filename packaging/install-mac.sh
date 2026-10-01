#!/bin/bash
# Установщик «Кадрового агента» для Mac на Apple Silicon. Клиент запускает его одной
# командой в Терминале: curl -fsSL <адрес>/install-mac.sh | bash
# Скачанное через curl не получает карантина, поэтому macOS не спрашивает про каждый файл.
# Ставим в ~/KadrovyAgent: к «Рабочему столу», «Документам» и «Загрузкам» Терминал
# просит отдельный доступ, к домашней папке — нет. Прежнюю версию не удаляем, а переносим
# в ~/KadrovyAgent.old-<дата-время>; копия её папки data (база, загрузки) едет в новую.
URL="__ПОДСТАВИТЬ__"

main() {
  set -e
  [ -n "${HOME:-}" ] || { echo "Не задана домашняя папка, установить некуда."; exit 1; }
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

  # Работающий агент остался бы жить из перенесённой папки, и следующий «Запустить»
  # открывал бы его, а не новую версию. Порты — те же, что перебирает агент (TA_PORT и +10).
  local first=${TA_PORT:-8000} port
  for port in $(seq "$first" $((first + 10))); do
    if curl -s -m 2 -o /dev/null -D - "http://127.0.0.1:$port/health" 2>/dev/null |
      grep -qi '^x-agent-instance:'; then
      echo "Кадровый агент сейчас запущен. Закройте его окно в Терминале и запустите команду ещё раз."
      exit 1
    fi
  done

  local dir="$HOME/KadrovyAgent" new="$HOME/KadrovyAgent.new" archive old=""
  archive=$(mktemp "${TMPDIR:-/tmp}/kadrovyi-agent.XXXXXX")  # mktemp -t в macOS не смотрит на TMPDIR
  # Недокачанный архив и недораспакованная папка не остаются ни при каком выходе
  trap 'rm -rf "$archive" "$new"' EXIT
  echo "Скачиваем «Кадровый агент», около 800 МБ. Это займёт несколько минут."
  if ! curl -fL --progress-bar "$URL" -o "$archive"; then
    echo "Не удалось скачать архив. Проверьте интернет и повторите команду."
    exit 1
  fi
  echo "Распаковываем…"
  rm -rf "$new"
  if ! ditto -x -k "$archive" "$new" || [ ! -x "$new/KadrovyAgent/Запустить.command" ]; then
    echo "Архив не распаковался: он скачался не полностью или на диске мало места. Прежняя версия не тронута, повторите команду."
    exit 1
  fi
  xattr -dr com.apple.quarantine "$new" 2>/dev/null || true
  # Данные клиента — из прежней версии вместо демо-базы из архива. Копия, а не перенос:
  # прежняя версия со своими данными остаётся целой. Копируем ещё в распакованную папку:
  # не скопировалось (место, права) — она уберётся при выходе, установленная не тронута.
  # Веса модели поиска берём из нового архива.
  local data="$new/KadrovyAgent/data" item kept="" err
  if [ -f "$dir/data/app.db" ]; then
    mkdir -p "$data"
    for item in "$data"/*; do
      [ "$(basename "$item")" = models ] || rm -rf "$item"
    done
    for item in "$dir/data"/*; do
      case "$(basename "$item")" in
        models | agent.lock) ;;
        *)
          if ! err=$(cp -R "$item" "$data/" 2>&1); then
            echo "Не удалось перенести данные (${err%%$'\n'*}). Прежняя версия не тронута."
            exit 1
          fi
          ;;
      esac
    done
    kept=1
  fi
  if [ -e "$dir" ]; then
    old="$dir.old-$(date +%Y%m%d-%H%M%S)"
    mv "$dir" "$old"
  fi
  mv "$new/KadrovyAgent" "$dir"
  rm -rf "$archive" "$new"
  trap - EXIT
  xattr -dr com.apple.quarantine "$dir" 2>/dev/null || true

  # Ярлык на рабочем столе. Копия «Запустить.command» там не сработала бы: она ищет
  # программу рядом с собой. При первой записи на рабочий стол macOS спрашивает, дать ли
  # Терминалу доступ к нему; при отказе ярлыка не будет, установка продолжится.
  echo "macOS может спросить, разрешить ли Терминалу доступ к рабочему столу — нажмите «Разрешить», это нужно только для ярлыка."
  local shortcut="$HOME/Desktop/Кадровый агент.command"
  { printf '#!/bin/bash\nexec "$HOME/KadrovyAgent/Запустить.command"\n' >"$shortcut" &&
    chmod +x "$shortcut"; } 2>/dev/null || true

  if [ "${TA_OPEN_BROWSER:-1}" != 0 ]; then  # в проверках Finder не открываем
    open "$dir"
  fi
  echo "Готово. В следующий раз дважды нажмите «Запустить» в папке KadrovyAgent или «Кадровый агент» на рабочем столе."
  if [ -n "$kept" ]; then
    echo "Ваши данные перенесены."
  fi
  if [ -n "$old" ]; then
    echo "Прежняя версия и её данные сохранены в $old."
  fi
  exec "$dir/Запустить.command"
}

main
