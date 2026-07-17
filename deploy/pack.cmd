@echo off
REM ========== Assets Management - Pack Script ==========
REM Usage: deploy\pack.cmd
REM Output: assetsmangment.zip (project root)
REM =====================================================
setlocal enabledelayedexpansion

set "PROJECT_DIR=%~dp0.."
set "DEST=%PROJECT_DIR%\assetsmangment.zip"
set "TMP=%PROJECT_DIR%\.tmp_zip"

echo [1/5] Cleaning old files...
if exist "%DEST%" del /q "%DEST%"
if exist "%TMP%" rmdir /s /q "%TMP%"

echo [2/5] Copying source files...
mkdir "%TMP%"
copy "%PROJECT_DIR%\Dockerfile" "%TMP%\" >nul
xcopy "%PROJECT_DIR%\app" "%TMP%\app\" /e /i /q >nul
xcopy "%PROJECT_DIR%\frontend" "%TMP%\frontend\" /e /i /q >nul

echo [3/5] Removing __pycache__...
for /d /r "%TMP%" %%d in (__pycache__) do if exist "%%d" rmdir /s /q "%%d" 2>nul

echo [4/5] Creating zip with forward-slash paths...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-Type -AssemblyName System.IO.Compression.FileSystem; $dest='%DEST%'; $tmp='%TMP%'; $archive=[System.IO.Compression.ZipFile]::Open($dest,'Create'); $base=(Resolve-Path $tmp).Path.TrimEnd('\'); Get-ChildItem -Path $tmp -Recurse -File | ForEach-Object { $n=$_.FullName.Substring($base.Length+1) -replace '\\','/'; [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive,$_.FullName,$n) }; $archive.Dispose(); Write-Host ('    OK: '+[math]::Round((Get-Item $dest).Length/1KB,1)+' KB')"

if %ERRORLEVEL% NEQ 0 (
    echo FAILED: zip creation error
    pause
    exit /b 1
)

echo [5/5] Cleaning temp files...
rmdir /s /q "%TMP%"

echo DONE: %DEST%
pause
