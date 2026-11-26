# Course Materials Scraper — Template & Framework

Универсальный шаблон модульного фреймворка на **Python + Playwright** для локальной, отказоустойчивой и безопасной выгрузки учебных материалов (PDF, видео, составные HTML-лекции, задания, разборы тестов) с любых онлайн-курсов и LMS.

> **Текущий статус проекта:** `Foundation / Blueprint`
> В репозитории зафиксированы архитектурная спецификация, системный промпт для ИИ-агента и базовые модули подсистемы гибридных отчётов. Остальные компоненты ядра, адаптеры под конкретные площадки и CLI-утилиты разворачиваются по дорожной карте ниже при старте работы с новым курсом.

---

## Ключевые архитектурные принципы

* **Zero-Credentials CDP:** логины, пароли и токены нигде не хранятся. Скрипты подключаются к живой авторизованной сессии системного Chromium-браузера (Chrome, Яндекс Браузер, Edge) через `connect_over_cdp`.
* **Разделение ядра и адаптеров:** сайт-специфичная логика (`adapters/<platform>.py`) отвечает только за разбор DOM/сети и возврат нормализованных моделей, а загрузка, валидация и сохранение живут в `core/`.
* **Гибридные отчёты для небинарных страниц:** задания, текстовые лекции и разборы тестов сохраняются как автономные папки (`Markdown`/`JSON` для поиска + `visual_report.png` в светлой печатной теме с откатом CSS).
* **Strict Read-Only на тестах и заданиях:** скрипты никогда не нажимают деструктивные кнопки («Начать тест», «Сдать работу», «Сбросить попытку»).
* **Идемпотентность и проверка целостности:** атомарная запись (`.tmp`/`.part` → целевое имя), проверка бинарных сигнатур (magic bytes) и трекинг состояния.

---

## Текущее состояние репозитория (готовые файлы)

```text
course-scraper-template/
├── README.md                        # Этот файл — обзор состояния и дорожная карта
├── SPEC.md                          # Полная техническая спецификация и инварианты архитектуры
├── AGENT_PROMPT.md                  # Системный промпт агента (можно использовать как CLAUDE.md / Cursor Rules)
├── core/
│   └── handlers/
│       └── base_report.py           # Движок гибридных отчётов (Print CSS + MD/JSON + Full-Page PNG)
└── scripts/
    └── pack_to_pdf.py               # Офлайн-конвертер длинных visual_report.png в многостраничный PDF (A4)
```

### Что уже реализовано
1. **`SPEC.md`** — единый источник правды: структура модулей, контракты CLI-флагов и кодов возврата (`0`/`1`/`2`), матрица обработки форматов контента, правила CDP и реестр учтённых ошибок (Postmortem).
2. **`AGENT_PROMPT.md`** — готовые системные инструкции, задающие кодинг-агенту пошаговый процесс (разведка → проектирование → реализация) и все инженерные инварианты проекта.
3. **`core/handlers/base_report.py`** — синхронный Playwright-модуль с контекстным менеджером `clean_print_view()` (инъекция светлого Print CSS, раскрытие скроллбоксов и `100vh`-контейнеров, поддержка `iframe`, гарантированный откат DOM) и `capture_visual_report()` (автонарезка сверхдлинных страниц `>15 000 px`, проверка PNG-сигнатуры).
4. **`scripts/pack_to_pdf.py`** — автономный скрипт постобработки на `Pillow` + `img2pdf` для умной нарезки длинных `visual_report.png` на страницы A4 (с поиском светлых строк на границах разрыва) и проверкой `%PDF-`/`%%EOF`.

---

## Как работать с этим шаблоном на новом курсе

### Шаг 1. Настрой гигиену репозитория (`.gitignore`)
Перед началом реверс-инжиниринга создай `.gitignore` в корне, чтобы дампы с сессией, куками и ПДн гарантированно не попали в Git:

```gitignore
web-page-reverse-engineering/
*.har
downloaded_course/
sorted_course/
archives/
state.json
.venv/
__pycache__/
*.pyc
```

### Шаг 2. Запусти системный браузер в режиме отладки (CDP)
Используй **изолированный профиль строго на латинице** (кириллица в пути ломает `--user-data-dir` в Chromium/Яндекс Браузере) и явную привязку к `127.0.0.1`:

```powershell
# Google Chrome
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="C:\Users\Public\chrome-debug" --disable-dns-prefetching --no-first-run

# Яндекс Браузер
& "C:\Program Files\Yandex\YandexBrowser\Application\browser.exe" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --user-data-dir="C:\Users\Public\yandex-debug" --no-first-run
```

> Команду `playwright install` выполнять **не нужно** — достаточно `pip install playwright`, используется системный браузер.

### Шаг 3. Собери срезы страниц и подключи ИИ-агента
1. Авторизуйся на площадке курса вручную в отладочном окне браузера.
2. Сохрани HTML-срезы ключевых состояний (оглавление курса, страница урока, видео, задание, разбор теста) в `web-page-reverse-engineering/` и зафиксируй сетевую активность в DevTools (`Network` → `Preserve log`).
3. **Обязательно очисти дампы** от `Cookie`, `Authorization`, токенов и персональных данных перед передачей в LLM.
4. Передай агенту `AGENT_PROMPT.md` (как системные инструкции), `SPEC.md` и очищенные срезы — агент развернёт недостающие модули ядра и напишет адаптер `adapters/<platform>.py` под целевую LMS.

---

## Запуск готовой утилиты `scripts/pack_to_pdf.py`

Зависимости изолированы от остального проекта (`Pillow` и `img2pdf` нужны только для сборки PDF из скриншотов):

```powershell
pip install pillow img2pdf

# Нарезать все visual_report.png на многостраничные PDF формата A4
python scripts/pack_to_pdf.py --dir downloaded_course --page-size a4

# Собрать в один бесшовный лист без разрывов и удалить исходные PNG
python scripts/pack_to_pdf.py --dir downloaded_course --page-size single --delete-png
```

---

## Дорожная карта (модули к реализации / переносу)

Ниже — полный список компонентов, которые предусмотрены `SPEC.md` и будут добавляться по мере развёртывания под конкретные курсы.

### 1. Инфраструктура и гигиена репозитория
- [ ] `.gitignore` — исключение дампов, `.har`, скачанных материалов, `state.json`, `.venv/`, `__pycache__/`.
- [ ] `requirements.txt` / `requirements-reports.txt` — разделение базовых (`playwright`) и опциональных (`pillow`, `img2pdf`) зависимостей.
- [ ] `web-page-reverse-engineering/.gitkeep` — фиксация пустой папки для HTML/HAR-срезов.

### 2. Ядро фреймворка (`core/`) — переиспользуемо на всех LMS
- [ ] `core/__init__.py` и `core/handlers/__init__.py` — оформление Python-пакетов.
- [ ] `core/models.py` — строгие модели данных (`Course`, `Module`, `Item`, `ItemType`, `ItemStatus`).
- [ ] `core/fs_utils.py` — санитизация имён под Windows (`MAX_PATH = 260`, запрещённые имена), zero-padding (`M01_`, `01_`), атомарная запись.
- [ ] `core/state.py` — атомарная работа с `manifest.json` и `state.json` (`PENDING`, `DONE`, `FAILED`, `DRM_LOCKED`, `EXTERNAL_LINK`, `SKIPPED`).
- [ ] `core/browser.py` — подключение к CDP, префлайт `/json/version` и `/json/list`, поиск фреймов, Session Expiration Circuit Breaker.
- [ ] `core/downloader.py` — строго последовательный CDP-загрузчик (`Browser.setDownloadBehavior`), Popup Await, декодер редиректов (`utf-8`/`cp1251`), WAF-jitter и валидация magic bytes.
- [ ] `core/handlers/binary_file.py` — обработчик прямых файлов (PDF, Office, архивы).
- [ ] `core/handlers/video_stream.py` — обработчик прямых видео и потоков HLS/DASH с предварительным детектором DRM (`SAMPLE-AES`, Widevine/FairPlay, `<ContentProtection>`).
- [ ] `core/handlers/composite_html.py` — обработчик составных HTML-страниц (текст + локальные картинки + встроенное видео + `base_report.py`).
- [ ] `core/handlers/assignment.py` — обработчик заданий поверх `base_report.py` + загрузка вложений.
- [ ] `core/handlers/test_readonly.py` — обработчик завершённых тестов (строго read-only) поверх `base_report.py`.

### 3. Слой адаптеров (`adapters/`) — точка расширения под конкретный сайт
- [ ] `adapters/__init__.py` — реестр и фабрика загрузки адаптера по имени (`--adapter`).
- [ ] `adapters/base.py` — абстрактный `BaseAdapter` и утилита `scroll_to_bottom()` для SPA с бесконечным скроллом.
- [ ] `adapters/_template.py` — болванка адаптера с типизацией и комментариями для быстрого старта.

### 4. Универсальные CLI-скрипты (`scripts/`)
- [ ] `scripts/download_course.py` — основная точка входа (`--adapter`, `--dir`, `--cdp`, `--delay`, `--force-recheck`).
- [ ] `scripts/verify_course.py` — онлайн/оффлайн аудит magic bytes (PDF, WebM, MP4, PNG, Office) + `--clean`.
- [ ] `scripts/convert_to_mp4.py` — пакетная конвертация `.webm`/`.mkv` → `.mp4` через FFmpeg (`libx264`, `yuv420p`, `aac 128k`, `+faststart`, чётные размеры кадра).
- [ ] `scripts/split_materials.py` — read-only сортировка документов, видео и отчётов (`--docs-only`, `--flat`, без сторонних зависимостей).
- [ ] `scripts/zip_folders.py` — автономный Zip64-архиватор любых папок с умным сжатием (только стандартная библиотека Python).
- [ ] `scripts/sanitize_capture.py` — локальный очиститель `.har` и `.html` от `Cookie`, `Authorization`, токенов и ПДн.

### 5. Оркестратор ОС
- [ ] `scrape-course.ps1` — интерактивное меню для Windows (**обязательно в UTF-8 with BOM** для Windows PowerShell 5.1, запуск через `-ExecutionPolicy Bypass`).
- [ ] `scrape-course.sh` *(опционально)* — аналогичное меню для Linux / WSL.

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
