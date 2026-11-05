
set -e
GITHUB_USER="arhman92"
REPO="mybot"
BRANCH="main"

DEST="/usr/local/bin/mybot"
RAW="https://raw.githubusercontent.com/${GITHUB_USER}/${REPO}/${BRANCH}/mybot"

case "$0" in
  */install.sh|install.sh) HERE="$(cd "$(dirname "$0")" && pwd)" ;;
  *)                       HERE="" ;;
esac

if [ "$(id -u)" != "0" ]; then
  echo "Нужны права root. Запусти от root или через sudo."
  exit 1
fi

# screen нужен: боты живут в его сессиях.
if ! command -v screen >/dev/null 2>&1; then
  echo "Ставлю screen…"
  dnf install -y screen >/dev/null 2>&1 || yum install -y screen >/dev/null 2>&1 \
    || apt-get install -y screen >/dev/null 2>&1 || true
fi
if ! command -v screen >/dev/null 2>&1; then
  echo "screen не установился. Поставь вручную:  dnf install -y screen"
  exit 1
fi

# Берём mybot: сначала рядом с собой, иначе качаем с GitHub.
if [ -n "$HERE" ] && [ -f "$HERE/mybot" ]; then
  install -m 755 "$HERE/mybot" "$DEST"
  echo "Взял mybot из папки рядом."
else
  echo "Качаю mybot с GitHub…"
  if ! curl -fsSL "$RAW" -o /tmp/mybot.$$; then
    echo "Не скачалось: $RAW"
    echo "Проверь, что в install.sh вписан твой логин GitHub"
    echo "и что репозиторий публичный."
    exit 1
  fi
  # Проверка, что приехал скрипт, а не страница ошибки.
  if ! head -1 /tmp/mybot.$$ | grep -q '^#!'; then
    echo "Скачалось что-то не то — похоже, страница ошибки."
    rm -f /tmp/mybot.$$
    exit 1
  fi
  install -m 755 /tmp/mybot.$$ "$DEST"
  rm -f /tmp/mybot.$$
fi

# backup.py — рядом с mybot, в своей папке. Его зовёт и меню, и cron.
mkdir -p /usr/local/lib/mybot
if [ -n "$HERE" ] && [ -f "$HERE/backup.py" ]; then
  install -m 644 "$HERE/backup.py" /usr/local/lib/mybot/backup.py
  echo "Взял backup.py из папки рядом."
else
  if curl -fsSL "https://raw.githubusercontent.com/${GITHUB_USER}/${REPO}/${BRANCH}/backup.py" \
       -o /tmp/backup.py.$$ 2>/dev/null \
     && head -1 /tmp/backup.py.$$ | grep -q "coding"; then
    install -m 644 /tmp/backup.py.$$ /usr/local/lib/mybot/backup.py
  else
    echo "Внимание: backup.py не скачался — раздел копий работать не будет."
  fi
  rm -f /tmp/backup.py.$$
fi

mkdir -p /root/4idea/Artemis/logs 2>/dev/null || true
mkdir -p /root/backups 2>/dev/null || true

echo
echo "Готово. Команда установлена: $DEST"
echo
echo "Проверь:"
echo "    mybot status"
echo "    mybot"
echo
echo "Перезагрузки серверу не страшны: это обычная команда в /usr/local/bin."
echo "Боты при этом сами НЕ поднимаются — поднимешь их из меню."
