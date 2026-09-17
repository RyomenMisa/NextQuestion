# NextQuestion
**RAG-конвейер с графовым расширением, который превращает PDF-учебники в умную базу знаний Obsidian с вопросами на естественном языке и цитатами с указанием страниц.**
---
## Что это такое?
NextQuestion — инструмент для построения **личной базы знаний** из медицинских (и любых других) учебников. Он:
1. **Нарезает** PDF на смысловые фрагменты по оглавлению
2. **Генерирует** каталожные карточки (кратко + ключевые слова + вопросы) через LLM
3. **Собирает** заметки Obsidian с автоматическими wikilink между связанными темами
4. **Отвечает** на ваши вопросы, цитируя конкретные страницы учебника
**Результат:** вместо того чтобы листать учебник на 1500 страниц, вы вводите вопрос в терминале и получаете ответ с кликабельными ссылками на нужные заметки Obsidian.
---
## ✨ Возможности
- **Семантическая нарезка** — умное разбиение PDF по оглавлению или заголовкам
- **LLM-обогащение** — генерация каталожных карточек с кэшированием
- **Граф связей** — автоматические wikilink между связанными фрагментами
- **Хабы сущностей** — объединение упоминаний одного термина из разных глав
- **HyDE-поиск** — семантический поиск с генерацией гипотетических документов
- **Интерактивный REPL** — режим диалога «вопрос-ответ»
- **TUI-визард** — интерактивная настройка без ручного редактирования JSON
- **Экономия токенов** — кэширование вызовов LLM: повторные запуски бесплатны
- **Отказоустойчивость** — FTS5 с откатом на LIKE-поиск, ретраи при ошибках API
---
## Быстрый старт (3 минуты)
### 1. Склонируйте репозиторий и установите зависимости
```bash
git clone https://github.com/yourusername/nextquestion.git
cd nextquestion
pip install -r requirements.txt
```
### 2. Запустите визард настройки
```bash
python nextquestion.py init
```
Визард проведёт вас через:
- Выбор LLM-бэкенда (OpenAI, DeepSeek, Ollama и т.д.)
- Ввод API-ключа (Проверь свой API, модель и base URL!!!!! Их можно поменять вручную)
- Указание пути к Obsidian Vault
- Создание `config.json`
### 3. Загрузите первый PDF
```bash
python nextquestion.py chunk path/to/your/book.pdf
```
### 4. Сгенерируйте каталожные карточки
```bash
python nextquestion.py enrich
```
### 5. Постройте граф заметок
```bash
python nextquestion.py link
```
### 6. Задайте вопрос
```bash
python nextquestion.py query "что такое гломерулонефрит"
```
**Готово!** Откройте Obsidian → папка `RAG_Knowledge_Base` → вы увидите структуру заметок с автоматическими ссылками.
---
## Установка
### Требования
- Python 3.10+
- Obsidian (для просмотра результатов)
- Доступ к LLM API (OpenAI, OpenRouter, DeepSeek, локальная модель и т.д.)
### Зависимости
```bash
pip install pymupdf requests pydantic pyyaml
```
Или через `requirements.txt`:
```txt
pymupdf>=1.23.0
requests>=2.28.0
pydantic>=2.0.0
pyyaml>=6.0
```
### Опционально: локальная модель (Ollama)
```bash
# Установите Ollama: https://ollama.ai
ollama pull qwen2.5:14b (Например. Оцени возможности своего ПК)
```
В визарде выберите `Ollama (local)` — API-ключ не нужен.
---
## Конфигурация
После запуска `init` создаётся файл `config.json`. Вот все доступные поля:
```json
{
"vault_path": "/path/to/obsidian/vault",
"store_root": "RAG_Knowledge_Base",
"db_path": "nextquestion.db",
"cache_namespace": "nq-1.0",
"chunk_max_chars": 9000,
"chunk_min_chars": 400,
"api_key": "sk-...",
"base_url": "https://api.openai.com/v1",
"model": "gpt-4o-mini",
"folders": {
"excerpts": "Notes",
"sections": "Sections",
"entities": "Entities"
},
"labels": {
"brief": "Brief",
"answers": "Answers",
"keywords": "Keywords",
"full": "Full source text",
"links": "Links",
"chapter": "Chapter",
"prev": "Previous",
"next": "Next",
"topics": "Related topics & entities"
},
"hub_labels": {
"section": "Excerpts in this section",
"entity": "Entity covered in",
"entity_tail": "excerpts, possibly across different books"
},
"prompts": {
"enrich": "...",
"hyde": "...",
"answer": "..."
},
"entity_patterns": [
"\\bCD\\d+\\b",
"\\bIL-\\d+\\b"
]
}
```
### Описание полей
| Поле | Назначение | Пример |
|-------|---------|---------|
| `vault_path` | Путь к корню Obsidian Vault | `"/home/user/Documents/Obsidian"` |
| `store_root` | Подпапка внутри Vault для базы знаний | `"RAG_Knowledge_Base"` |
| `db_path` | Путь к базе SQLite | `"nextquestion.db"` |
| `cache_namespace` | Префикс кэша (меняйте при обновлении промптов) | `"nq-1.0"` |
| `chunk_max_chars` | Максимальный размер чанка | `9000` |
| `chunk_min_chars` | Минимальный размер чанка | `400` |
| `api_key` | API-ключ LLM | `"sk-..."` |
| `base_url` | URL эндпоинта API | `"https://api.openai.com/v1"` |
| `model` | Название модели | `"gpt-4o-mini"` |
| `folders` | Имена папок для заметок | `{"excerpts": "Notes", ...}` |
| `labels` | Подписи в заметках (настраиваются) | `{"brief": "Кратко", ...}` |
| `prompts` | Системные промпты для LLM | См. ниже |
| `entity_patterns` | Regex-паттерны для извлечения сущностей | `["\\bIL-\\d+\\b"]` |
### Переменные окружения
Можно задать их вместо редактирования `config.json`:
```bash
export OPENAI_API_KEY="sk-..."
export OPENAI_BASE_URL="https://api.openai.com/v1"
export RAG_MODEL="gpt-4o-mini"
export RAG_VAULT="/path/to/vault"
```
---
## Стадии работы
### 1. `chunk` — нарезка PDF
```bash
python nextquestion.py chunk path/to/book.pdf [--start-page 10] [--end-page 100] [--dry-run]
```
**Что делает:**
- Читает PDF через PyMuPDF
- Извлекает оглавление (TOC)
- Разбивает текст на смысловые фрагменты по заголовкам
- Сохраняет в таблицу `chunks_raw` (0 вызовов LLM)
**Флаги:**
- `--start-page`, `--end-page` — обработать только диапазон страниц
- `--dry-run` — показать, что будет сделано, без записи в БД
**Пример:**
```bash
python nextquestion.py chunk ~/Books/Robbins.pdf --start-page 45 --end-page 120
```
### 2. `enrich` — генерация каталожных карточек
```bash
python nextquestion.py enrich [--limit 10]
```
**Что делает:**
- Берёт чанки без карточек (или с `src="fallback"`)
- Отправляет текст в LLM с промптом `enrich`
- Получает JSON: `{summary, keywords, questions}`
- Сохраняет в таблицу `chunk_enrich`
- Кэширует ответы (повторные вызовы бесплатны)
**Флаги:**
- `--limit N` — обработать только первые N чанков (для теста)
**Стоимость:** ~400 токенов на чанк (примерно $0.001 за чанк для GPT-4o-mini)
### 3. `link` — построение графа
```bash
python nextquestion.py link [--alias-db path/to/aliases.db]
```
**Что делает:**
- Создаёт заметки Obsidian в папке `Notes/`
- Создаёт хабы глав в `Sections/`
- Создаёт хабы сущностей в `Entities/`
- Генерирует wikilink между связанными чанками
- Сохраняет связи в таблицу `links` (0 вызовов LLM)
**Флаги:**
- `--alias-db` — путь к базе синонимов для слияния сущностей (опционально)
### 4. `reindex` — построение поискового индекса
```bash
python nextquestion.py reindex
```
**Что делает:**
- Создаёт индекс FTS5 для быстрого полнотекстового поиска
- Запускается автоматически при первом `query`
### 5. `query` — поиск и ответ
```bash
python nextquestion.py query "ваш вопрос"
```
**Что делает:**
- Запускает HyDE (генерацию гипотетических документов)
- Ищет релевантные чанки через FTS5
- Обходит граф связей для расширения контекста
- Генерирует ответ через LLM со стримингом
- Выводит кликабельные ссылки на заметки Obsidian
**Без аргументов** — запускает интерактивный REPL:
```bash
python nextquestion.py query
```
### 6. `init` — визард настройки
```bash
python nextquestion.py init
```
Запускает интерактивный TUI-визард с навигацией стрелками.
### 7. `example` — пример конфига
```bash
python nextquestion.py example
```
Печатает шаблон `config.json` с комментариями.
---
## Продвинутое использование
### Обработка нескольких книг
```bash
# Книга 1
python nextquestion.py chunk ~/Books/MadebyMisa.pdf
python nextquestion.py enrich
python nextquestion.py link
# Книга 2
python nextquestion.py chunk ~/Books/MadebyMisa.pdf
python nextquestion.py enrich
python nextquestion.py link
```
Сущности из разных книг автоматически сливаются в общие хабы (например, «IL-6» из Роббинса и Харрисона окажется в одном `ENTITY__IL-6.md`).
### Локализация интерфейса
Отредактируйте `config.json`, чтобы поменять подписи заметок и промпты на любой язык, например на английский:
```json
{
"labels": {
"brief": "Brief",
"answers": "Answers these questions",
"keywords": "Key terms",
"full": "Full source text",
"links": "Connections",
"chapter": "Chapter",
"prev": "Previous",
"next": "Next",
"topics": "Related topics"
}
}
```
Затем запустите `enrich` с новым `cache_namespace`, чтобы перегенерировать карточки:
```json
{
"cache_namespace": "nq-1.1-custom"
}
```
### Извлечение специфических сущностей
Добавьте regex-паттерны в `entity_patterns`:
```json
{
"entity_patterns": [
"\\b(?:IL-\\d+|TNF-?[A-Za-z]?|CD\\d+)\\b",
"\\b(?:HbA1c|CRP|NT-proBNP)\\b"
]
}
```
Эти сущности будут извлекаться автоматически и появятся в хабах `Entities/`.
### Переиндексация после изменений
Если вы вручную правили заметки или меняли промпты:
```bash
# Очистите кэш (удалите nextquestion_cache.db)
rm nextquestion_cache.db
# Переиндексируйте
python nextquestion.py reindex
# Переобогатите
python nextquestion.py enrich
```
ОШИБКИ:
## Справочник ошибок (что означает каждое сообщение и как его исправить)
NextQuestion никогда не падает молча: любая проблема печатается читаемым сообщением.
Эта таблица сопоставляет каждое сообщение с его причиной и способом лечения.
*Примечание: сообщения приведены так, как их печатает сборка; в русской сборке часть сообщений идёт по-русски, в английской — по-английски. Сверяйтесь по коду статуса HTTP и по смыслу.*
### Ошибки подключения и API (стадии enrich / query)
| Что увидите | Что это значит | Как исправить |
|---|---|---|
| `LLM failed: ... Connection refused` / `Max retries exceeded` | Эндпоинт недоступен: опечатка в `base_url` или локальный сервер не запущен | Проверьте URL; для Ollama выполните `ollama serve`; проверьте порт (`11434` / `8080`) |
| `LLM failed: HTTP 404: ...` | Неверный путь или неверное имя модели | OpenAI-совместимые URL обычно заканчиваются на `/v1`; проверьте имя модели (`ollama list`, документация провайдера) |
| `LLM failed: HTTP 401 / 403: ...` | API-ключ невалиден, просрочен или принадлежит другому провайдеру | Перевыпустите ключ; обновите `api_key` в `config.json` или `OPENAI_API_KEY` |
| `LLM failed: HTTP 429: ...` | Лимит запросов или нулевой баланс | Подождите минуту / пополните баланс; скрипт сам ретраит с бэкоффом, прежде чем сдаться |
| `LLM failed: HTTP 400: ...` | Провайдер отклонил параметр запроса | Скрипт сам отбрасывает `response_format` → `temperature` → `reasoning_effort` и повторяет; если всё равно падает — проверьте имя модели |
| `LLM failed: empty response` | Модель ничего не вернула (thinking-модели могут сжечь все токены на размышления) | Повторите запрос или смените модель через `--model` |
| `Query expansion parsing failed: ...` | Шаг HyDE вернул битый JSON | Просто задайте вопрос ещё раз; если повторяется — смените модель |
| `⚠️ Stream interrupted (...)` | Сеть оборвалась посреди ответа | Напечатана частичная версия ответа; задайте вопрос снова, чтобы получить полный |
| `❌ Model returned no tokens...` | Провайдер вернул пустой поток | Повторите запрос или `--model другая-модель` |
### Ошибки конвейера (стадии chunk / enrich / link)
| Что увидите | Что это значит | Как исправить |
|---|---|---|
| `Config file not found: config.json` | Конфига ещё нет | `python nextquestion.py init` (визард) или `example > config.json` |
| `'vault_path' is not set...` / `'api_key' is not set...` | Обязательное поле пустое | Заполните его в `config.json` или экспортируйте `RAG_VAULT` / `OPENAI_API_KEY` |
| `File not found: ...` | Неверный путь к источнику | Проверьте путь; пути с пробелами берите в кавычки |
| `Unsupported format: .xyz` | Принимаются только `.pdf`, `.md`, `.markdown`, `.txt` | Сначала сконвертируйте источник |
| `PDF is password-protected` | PDF зашифрован | Снимите защиту (Acrobat, qpdf) |
| `Could not split source: no text (possibly a scan without OCR)` | PDF — скан без текстового слоя | Сначала прогоните OCR или найдите текстовую версию |
| `Could not decode text file: ...` | Неизвестная кодировка текста | Пересохраните файл в UTF-8 |
| `❌ Run 'chunk' first.` | Вы вызвали `link`/`query` на пустой базе | Соблюдайте порядок: chunk → enrich → link |
| `⚠️ N excerpts missing cards. Run 'enrich' first.` | У части чанков нет карточек LLM | Запустите `enrich` (уже закэшированные чанки бесплатны) |
| `⚠️ FTS5 unavailable ... LIKE fallback` | Ваш SQLite собран без FTS5 | Всё работает, просто медленнее; обновите Python/SQLite для скорости |
| `❌ Nothing found. Try rephrasing...` | Поиск не вернул ни одного чанка | Убедитесь, что `enrich` + `link` + `reindex` выполнены; переформулируйте или добавьте термин из предметной области |
### Проблемы с Obsidian
| Что видите / наблюдаете | Что это значит | Как исправить |
|---|---|---|
| Заметки не появляются в Obsidian | Неверный `vault_path` / `store_root` | Проверьте оба; заметки лежат в `<vault>/<store_root>/Notes/...` |
| Ссылка `obsidian://` не открывается | Obsidian закрыт или активен другой vault | Откройте Obsidian с этим vault или используйте резервный путь, напечатанный рядом со ссылкой |
| Граф выглядит пустым | Стадия `link` не запускалась | Запустите `python nextquestion.py link` |
### Как читать префиксы
- `❌` — фатально для этой команды; ничего не записано, исправьте и запустите снова.
- `⚠️` — предупреждение; стадия продолжилась в урезанном режиме.
- `✅ / 💰 /  / 🕸️` — прогресс и статистика, всё хорошо.
