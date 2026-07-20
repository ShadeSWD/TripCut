# Сборка TripCut на Windows-VM (запускать в C:\claude\TripCut)
# Требует: код проекта + папка bin\ (ffmpeg.exe, ffprobe.exe, libmpv-2.dll) уже на месте.
$ErrorActionPreference = "Stop"
Set-Location C:\claude\TripCut

# --- python
$py = $null
foreach ($cand in @("C:\claude\tools\python311\python.exe", "python", "py")) {
    try { & $cand --version *> $null; if ($LASTEXITCODE -eq 0) { $py = $cand; break } } catch {}
}
if (-not $py) {
    Write-Output "Скачиваю Python 3.11..."
    $url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    Invoke-WebRequest $url -OutFile "$env:TEMP\py311.exe"
    Start-Process "$env:TEMP\py311.exe" -Wait -ArgumentList `
        "/quiet InstallAllUsers=0 TargetDir=C:\claude\tools\python311 PrependPath=0 Include_test=0 Include_doc=0"
    $py = "C:\claude\tools\python311\python.exe"
}
Write-Output "python: $py"

# --- venv + зависимости
if (-not (Test-Path .\venv)) { & $py -m venv venv }
.\venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.\venv\Scripts\python.exe -m pip install --quiet -r requirements.txt pyinstaller
Write-Output "deps ok"

# --- сборка (onedir, без консоли)
if (Test-Path .\dist) { Remove-Item .\dist -Recurse -Force }
.\venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --name TripCut `
    --icon tripcut.ico --add-binary "bin\*;bin" --add-data "tripcut.ico;." main.py
if (-not (Test-Path .\dist\TripCut\TripCut.exe)) { throw "PyInstaller: exe не появился" }
Write-Output ("BUILD OK: " + (Get-Item .\dist\TripCut\TripCut.exe).Length + " bytes")
