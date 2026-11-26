#!/usr/bin/env bash
set -uo pipefail

export PYTHONUTF8=1

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_ROOT"

COURSE_DIR="downloaded_course"
SORTED_DIR="sorted_course"
ARCHIVE_DIR="archives"
CDP_URL="http://127.0.0.1:9222"
ADAPTER="_template"

if [[ -x "$SCRIPT_ROOT/.venv/bin/python" ]]; then
  PYTHON_BIN="$SCRIPT_ROOT/.venv/bin/python"
else
  PYTHON_BIN="python3"
fi

get_cdp_port() {
  local port
  port="$(echo "$CDP_URL" | sed -nE 's|.*:([0-9]+).*|\1|p')"
  echo "${port:-9222}"
}

run_py() {
  echo ""
  echo "> $PYTHON_BIN $*"
  "$PYTHON_BIN" "$@"
  return $?
}

start_debug_browser() {
  local port
  port="$(get_cdp_port)"
  echo ""
  echo "1) Запустить Chrome/Chromium с --remote-debugging-port=$port"
  echo "2) Проверить / освободить порт $port"
  read -r -p "Выберите действие [1-2]: " sub

  if [[ "$sub" == "2" ]]; then
    if command -v fuser >/dev/null 2>&1; then
      fuser -k "${port}/tcp" 2>/dev/null || true
      echo "Порт $port освобождён."
    elif command -v lsof >/dev/null 2>&1; then
      lsof -ti "tcp:${port}" | xargs -r kill -9 || true
      echo "Порт $port освобождён."
    else
      echo "Утилиты fuser/lsof не найдены — проверьте процессы вручную (ss -ltnp | grep $port)."
    fi
    return
  fi

  local browser_bin=""
  for cand in google-chrome chromium chromium-browser yandex-browser; do
    if command -v "$cand" >/dev/null 2>&1; then
      browser_bin="$cand"
      break
    fi
  done

  if [[ -z "$browser_bin" ]]; then
    echo "Chromium-браузер не найден в PATH."
    return
  fi

  read -r -p "Стартовый URL курса (можно пустым): " start_url
  local profile_dir="/tmp/course-scraper-chrome-debug"
  nohup "$browser_bin" \
    --remote-debugging-port="$port" \
    --remote-debugging-address=127.0.0.1 \
    --user-data-dir="$profile_dir" \
    --disable-dns-prefetching \
    --no-first-run \
    ${start_url:+"$start_url"} >/dev/null 2>&1 &
  echo "Запущен $browser_bin (порт $port, профиль $profile_dir)."
}

run_full_pipeline() {
  run_py scripts/download_course.py --adapter "$ADAPTER" --dir "$COURSE_DIR" --cdp "$CDP_URL"
  local rc=$?
  if [[ $rc -eq 1 ]]; then
    echo "Критический сбой на этапе скачивания (код 1). Пайплайн остановлен."
    return
  fi

  run_py scripts/verify_course.py --dir "$COURSE_DIR" --offline
  rc=$?
  if [[ $rc -eq 1 ]]; then
    echo "Критический сбой на этапе проверки (код 1). Пайплайн остановлен."
    return
  fi

  run_py scripts/convert_to_mp4.py --dir "$COURSE_DIR"
  rc=$?
  echo "Полный цикл завершён (итоговый код: $rc)."
}

while true; do
  echo ""
  echo "============================================================"
  echo " Course Materials Scraper — Оркестратор Linux / WSL"
  echo "------------------------------------------------------------"
  echo " Адаптер: $ADAPTER | Папка: $COURSE_DIR | CDP: $CDP_URL"
  echo "============================================================"
  echo "1. Запустить отладочный браузер (или освободить порт CDP)"
  echo "2. Скачать / докачать курс (download_course.py)"
  echo "3. Проверить целостность и очистить битые файлы (verify_course.py)"
  echo "4. Конвертировать видео в .mp4 (convert_to_mp4.py)"
  echo "5. Разделить материалы (split_materials.py)"
  echo "6. Упаковать папки в .zip (zip_folders.py)"
  echo "7. Полный цикл (Скачать -> Проверить -> Конвертировать)"
  echo "8. Изменить настройки (адаптер / --dir / --cdp)"
  echo "9. Очистить HAR/HTML-дамп (sanitize_capture.py)"
  echo "0. Выход"

  read -r -p "Выберите пункт: " op
  case "$op" in
    1) start_debug_browser ;;
    2) run_py scripts/download_course.py --adapter "$ADAPTER" --dir "$COURSE_DIR" --cdp "$CDP_URL" || true ;;
    3)
      read -r -p "Удалять битые файлы и .part/.tmp (--clean)? [y/N]: " clean_ans
      if [[ "$clean_ans" =~ ^[yYдД]$ ]]; then
        run_py scripts/verify_course.py --dir "$COURSE_DIR" --offline --clean || true
      else
        run_py scripts/verify_course.py --dir "$COURSE_DIR" --offline || true
      fi
      ;;
    4) run_py scripts/convert_to_mp4.py --dir "$COURSE_DIR" || true ;;
    5) run_py scripts/split_materials.py --dir "$COURSE_DIR" --out "$SORTED_DIR" || true ;;
    6) run_py scripts/zip_folders.py --subdirs-of "$COURSE_DIR" --out "$ARCHIVE_DIR" || true ;;
    7) run_full_pipeline ;;
    8)
      echo "Адаптеры в adapters/: $(find adapters -maxdepth 1 -name '*.py' ! -name '__init__.py' ! -name 'base.py' -exec basename {} .py \; | tr '\n' ' ')"
      read -r -p "Имя адаптера [$ADAPTER]: " new_adapter
      [[ -n "$new_adapter" ]] && ADAPTER="$new_adapter"
      read -r -p "Рабочая папка --dir [$COURSE_DIR]: " new_dir
      [[ -n "$new_dir" ]] && COURSE_DIR="$new_dir"
      read -r -p "Адрес CDP --cdp [$CDP_URL]: " new_cdp
      [[ -n "$new_cdp" ]] && CDP_URL="$new_cdp"
      ;;
    9)
      read -r -p "Путь к исходному .har/.html файлу: " in_file
      read -r -p "Путь для сохранения очищенного файла: " out_file
      if [[ -n "$in_file" && -n "$out_file" ]]; then
        run_py scripts/sanitize_capture.py --input "$in_file" --output "$out_file" || true
      fi
      ;;
    0) break ;;
    *) echo "Неизвестный пункт меню." ;;
  esac
done
