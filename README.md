# mrcga_bot

Telegram-бот для разделения совместных покупок.

## Автозапуск на Ubuntu через Podman

Ниже используется rootless Podman и systemd Quadlet. Сервис запускается при
загрузке системы, работает без входа пользователя и перезапускается через 10
секунд после любого завершения процесса.

При сетевой ошибке Telegram бот вызывает штатный
`Application.stop_running()`. После корректного завершения приложения systemd
запускает контейнер заново согласно `Restart=always`. Это также пересоздаёт
зависшие соединения polling и прокси.

### 1. Проверить Podman и Quadlet

```bash
podman --version
podman info --format '{{.Host.CgroupsVersion}}'
```

Для Quadlet нужен cgroup v2. Если после установки unit-файлов сервис не
появится, проверьте ошибки генератора:

```bash
systemd-analyze --user --generators=true verify mrcga-bot.service
```

### 2. Собрать образ

Выполните из корня репозитория:

```bash
podman build --pull -t localhost/mrcga-bot:latest .
```

Сборка использует зафиксированный образ `uv` и выполняет
`uv sync --locked --no-dev`. Поэтому версии зависимостей берутся из `uv.lock`
и готовое виртуальное окружение `.venv` создаётся непосредственно внутри
образа. При несовпадении `pyproject.toml` и `uv.lock` сборка завершится ошибкой.

### 3. Установить Quadlet-файлы

```bash
mkdir -p "$HOME/.config/containers/systemd"

install -m 0644 deploy/quadlet/mrcga-bot.container \
  "$HOME/.config/containers/systemd/mrcga-bot.container"

install -m 0644 deploy/quadlet/mrcga-bot-data.volume \
  "$HOME/.config/containers/systemd/mrcga-bot-data.volume"

install -m 0600 deploy/quadlet/mrcga-bot.env.example \
  "$HOME/.config/containers/systemd/mrcga-bot.env"
```

Откройте файл с переменными окружения и замените значение токена:

```bash
nano "$HOME/.config/containers/systemd/mrcga-bot.env"
```

Минимальное содержимое:

```dotenv
TELEGRAM_BOT_TOKEN=ваш_токен
```

Файл имеет права `0600` и не должен попадать в Git.

### 4. Разрешить пользовательским сервисам работать после перезагрузки

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" --property=Linger
```

Вторая команда должна вывести `Linger=yes`.

### 5. Запустить бота

```bash
systemctl --user daemon-reload
systemctl --user start mrcga-bot.service
systemctl --user status mrcga-bot.service
```

Вызывать `systemctl --user enable` не нужно: Quadlet применяет секцию
`[Install]` при генерации сервиса.

Логи:

```bash
journalctl --user -u mrcga-bot.service -f
```

После этого можно перезагрузить сервер и проверить состояние:

```bash
sudo reboot

# После повторного подключения:
systemctl --user status mrcga-bot.service
```

Состояние бота хранится в Podman volume `mrcga-bot-data` и сохраняется при
пересоздании контейнера и обновлении образа.

## Обновление

После получения нового кода:

```bash
git pull
podman build --pull -t localhost/mrcga-bot:latest .
systemctl --user restart mrcga-bot.service
```

## Управление

```bash
systemctl --user restart mrcga-bot.service
systemctl --user stop mrcga-bot.service
systemctl --user start mrcga-bot.service
journalctl --user -u mrcga-bot.service --since today
```

Не удаляйте volume `mrcga-bot-data`, если нужно сохранить незавершённый чек и
список известных пользователей.

Фиксированный формат CSV
Кодировка строго UTF-8, разделитель — запятая, заголовок строго:
```
name,quantity,unit_price,total
Например:
name,quantity,unit_price,total
УГОЛЬ SPAR ДРЕВЕСНЫЙ,1,359.90,359.90
ВОДА ПИТЬЕВАЯ SPAR,1,72.90,72.90
ПИВО БАКАЛАР ХОЛОДНО,1,169.90,169.90
ПИВО БАКАЛАР ХОЛОДНО,1,169.90,169.90
КРЫЛЫШКИ КУРИНЫЕ,2.284,399.90,913.37
НАПИТОК ГАЗИРОВАННЫЙ,1,159.90,159.90
АРБУЗ,8.77,31.90,279.76
```
name — название позиции, quantity — количество/вес, unit_price — цена за единицу, total — фактическая сумма позиции. Именно total используется при расчётах, поэтому округления вроде 8.77 × 31.90 = 279.76 не создают проблем. Наименования позиций должны быть в кавычках что бы избежать конфликта при парсинге csv если в названии содержится символ разделителя csv. Файл csv должен быть доступен для скачивания.
