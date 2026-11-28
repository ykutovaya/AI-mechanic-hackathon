# База знаний и проверка поиска

## Извлечение текста из руководства

Сначала установите зависимости по [инструкции запуска](setup.md). В командах ниже для Ubuntu замените `.\.venv\Scripts\python.exe` на `.venv/bin/python`, а порт Qdrant 6335 на 6333.

Создайте реестр выданных руководств:

```powershell
.\.venv\Scripts\python.exe scripts/create_manual_registry.py
```

Скрипт сверяет число страниц с ТЗ v6 и записывает обозначения, редакции, модели двигателей и SHA-256 в `data/annotations/manuals.json`.

Для `--ocr-page auto` установите Tesseract и проверьте наличие `rus` в выводе `tesseract --list-langs`. Обработайте одно руководство:

```powershell
.\.venv\Scripts\python.exe -m ai_mechanic.knowledge.pdf "data/raw/manuals/zmz-5234_rk_2025.pdf" --registry data/annotations/manuals.json --output data/processed/pages.jsonl --ocr-page auto
```

Повторите команду для остальных пяти PDF, подставляя их исходные имена. Скрипт создаст `pages.jsonl` при первом успешном запуске. При повторной обработке заменятся записи выбранного PDF. Если разметка уже готова, передайте её путь через `--notes`. Текст OCR сохраняется отдельно со статусом `pending` до сверки. В разметке `verified_text` задаёт сверенный текст страницы и требует `source_sha256` исходного PDF.

Проверки извлечения PDF, сборки и поиска:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Пробная сборка индекса Qdrant

Запустите Qdrant по [инструкции настройки](setup.md).

Подготовьте `manuals.json`, `pages.jsonl` и `page_notes.json`. Если файлы моделей лежат в другом месте, задайте `AI_MECHANIC_E5_DIR` для E5 Small и `FASTEMBED_CACHE_PATH` для кэша BM25. По умолчанию используются `~/.cache/ai-mechanic/e5-small` и `~/.cache/fastembed`. На ВМ их готовит [prepare-vm.sh](../scripts/prepare-vm.sh). В каталоге E5 Small нужны `tokenizer.json`, `onnx/model.onnx` и файл `REVISION` со значением `614241f622f53c4eeff9890bdc4f31cfecc418b3`.

Соберите пробный индекс и проверьте физическую страницу в выдаче:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m ai_mechanic.knowledge.qdrant_index build --url http://127.0.0.1:6335 --collection mechanic_hackathon_smoke --document zmz-5234-10-rk-2025 --page 9 --page 23 --page 102 --page 103
.\.venv\Scripts\python.exe -X utf8 -m ai_mechanic.knowledge.qdrant_index search --url http://127.0.0.1:6335 --collection mechanic_hackathon_smoke --engine 'ЗМЗ-5234.10' --query 'Какое моторное масло использовать в двигателе?'
```

Для поиска по карточке машины используйте `--vehicle-id` вместо `--engine`. Команда возьмёт `engine_model` из `data/raw/vehicles.json`. Передавайте `--variant` при подтверждённом исполнении. При неизвестном исполнении поиск вернёт общие фрагменты этой модели.

Пути входных файлов можно переопределить через `--registry`, `--pages` и `--notes`. Ограниченную сборку запускайте с отдельным псевдонимом. Псевдоним `mechanic_hackathon` используйте для полной сборки после приёмки.

## Индекс шести руководств

Положите экспертную таблицу в `data/annotations/defect_sources.xlsx`. После извлечения всех PDF соберите отдельного кандидата. Запустите заглушку ML для проверки диалога.

```powershell
.\.venv\Scripts\python.exe -X utf8 -m ai_mechanic.knowledge.qdrant_index build --url http://127.0.0.1:6335 --collection mechanic_hackathon_expert_chat --expert-sources data/annotations/defect_sources.xlsx --expert-report data/processed/expert_sources_chat_report.json
.\.venv\Scripts\python.exe -X utf8 tests/check_e5_pilot.py --qdrant-url http://127.0.0.1:6335 --collection mechanic_hackathon_expert_chat --cases tests/fixtures/check_search_candidate.json --output data/processed/search_quality_expert_chat.json
```

После успешной проверки кандидата повторите полную сборку с теми же входными файлами и `--collection mechanic_hackathon`. Этот псевдоним использует файл запуска.

В `Выдержке` можно сохранить дословный текст или проверенный пересказ. Для пересказа укажите физические страницы в `Страницы PDF` и номер пункта в `Раздел`. Если этого недостаточно, добавьте необязательную колонку `Место в источнике`. Примеры:

- `PDF 33: пункт 5`
- `PDF 33: абзац «Последовательность регулировки клапанов»`
- `PDF 101: строка «Трещины или обломы» | графа «Заключение»`

Замените номера и названия на указанные в вашем PDF. Если факты относятся к разным шагам или ячейкам, укажите их отдельными строками. Колонка `Исполнение` необязательна. Для инструкции, которая зависит от исполнения, укажите подтверждённое исполнение по разметке PDF.

В Qdrant хранятся текст руководств и указатель по коду неисправности и двигателю. Параметр `--expert-sources` включает импорт Excel при сборке. После правки таблицы повторите сборку. В `data/processed/expert_sources_chat_report.json` у каждой строки указаны результат и физические страницы PDF.

Чтобы проверить ссылки всех доступных строк через веб-чат, запустите `.\.venv\Scripts\python.exe -X utf8 tests/check_expert_catalog.py`. Скрипт использует работающие Qdrant и заглушку ML. Результат появится в `data/processed/expert_chat_coverage.json`.

После диагностики спросите в чате «Покажи источники по этой неисправности». У каждой ссылки видно, сверена ли выдержка, найден ли тематический фрагмент или указана только страница для чтения. Технические рекомендации чат берёт из применимого текста руководства. Страница для чтения сама по себе не даёт основания добавить работу в заявку.

## Проверка качества поиска и ответов

Запустите Qdrant с пробной коллекцией и заглушку ML. Из корня проекта выполните команду.

```powershell
.\.venv\Scripts\python.exe -X utf8 tests/check_e5_pilot.py --qdrant-url http://127.0.0.1:6335 --collection mechanic_hackathon_smoke --cases tests/fixtures/check_search_smoke.json --output data/processed/search_quality_smoke.json
```

В файле сценариев для каждого вопроса укажите `vehicle_id` и `session_id` из `data/raw/vehicles.json`. Для проверяемого факта задайте обязательные слова или значения с единицами в `terms` и его физические страницы в `sources`. Для проверки отказа укажите `insufficient_evidence: true` и пустой список `facts`. Если сценарию нужен результат ML, добавьте `diagnose: true`. Адрес задаёт `MOCK_ML_URL` или параметр `--ml-url`.

Отчёт отдельно показывает первые пять страниц основного запроса, факты в выдаче всех уточняющих запросов, отобранные выдержки, факты со ссылками в ответе и результат открытия страниц по HTTP. Код завершения `1` означает, что в сохранённом отчёте есть расхождения. Код `2` означает ошибку запуска проверки.
