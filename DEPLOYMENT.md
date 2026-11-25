# Развёртывание VPN Watcher на Ubuntu через GitHub

Код отправляется с Windows из `D:\work\vpn_watcher` в репозиторий **MerrickGit/kvn**, ветку **master**. Ubuntu скачивает эту ветку в `~/vpn_watcher` и запускает сервис `bot` через Docker Compose. Секреты хранятся на сервере в `.env`, заявки — в томе Docker.

Для первого запуска нужны SSH-доступ к Ubuntu под пользователем с `sudo`, доступ к репозиторию GitHub и административный доступ к работающей панели 3x-ui **3.11.0**. На сервере должен быть доступ к GitHub, реестру образов Docker, PyPI, Telegram Bot API и адресу панели.

## 1. Windows · отправить файлы в GitHub

Откройте PowerShell. В этом проекте Git уже инициализирован и связан с GitHub через HTTPS. Проверьте каталог, ветку и изменения:

```powershell
Set-Location -LiteralPath 'D:\work\vpn_watcher'
git status --short --branch
git remote get-url origin
git check-ignore .env
```

Ожидаемая ветка — `master`, адрес заканчивается на `MerrickGit/kvn.git` или `MerrickGit/kvn`. Последняя команда должна вывести `.env`. Для публикации этой инструкции и ссылки на неё выполните:

```powershell
git add -- DEPLOYMENT.md README.md
git diff --cached --stat
git diff --cached --name-only
```

Убедитесь, что в списке только выбранные файлы. Для будущих изменений добавляйте конкретные файлы кода тем же способом. Рабочий `.env`, приватные SSH-ключи и копии SQLite держите вне коммитов. Затем отправьте изменения:

```powershell
git commit -m "Добавить инструкцию по развёртыванию на Ubuntu"
git push -u origin master
git rev-parse --short HEAD
```

Если все файлы уже закоммичены, достаточно `git push origin master`. При запросе авторизации войдите в GitHub через окно Git Credential Manager. Откройте ветку `master` на GitHub и сверьте короткий хеш последнего коммита. [Репозиторий проекта](https://github.com/MerrickGit/kvn) · [Публикация локального проекта](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)

## 2. Ubuntu · подключиться и подготовить Docker

В PowerShell подключитесь к серверу. Подставьте своего SSH-пользователя и адрес вместо примера. Далее все блоки с пометкой Bash выполняются **в SSH-сеансе Ubuntu**.

```powershell
ssh deploy@203.0.113.10
```

Проверьте установленное ПО:

```bash
cat /etc/os-release
git --version
sudo docker version
sudo docker compose version
```

Если Docker показывает версии клиента и сервера, а Compose — свою версию, переходите к включению автозапуска ниже. Если отсутствует только Git, достаточно `sudo apt update && sudo apt install -y git`.

**Для чистого сервера Ubuntu** установите Docker Engine и плагин Compose из репозитория Docker. Если Docker уже обслуживает 3x-ui, сохраните работающую установку. При отсутствующем плагине Compose доустановите пакет для того же источника, откуда установлен сам Docker. [Установка Docker Engine для Ubuntu](https://docs.docker.com/engine/install/ubuntu/)

```bash
sudo apt update
sudo apt install -y ca-certificates curl git openssh-client nano
sudo install -d -m 0755 /etc/apt/keyrings
sudo curl --fail --silent --show-error --location https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod 0644 /etc/apt/keyrings/docker.asc

. /etc/os-release
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Включите автозапуск системного Docker и проверьте результат. У сервиса бота в `compose.yaml` уже задано `restart: unless-stopped`. После штатной перезагрузки Docker поднимет работавший контейнер. Ручная остановка сохранится до следующего `docker compose up -d bot`. [Автозапуск Docker на Linux](https://docs.docker.com/engine/install/linux-postinstall/#configure-docker-to-start-on-boot-with-systemd)

```bash
sudo systemctl enable --now docker.service containerd.service
systemctl is-enabled docker.service
sudo docker compose version
```

## 3. Ubuntu и GitHub · скачать проект

**Для публичного репозитория** достаточно одной команды на Ubuntu:

```bash
git clone --branch master https://github.com/MerrickGit/kvn.git "$HOME/vpn_watcher"
```

**Для приватного репозитория** настройте ключ развёртывания. Команды Git и SSH выполняйте под одним и тем же пользователем Ubuntu.

1. На Ubuntu создайте отдельную пару ключей. Если файл `~/.ssh/vpn_watcher_github` уже существует и его публичный ключ прикреплён к этому репозиторию, используйте существующую пару. При ручных обновлениях можно указать парольную фразу и вводить её при обращении к GitHub.

```bash
install -d -m 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/vpn_watcher_github -C "vpn-watcher Ubuntu"
cat ~/.ssh/vpn_watcher_github.pub
```

2. В GitHub откройте **MerrickGit/kvn → Settings → Deploy keys → Add deploy key**. Вставьте выведенную строку `.pub`, задайте название сервера и оставьте флажок **Allow write access** снятым. Приватный ключ остаётся на Ubuntu. [Deploy keys в GitHub](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
3. На Ubuntu клонируйте проект. Команда сохранит выбор ключа и для последующих `git pull`. При первом SSH-соединении сверьте показанный отпечаток GitHub с официальным списком. [Отпечатки SSH-ключей GitHub](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)

```bash
git clone --branch master --config "core.sshCommand=ssh -i ~/.ssh/vpn_watcher_github -o IdentitiesOnly=yes" git@github.com:MerrickGit/kvn.git "$HOME/vpn_watcher"
```

Для обоих вариантов проверьте результат:

```bash
cd ~/vpn_watcher
git status --short --branch
git rev-parse --short HEAD
```

Хеш должен совпасть с тем, который вы отправили с Windows. Если рабочая копия на сервере уже существует, используйте её каталог и порядок обновления из раздела 6. Имя каталога по умолчанию влияет на имя проекта Compose и его тома SQLite.

## 4. Ubuntu · заполнить настройки

Создайте `.env` с правами только для своего пользователя. Команда сохраняет ранее созданный файл. В `nano` сохранение — **Ctrl+O**, затем **Enter**, выход — **Ctrl+X**.

```bash
cd ~/vpn_watcher
test -e .env || install -m 600 .env.example .env
chmod 600 .env
nano .env
```

| Поле | Откуда взять значение |
|---|---|
| `BOT_TOKEN` | Создайте отдельного бота командой `/newbot` у официального `@BotFather`. Вставьте выданный токен. У встроенного бота 3x-ui должен быть свой токен. [Инструкция Telegram](https://core.telegram.org/bots/features#creating-a-new-bot) |
| `ADMIN_ID` | Числовой ID вашего **личного** аккаунта Telegram. Если он неизвестен, в Telegram Desktop откройте **Настройки → Продвинутые настройки → Экспорт данных из Telegram**, выберите информацию об аккаунте и формат JSON. В файле `result.json` возьмите число из `personal_information.user_id`. [Формат экспорта Telegram Desktop](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/export/output/export_output_json.cpp) |
| `PANEL_URL` | Полный адрес панели, доступный из контейнера, например `https://panel.example.org:8443/my-panel`. Укажите фактический порт и базовый путь. Суффикс `/panel/api/clients/` бот добавляет сам. Внутри контейнера `localhost` указывает на контейнер бота |
| `PANEL_TOKEN` | В разделе API-токенов 3x-ui создайте отдельный токен с областью `admin`. Сразу скопируйте выданное значение. [API-токены 3x-ui 3.11.0](https://github.com/MHSanaei/3x-ui/blob/v3.11.0/docs/content/docs/en/reference/api/api-tokens.mdx) |

Остальные значения можно оставить из `.env.example`. По умолчанию напоминание приходит за три дня, время показывается по Москве. Бот принимает обновления через long polling. Публиковать для него входной порт или настраивать webhook не требуется.

## 5. Ubuntu и Telegram · запустить и проверить

```bash
cd ~/vpn_watcher
sudo docker compose config --quiet && sudo docker compose up -d --build bot
sudo docker compose ps bot
sudo docker compose logs --tail=50 bot
```

Ожидаемый результат — статус **Up**, а в журнале начат опрос Telegram. После этого проверьте весь путь на **тестовом клиенте 3x-ui**:

1. С администраторского аккаунта отправьте боту `/start`, затем `/id`. Сверьте ID с `ADMIN_ID`. В меню должна быть кнопка **«Ожидают решения»**.
2. Со второго аккаунта откройте личный чат, нажмите **«Регистрация»** и введите Email тестового клиента. Подтвердите привязку с администраторского аккаунта. В 3x-ui у клиента должен появиться Telegram ID второго аккаунта.
3. В пользовательском меню нажмите **«Моя подписка» → «Я оплатил»**. Подтвердите тестовую заявку кнопкой **«Продлить на 30 дней»** и сверьте дату в Telegram и панели. Это действие действительно меняет срок выбранного клиента.

После очередной штатной перезагрузки зайдите по SSH и выполните `cd ~/vpn_watcher && sudo docker compose ps bot`. Для проверки ответа отправьте `/start` в Telegram.

## 6. Как доставлять следующие обновления

**На Windows** сначала просмотрите изменения. Для уже отслеживаемого кода `git add -p` позволяет выбрать изменения по частям. Новые файлы добавляйте командой `git add -- имя_файла`. Список для коммита должен содержать только файлы, которые вы намерены опубликовать.

```powershell
Set-Location -LiteralPath 'D:\work\vpn_watcher'
git status --short --branch
git add -p
git diff --cached --stat
git commit -m "Описание изменения"
git push origin master
git rev-parse --short HEAD
```

**На Ubuntu** перед обновлением сохраните консистентную копию SQLite и хеш текущей версии. Каталог копий находится **вне репозитория**. Утилита бота проверяет целостность копии.

```bash
cd ~/vpn_watcher
install -d -m 700 ~/vpn_watcher_backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_file="watcher-$stamp.sqlite3"
sudo docker compose exec -T bot python -m watcher --backup "/tmp/$backup_file" &&
  sudo docker compose cp "bot:/tmp/$backup_file" "$HOME/vpn_watcher_backups/$backup_file" &&
  git rev-parse HEAD > "$HOME/vpn_watcher_backups/$stamp.commit"
```

После успешного копирования получите версию из GitHub и пересоберите сервис. Выполняйте команды в том же каталоге. Так сохраняются `.env` и существующий том с заявками.

```bash
cd ~/vpn_watcher
git status --short --branch
git pull --ff-only origin master &&
  sudo docker compose config --quiet &&
  sudo docker compose up -d --build bot
sudo docker compose ps bot
git rev-parse --short HEAD
```

Сверьте хеш с Windows и проверьте **«Моя подписка»** в Telegram. Если Git сообщает о расхождении веток либо локальных изменениях, сначала разберите их. Для обновления контейнера не используйте `docker compose down -v` — эта команда удаляет том с заявками. Если изменился только `.env`, достаточно `sudo docker compose up -d bot`.

**Если требуется откат кода**, выберите нужный файл `.commit` в `~/vpn_watcher_backups`. Для совместимой схемы SQLite можно переключиться на сохранённый коммит командой `git switch --detach ХЕШ` и выполнить `sudo docker compose up -d --build bot`. Для следующего обычного обновления вернитесь на ветку `git switch master`. Если в выпуске изменилась схема данных, используйте процедуру восстановления базы из README и сверьте обработанные заявки перед запуском старой версии.

## 7. Если запуск остановился

| Симптом | Что проверить |
|---|---|
| `Permission denied (publickey)` при клонировании или обновлении | Ключ `.pub` добавлен в Deploy keys репозитория `MerrickGit/kvn`. Git запускается под тем же пользователем Ubuntu, который создал ключ |
| В журнале Telegram `Unauthorized` или `Conflict` | Для `Unauthorized` сверьте токен с BotFather. Для `Conflict` проверьте, не работает ли второй экземпляр с этим токеном, в том числе на Windows |
| Бот отвечает, но администратор не получает заявки | Откройте личный чат с ботом, нажмите `/start`, сравните ответ `/id` с `ADMIN_ID`. После исправления `.env` выполните `sudo docker compose up -d bot` |
| Регистрация доходит, но карточка 3x-ui не читается | Проверьте базовый путь и порт `PANEL_URL`, доступ из контейнера, доверие к HTTPS-сертификату, срок действия и права `PANEL_TOKEN`. В журнале используйте `sudo docker compose logs --tail=100 bot` |
