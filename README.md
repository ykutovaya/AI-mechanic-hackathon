# Цифровой механик

Веб-агент объясняет результаты диагностики двигателя, отвечает по руководствам и отправляет подтверждённые заявки в тестовую ТОиР. [Краткая постановка](documents/task.md)

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

## Подготовка ВМ

Для стенда выделена ВМ с 4 vCPU, 8 ГиБ оперативной памяти и SSD на 40 ГиБ.

На ВМ с Ubuntu 24.04 войдите под обычным пользователем с доступом к `sudo`. Из корня репозитория проверьте окружение без скачивания:

```bash
bash scripts/prepare-vm.sh --check
```

Если не хватает зависимостей или файлов моделей, установите их и повторите проверку:

```bash
bash scripts/prepare-vm.sh && bash scripts/prepare-vm.sh --check
```

При успешной проверке скрипт выводит «Проверка пройдена.»

## Извлечение текста из руководства

При первой настройке из корня репозитория создайте окружение и установите зависимости. Понадобится Python 3.11 или новее.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Создайте реестр выданных руководств:

```powershell
.\.venv\Scripts\python.exe scripts/create_manual_registry.py
```

Скрипт сверяет число страниц с ТЗ v6 и записывает обозначения, редакции, модели двигателей и SHA-256 в `data/annotations/manuals.json`.

Для `--ocr-page auto` установите Tesseract и проверьте наличие `rus` в выводе `tesseract --list-langs`. Обработайте одно руководство:

```powershell
.\.venv\Scripts\python.exe -m ai_mechanic.knowledge.pdf "data/raw/manuals/zmz-5234_rk_2025.pdf" --registry data/annotations/manuals.json --output data/processed/pages.jsonl --ocr-page auto
```

Скрипт создаст `pages.jsonl` при первом успешном запуске. При повторной обработке заменятся записи выбранного PDF. Если разметка уже готова, передайте её путь через `--notes`. Текст OCR сохраняется отдельно со статусом `pending` до сверки. В разметке `verified_text` задаёт сверенный текст страницы и требует `source_sha256` исходного PDF.

Проверки извлечения PDF, сборки и поиска:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Пробная сборка индекса Qdrant

После обновления зависимостей командой `pip install -e .` запустите Docker Desktop. При первом запуске создайте контейнер с отдельным томом:

```powershell
docker run -d --name mechanic-hackathon-qdrant --publish 127.0.0.1:6335:6333 --mount type=volume,source=mechanic-hackathon-qdrant,target=/qdrant/storage qdrant/qdrant:v1.19.2
```

При повторном запуске контейнера используйте `docker start mechanic-hackathon-qdrant`.

Подготовьте `manuals.json`, `pages.jsonl` и `page_notes.json`. Если файлы моделей лежат в другом месте, задайте `AI_MECHANIC_E5_DIR` для E5 Small и `FASTEMBED_CACHE_PATH` для кэша BM25. По умолчанию используются `~/.cache/ai-mechanic/e5-small` и `~/.cache/fastembed`. На ВМ их готовит [prepare-vm.sh](scripts/prepare-vm.sh). В каталоге E5 Small нужны `tokenizer.json`, `onnx/model.onnx` и файл `REVISION` со значением `614241f622f53c4eeff9890bdc4f31cfecc418b3`.

Соберите пробный индекс и проверьте физическую страницу в выдаче:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m ai_mechanic.knowledge.qdrant_index build --url http://127.0.0.1:6335 --collection mechanic_hackathon_smoke --document zmz-5234-10-rk-2025 --page 9 --page 23 --page 102 --page 103
.\.venv\Scripts\python.exe -X utf8 -m ai_mechanic.knowledge.qdrant_index search --url http://127.0.0.1:6335 --collection mechanic_hackathon_smoke --engine 'ЗМЗ-5234.10' --query 'Какое моторное масло использовать в двигателе?'
```

Для поиска по карточке машины используйте `--vehicle-id` вместо `--engine`. Команда возьмёт `engine_model` из `data/raw/vehicles.json`. Передавайте `--variant` при подтверждённом исполнении. При неизвестном исполнении поиск вернёт общие фрагменты этой модели.

Пути входных файлов можно переопределить через `--registry`, `--pages` и `--notes`. Ограниченную сборку запускайте с отдельным псевдонимом. Псевдоним `mechanic_hackathon` используйте для полной сборки после приёмки.
