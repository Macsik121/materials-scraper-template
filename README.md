# Course Materials Scraper — Template & Framework

Универсальный шаблон модульного фреймворка на **Python + Playwright** для локальной, отказоустойчивой и безопасной выгрузки учебных материалов (PDF, видео, составные HTML-лекции, задания, разборы тестов) с любых онлайн-курсов и LMS.

> **Текущий статус проекта:** `Framework Ready (Template)`
> Реализованы все модули ядра (`core/`), слой адаптеров (`adapters/`), автономные CLI-утилиты (`scripts/`), санитайзер дампов и оркестраторы ОС (`scrape-course.ps1` в UTF-8 BOM / `scrape-course.sh`). Для подключения новой учебной площадки достаточно создать один файл адаптера по образцу `adapters/_template.py`.

---

## Ключевые архитектурные принципы

* **Zero-Credentials CDP:** логины, пароли и токены нигде не хранятся. Скрипты подключаются к живой авторизованной сессии системного Chromium-браузера (Chrome, Яндекс Браузер, Edge) через `connect_over_cdp`.
* **Разделение ядра и адаптеров:** сайт-специфичная логика (`adapters/<platform>.py`) отвечает только за разбор DOM/сети и возврат нормализованных моделей, а загрузка, валидация и сохранение живут в `core/`.
* **Гибридные отчёты для небинарных страниц:** задания, текстовые лекции и разборы тестов сохраняются как автономные папки (`Markdown`/`JSON` для поиска + `visual_report.png` в светлой печатной теме с откатом CSS).
* **Strict Read-Only на тестах и заданиях:** скрипты никогда не нажимают деструктивные кнопки («Начать тест», «Сдать работу», «Сбросить попытку»).
* **Идемпотентность и проверка целостности:** атомарная запись (`.tmp`/`.part` → целевое имя), проверка бинарных сигнатур (magic bytes) и трекинг состояния (`manifest.json` + `state.json`).

---

## Структура репозитория

```text
course-scraper/
├── .github/
│   └── workflows/
│       └── tests.yml                 # CI: unittest, py_compile, UTF-8 BOM и синтаксис .sh
├── .gitignore                        # Исключает дампы, *.har, downloaded_course/, state.json, .venv/
├── LICENSE                           # Лицензия MIT
├── requirements.txt                  # Базовая зависимость (playwright)
├── requirements-reports.txt          # Опциональные зависимости для pack_to_pdf.py (pillow, img2pdf)
├── README.md                         # Инструкция запуска, создания адаптера и пайплайна
├── SPEC.md                           # Полная техническая спецификация и инварианты
├── AGENT_PROMPT.md                   # Системный промпт ИИ-агента (для CLAUDE.md / Cursor Rules)
├── web-page-reverse-engineering/
│   └── .gitkeep                      # Папка для очищенных HTML/HAR-срезов (содержимое в .gitignore)
├── core/                             # 100% переиспользуемое ядро фреймворка
│   ├── __init__.py
│   ├── models.py                     # Course, Module, Item, ItemType, ItemStatus, ItemStateRecord
│   ├── fs_utils.py                   # Windows MAX_PATH=260, запрещённые имена, zero-padding, атомарная запись
│   ├── state.py                      # ManifestStore (manifest.json) и StateStore (state.json)
│   ├── browser.py                    # CDP-префлайт /json/version, поиск фреймов, Session Circuit Breaker
│   ├── downloader.py                 # Последовательный CDP-загрузчик, Popup Await, UTF-8/CP1251, magic bytes
│   └── handlers/
│       ├── __init__.py
│       ├── base_report.py            # Print CSS + MD/JSON + Full-Page PNG с автонарезкой >15 000 px
│       ├── binary_file.py            # PDF, Office, архивы (+ ?file= PDF.js)
│       ├── video_stream.py           # Прямое видео и HLS/DASH с предварительным DRM-детектором
│       ├── composite_html.py         # Составные HTML-страницы (Markdown + assets/ + видео + отчёт)
│       ├── assignment.py             # Практические задания + вложения
│       └── test_readonly.py          # Разбор завершённых тестов (строго read-only)
├── adapters/                         # Сайт-специфичные адаптеры LMS
│   ├── __init__.py                   # Реестр и динамический загрузчик load_adapter()
│   ├── base.py                       # Абстрактный BaseAdapter + scroll_to_bottom()
│   └── _template.py                  # Типизированная болванка для новой площадки
├── scripts/                          # Автономные CLI-утилиты (коды возврата 0 / 1 / 2)
│   ├── download_course.py            # Основная точка входа (--adapter, --dir, --cdp)
│   ├── verify_course.py              # Онлайн/оффлайн аудит magic bytes + --clean
│   ├── convert_to_mp4.py             # Пакетная конвертация видео в универсальный .mp4 (FFmpeg)
│   ├── split_materials.py            # Read-only разделение документов, видео и отчётов (stdlib)
│   ├── zip_folders.py                # Автономный Zip64-архиватор любых папок (stdlib)
│   ├── sanitize_capture.py           # Очиститель HAR/HTML от cookies, токенов и ПДн (stdlib)
│   └── pack_to_pdf.py                # Офлайн-конвертер visual_report.png в A4 PDF
├── scrape-course.ps1                 # Интерактивное меню для Windows (строго UTF-8 with BOM)
├── scrape-course.sh                  # Интерактивное меню для Linux / WSL
└── tests/
    └── test_framework.py             # Юнит-тесты ядра и CLI-утилит (python3 -m unittest)
```

---

## Быстрый старт на новом курсе

### Шаг 1. Подготовь окружение Python
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Опционально, только для scripts/pack_to_pdf.py:
pip install -r requirements-reports.txt
```
> Команду `playwright install` выполнять **не нужно** — используется уже установленный в системе браузер.

### Шаг 2. Запусти системный браузер в режиме отладки (CDP)
Используй **изолированный профиль строго на латинице** (кириллица в пути ломает `--user-data-dir` в Chromium/Яндекс Браузере) и явную привязку к `127.0.0.1`:

```powershell
# Google Chrome
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="C:\Users\Public\chrome-debug" --disable-dns-prefetching --no-first-run

# Яндекс Браузер
& "C:\Program Files\Yandex\YandexBrowser\Application\browser.exe" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="C:\Users\Public\yandex-debug" --disable-dns-prefetching --no-first-run
```

### Шаг 3. Собери очищенные срезы и создай адаптер
1. Авторизуйся на площадке курса вручную в отладочном окне браузера.
2. Сохрани HTML-срезы и HAR-дампы ключевых страниц в `web-page-reverse-engineering/`.
3. Очисти дампы перед анализом или передачей в ИИ-агента:
   ```powershell
   python scripts/sanitize_capture.py --input web-page-reverse-engineering/raw.har --output web-page-reverse-engineering/clean.har
   ```
4. Скопируй `adapters/_template.py` в `adapters/<platform>.py` и реализуй парсинг оглавления и контента по контракту `BaseAdapter`.

### Шаг 4. Прогони пайплайн выгрузки
Через интерактивное меню ОС:
```powershell
# Windows (PowerShell 5.1 / 7+)
powershell -ExecutionPolicy Bypass -File .\scrape-course.ps1

# Linux / WSL
./scrape-course.sh
```
Или напрямую через CLI:
```powershell
python scripts/download_course.py --adapter <platform> --dir downloaded_course --cdp http://127.0.0.1:9222
python scripts/verify_course.py --dir downloaded_course --offline --clean
python scripts/convert_to_mp4.py --dir downloaded_course
python scripts/split_materials.py --dir downloaded_course --out sorted_course
python scripts/zip_folders.py --subdirs-of downloaded_course --out archives
python scripts/pack_to_pdf.py --dir downloaded_course --page-size a4
```

---

## Дорожная карта реализации (выполнено 16 / 16)

### 1. Инфраструктура и гигиена репозитория
- [x] `.gitignore` — исключение дампов, `.har`, скачанных материалов, `state.json`, `.venv/`, `__pycache__/`.
- [x] `requirements.txt` / `requirements-reports.txt` — разделение базовых (`playwright`) и опциональных (`pillow`, `img2pdf`) зависимостей.
- [x] `web-page-reverse-engineering/.gitkeep` — фиксация пустой папки для HTML/HAR-срезов.

### 2. Ядро фреймворка (`core/`)
- [x] `core/__init__.py` и `core/handlers/__init__.py` — оформление Python-пакетов.
- [x] `core/models.py` — строгие модели данных (`Course`, `Module`, `Item`, `ItemType`, `ItemStatus`).
- [x] `core/fs_utils.py` — санитизация имён под Windows (`MAX_PATH = 260`, запрещённые имена), zero-padding (`M01_`, `01_`), атомарная запись.
- [x] `core/state.py` — атомарная работа с `manifest.json` и `state.json`.
- [x] `core/browser.py` — подключение к CDP, префлайт `/json/version` и `/json/list`, поиск фреймов, Session Expiration Circuit Breaker.
- [x] `core/downloader.py` — последовательный CDP-загрузчик (`Browser.setDownloadBehavior`), Popup Await, декодер редиректов (`utf-8`/`cp1251`), WAF-jitter и валидация magic bytes.
- [x] `core/handlers/base_report.py` — движок гибридных отчётов (Print CSS + MD/JSON + Full-Page PNG).
- [x] `core/handlers/binary_file.py` — обработчик прямых файлов (PDF, Office, архивы).
- [x] `core/handlers/video_stream.py` — обработчик прямых видео и потоков HLS/DASH с предварительным DRM-детектором.
- [x] `core/handlers/composite_html.py` — обработчик составных HTML-страниц.
- [x] `core/handlers/assignment.py` и `core/handlers/test_readonly.py` — обработчики заданий и завершённых тестов (строго read-only).

### 3. Слой адаптеров (`adapters/`)
- [x] `adapters/__init__.py` — реестр и фабрика загрузки адаптера по имени (`--adapter`).
- [x] `adapters/base.py` — абстрактный `BaseAdapter` и утилита `scroll_to_bottom()`.
- [x] `adapters/_template.py` — болванка адаптера с типизацией и комментариями.

### 4. Универсальные CLI-скрипты (`scripts/`) и оркестратор ОС
- [x] `scripts/download_course.py`, `verify_course.py`, `convert_to_mp4.py`, `split_materials.py`, `zip_folders.py`, `sanitize_capture.py`, `pack_to_pdf.py`.
- [x] `scrape-course.ps1` (**UTF-8 with BOM**) и `scrape-course.sh`.

---

## Быстрая памятка по диагностике среды (Windows)

* **Конфликт порта `9222` (фоновый Chrome/Яндекс держит порт после закрытия окон):**
  ```powershell
  Get-NetTCPConnection -LocalPort 9222 -State Listen -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty OwningProcess -Unique |
      ForEach-Object { Stop-Process -Id $_ -Force }
  Stop-Process -Name chrome, browser -Force -ErrorAction SilentlyContinue
  ```
* **Кодировка `.ps1` в Windows PowerShell 5.1:** все PowerShell-скрипты с кириллицей сохраняются строго в **UTF-8 with BOM**, иначе парсер прочитает их как Windows-1251.

---

## Правовая оговорка (Disclaimer)

Инструмент предназначен исключительно для **личного офлайн-архивирования учебных материалов**, к которым пользователь имеет законный доступ в рамках собственной учётной записи. Фреймворк сознательно **не обходит DRM**, не взламывает механизмы авторизации и не предназначен для несанкционированного распространения контента. Ответственность за соблюдение условий использования (Terms of Service) конкретной образовательной платформы лежит на пользователе.

## Лицензия

Проект распространяется по лицензии **MIT** — подробности в файле [`LICENSE`](LICENSE).
