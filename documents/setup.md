# Подготовка и запуск

## Материалы организаторов

После клонирования возьмите выданный комплект и разместите файлы по указанным ниже путям от корня проекта.

| Путь | Содержимое |
| --- | --- |
| `data/raw/requirements/` | Оригиналы DOCX. Описание кейса v2, ТЗ НейроДВС v6 и описание бизнес-процессов v6. |
| `documents/requirements/` | Markdown-копии этих документов с исходными названиями. Для сверки требований и приёмки используйте локальный файл `documents/requirements/ТЗ_хакатон_ИИкузница_НейроДВС_v6_2026-11-16.md`. |
| `data/raw/manuals/` | Шесть выданных PDF с исходными именами. |
| `data/raw/vehicles.json`, `data/raw/fault_types.json` | Список машин с историей проверок и справочник неисправностей. Если они лежат в архиве заглушек, скопируйте их из `vendor/organizers/data/`. |
| `vendor/organizers/` | Архив заглушек ML и ТОиР в распакованном виде. Сохраните его структуру, включая OpenAPI и каталог `data/`. После распаковки откройте `vendor/organizers/README.md` с командами запуска. |

Комплект и Markdown-копии требований хранятся локально. Git исключает `data/`, `vendor/` и `documents/requirements/`.

## Windows

Выполните команды в PowerShell из корня проекта. Понадобятся Python 3.11 или новее и запущенный Docker Desktop.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

python -m venv vendor/organizers/.venv
.\vendor\organizers\.venv\Scripts\python.exe -m pip install -r vendor/organizers/requirements.txt
```

## Ubuntu 24.04

Из корня репозитория проверьте окружение командой `bash scripts/prepare-vm.sh --check`. При первой установке выполните `bash scripts/prepare-vm.sh` и повторите проверку. Скрипт запускает Docker и готовит модели поиска. На публичном стенде запуском управляют systemd и Caddy.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .

python3 -m venv vendor/organizers/.venv
vendor/organizers/.venv/bin/python -m pip install -r vendor/organizers/requirements.txt
```

## Qdrant и чат

При первом запуске создайте Qdrant с отдельным томом.

```powershell
docker run -d --name mechanic-hackathon-qdrant --publish 127.0.0.1:6335:6333 --mount type=volume,source=mechanic-hackathon-qdrant,target=/qdrant/storage qdrant/qdrant:v1.19.2
```

На Ubuntu замените `127.0.0.1:6335:6333` на `127.0.0.1:6333:6333`. Если Docker требует `sudo`, выполните `sudo docker run` при создании или `sudo docker start mechanic-hackathon-qdrant` для уже созданного контейнера.

[Подготовьте корпус и рабочий индекс](knowledge-base.md), затем выполните [команду запуска для своей системы](../README.md#запуск).

Скрипт проверяет коллекцию `mechanic_hackathon` на порту 6335 в Windows или 6333 в Ubuntu, при необходимости запускает существующий контейнер и поднимает заглушки ML и ТОиР на портах 8001 и 8002. Другой адрес или коллекцию Qdrant можно указать через `QDRANT_URL` и `QDRANT_COLLECTION`. Адрес чата — `http://127.0.0.1:8765/`. Нажмите `Ctrl+C`, чтобы завершить чат и заглушки, запущенные этим сеансом. Управляйте Qdrant отдельно через `docker start mechanic-hackathon-qdrant` и `docker stop mechanic-hackathon-qdrant`.

### Ручной запуск

Ниже приведены команды PowerShell. На Ubuntu используйте `.venv/bin/python` и порт Qdrant 6333.

Для отладки откройте два терминала в `vendor/organizers/` и запустите по одной команде в каждом:

```powershell
.\.venv\Scripts\python.exe -m uvicorn mock_ml.app:app --host 127.0.0.1 --port 8001
.\.venv\Scripts\python.exe -m uvicorn mock_toir.app:app --host 127.0.0.1 --port 8002
```

В третьем терминале из корня проекта запустите чат:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m ai_mechanic.web --qdrant-url http://127.0.0.1:6335 --collection mechanic_hackathon --ml-url http://127.0.0.1:8001 --toir-url http://127.0.0.1:8002
```

Черновики и история заявок хранятся в `data/state/work_orders.sqlite`. Чтобы проверить весь маршрут, выберите машину `H275AA47` и проверку `S-0139`. Получите диагностику, откройте руководство и просмотрите черновик. После подтверждения отправки номер заявки появится в чате и истории машины.

## YandexGPT Pro 5.1

Для подключения к Yandex AI Studio задайте `YANDEX_FOLDER_ID` и `YANDEX_API_KEY` в окружении процесса чата. Для разовой проверки вместо ключа можно передать `YANDEX_IAM_TOKEN`. Настройки читаются при запуске. [Авторизация в Yandex AI Studio](https://aistudio.yandex.ru/ru/docs/ai-studio/api-ref/authentication)

После подключения модель разбирает каждую реплику механика в веб-чате. Для уточнений учитывается предыдущий вопрос из текущего диалога. Уточнённая формулировка дополняет поиск, когда исходный запрос не дал ответа. В диалоге о стуке ЗМЗ-5234.10 модель выделяет наблюдения механика с цитатами из его реплики. Приложение проверяет их, формирует ответ по руководству и отдельно оценивает подтверждение аварийного сигнала. Расход токенов на разбор доступен в поле ответа `analysis_usage`. При недоступности модели приложение отвечает на основе локального разбора и поиска.

В справочном ответе с несколькими проверенными выдержками о деталях, форсунках или марках масла модель определяет порядок текста. Чат сохраняет формулировки и номера страниц. Без настроек или при сбое сервис возвращает исходный ответ. При успешном запросе `/api/ask` отдаёт `synthesis: yandexgpt` и счётчики токенов в `generation_usage`.
