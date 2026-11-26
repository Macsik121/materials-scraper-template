#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Интерактивный оркестратор Course Materials Scraper для Windows (UTF-8 with BOM).
.DESCRIPTION
    Запуск в Windows PowerShell 5.1:
      powershell -ExecutionPolicy Bypass -File .\scrape-course.ps1
#>

[Console]::InputEncoding  = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding           = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8           = "1"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptRoot

$CourseDir  = "downloaded_course"
$SortedDir  = "sorted_course"
$ArchiveDir = "archives"
$CdpUrl     = "http://127.0.0.1:9222"
$Adapter    = "_template"
$PythonExe  = if (Test-Path "$ScriptRoot\.venv\Scripts\python.exe") { "$ScriptRoot\.venv\Scripts\python.exe" } else { "python" }

function Get-CdpPort {
    try { return ([uri]$CdpUrl).Port } catch { return 9222 }
}

function Invoke-PyScript {
    param(
        [Parameter(Mandatory)][string]$ScriptPath,
        [string[]]$Arguments = @()
    )
    Write-Host "`n> $PythonExe $ScriptPath $($Arguments -join ' ')" -ForegroundColor Cyan
    & $PythonExe $ScriptPath @Arguments
    return $LASTEXITCODE
}

function Start-DebugBrowser {
    $port = Get-CdpPort
    Write-Host "`n1) Google Chrome"
    Write-Host "2) Яндекс Браузер"
    Write-Host "3) Проверить / освободить порт $port"
    $choice = Read-Host "Выберите действие [1-3]"

    if ($choice -eq "3") {
        Write-Host "Освобождаем порт $port и завершаем зависшие процессы Chrome/Яндекс..." -ForegroundColor Yellow
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
        Stop-Process -Name chrome, browser -Force -ErrorAction SilentlyContinue
        Write-Host "Готово. Порт $port освобождён." -ForegroundColor Green
        return
    }

    $startUrl = Read-Host "Стартовый URL курса (можно оставить пустым)"
    if ($choice -eq "2") {
        $exe = "$env:LOCALAPPDATA\Yandex\YandexBrowser\Application\browser.exe"
        if (-not (Test-Path $exe)) { $exe = "C:\Program Files\Yandex\YandexBrowser\Application\browser.exe" }
        $profileDir = "C:\Users\Public\yandex-debug"
    } else {
        $exe = "C:\Program Files\Google\Chrome\Application\chrome.exe"
        if (-not (Test-Path $exe)) { $exe = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe" }
        $profileDir = "C:\Users\Public\chrome-debug"
    }

    if (-not (Test-Path $exe)) {
        Write-Host "Браузер не найден по пути: $exe" -ForegroundColor Red
        return
    }

    $browserArgs = @(
        "--remote-debugging-port=$port",
        "--remote-debugging-address=127.0.0.1",
        "--user-data-dir=$profileDir",
        "--disable-dns-prefetching",
        "--no-first-run"
    )
    if ($startUrl) { $browserArgs += $startUrl }

    Write-Host "Запуск: $exe $($browserArgs -join ' ')" -ForegroundColor Green
    Start-Process -FilePath $exe -ArgumentList $browserArgs
}

function Select-Adapter {
    $files = Get-ChildItem -Path "$ScriptRoot\adapters" -Filter "*.py" -ErrorAction SilentlyContinue |
        Where-Object { $_.BaseName -notin @("__init__", "base") } |
        Select-Object -ExpandProperty BaseName
    Write-Host "`nДоступные адаптеры в adapters/: $($files -join ', ')"
    $inputName = Read-Host "Имя адаптера [$Adapter]"
    if ($inputName) { $script:Adapter = $inputName.Trim() }
}

function Run-FullPipeline {
    $rc = Invoke-PyScript "scripts\download_course.py" @("--adapter", $Adapter, "--dir", $CourseDir, "--cdp", $CdpUrl)
    if ($rc -eq 1) {
        Write-Host "Критический сбой на этапе скачивания (код 1). Пайплайн остановлен." -ForegroundColor Red
        return
    }
    $rc = Invoke-PyScript "scripts\verify_course.py" @("--dir", $CourseDir, "--offline")
    if ($rc -eq 1) {
        Write-Host "Критический сбой на этапе проверки (код 1). Пайплайн остановлен." -ForegroundColor Red
        return
    }
    $rc = Invoke-PyScript "scripts\convert_to_mp4.py" @("--dir", $CourseDir)
    if ($rc -eq 1) {
        Write-Host "Критический сбой на этапе конвертации (код 1)." -ForegroundColor Red
        return
    }
    Write-Host "Полный цикл завершён (итоговый код: $rc)." -ForegroundColor Green
}

while ($true) {
    Write-Host "`n============================================================"
    Write-Host " Course Materials Scraper — Оркестратор Windows (PowerShell)"
    Write-Host "------------------------------------------------------------"
    Write-Host " Адаптер: $Adapter | Папка: $CourseDir | CDP: $CdpUrl"
    Write-Host "============================================================"
    Write-Host "1. Запустить отладочный браузер (или освободить порт CDP)"
    Write-Host "2. Скачать / докачать курс (download_course.py)"
    Write-Host "3. Проверить целостность и очистить битые файлы (verify_course.py)"
    Write-Host "4. Конвертировать видео в .mp4 (convert_to_mp4.py)"
    Write-Host "5. Разделить материалы (split_materials.py)"
    Write-Host "6. Упаковать папки в .zip (zip_folders.py)"
    Write-Host "7. Полный цикл (Скачать -> Проверить -> Конвертировать)"
    Write-Host "8. Изменить настройки (адаптер / --dir / --cdp)"
    Write-Host "9. Очистить HAR/HTML-дамп (sanitize_capture.py)"
    Write-Host "0. Выход"

    $op = Read-Host "`nВыберите пункт"
    switch ($op) {
        "1" { Start-DebugBrowser }
        "2" { [void](Invoke-PyScript "scripts\download_course.py" @("--adapter", $Adapter, "--dir", $CourseDir, "--cdp", $CdpUrl)) }
        "3" {
            $cleanFlag = Read-Host "Удалять битые файлы и .part/.tmp (--clean)? [y/N]"
            $vArgs = @("--dir", $CourseDir, "--offline")
            if ($cleanFlag -match "^[yYдД]") { $vArgs += "--clean" }
            [void](Invoke-PyScript "scripts\verify_course.py" $vArgs)
        }
        "4" { [void](Invoke-PyScript "scripts\convert_to_mp4.py" @("--dir", $CourseDir)) }
        "5" { [void](Invoke-PyScript "scripts\split_materials.py" @("--dir", $CourseDir, "--out", $SortedDir)) }
        "6" { [void](Invoke-PyScript "scripts\zip_folders.py" @("--subdirs-of", $CourseDir, "--out", $ArchiveDir)) }
        "7" { Run-FullPipeline }
        "8" {
            Select-Adapter
            $newDir = Read-Host "Рабочая папка --dir [$CourseDir]"
            if ($newDir) { $CourseDir = $newDir.Trim() }
            $newCdp = Read-Host "Адрес CDP --cdp [$CdpUrl]"
            if ($newCdp) { $CdpUrl = $newCdp.Trim() }
        }
        "9" {
            $inFile  = Read-Host "Путь к исходному .har/.html файлу"
            $outFile = Read-Host "Путь для сохранения очищенного файла"
            if ($inFile -and $outFile) {
                [void](Invoke-PyScript "scripts\sanitize_capture.py" @("--input", $inFile, "--output", $outFile))
            }
        }
        "0" { break }
        default { Write-Host "Неизвестный пункт меню." -ForegroundColor Yellow }
    }
}
