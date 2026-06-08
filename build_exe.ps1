$ErrorActionPreference = "Stop"

Write-Host "=== DashcamVideoJoiner build start ==="

Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}

$python = ".\.venv\Scripts\python.exe"

Write-Host "Installing requirements..."
& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt

Write-Host "Cleaning old build outputs..."
if (Test-Path "build") {
    Remove-Item -Recurse -Force "build"
}
if (Test-Path "dist") {
    Remove-Item -Recurse -Force "dist"
}

Write-Host "Running PyInstaller..."
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name DashcamVideoJoiner `
    "src\dashcam_joiner.py"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed."
}

Write-Host "Copying ffmpeg tools..."
$distTools = "dist\DashcamVideoJoiner\tools"
New-Item -ItemType Directory -Force $distTools | Out-Null

if (Test-Path "tools\ffmpeg.exe") {
    Copy-Item "tools\ffmpeg.exe" $distTools -Force
} else {
    Write-Warning "tools\ffmpeg.exe not found. Copy it manually before running the app."
}

if (Test-Path "tools\ffprobe.exe") {
    Copy-Item "tools\ffprobe.exe" $distTools -Force
} else {
    Write-Warning "tools\ffprobe.exe not found. Copy it manually before running the app."
}

Write-Host ""
Write-Host "=== Build completed ==="
Write-Host "Run:"
Write-Host "  dist\DashcamVideoJoiner\DashcamVideoJoiner.exe"
Write-Host ""
Write-Host "Check tools:"
Write-Host "  dist\DashcamVideoJoiner\tools\ffmpeg.exe"
Write-Host "  dist\DashcamVideoJoiner\tools\ffprobe.exe"
