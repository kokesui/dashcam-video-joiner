# build_exe.ps1
# Dashcam Video Joiner を PyInstaller で exe 化するスクリプト
# 実行方法: PowerShell で .\build_exe.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# 仮想環境が存在する場合はアクティベート
$VenvActivate = Join-Path $ScriptDir ".venv\Scripts\Activate.ps1"
if (Test-Path $VenvActivate) {
    Write-Host "仮想環境をアクティベートします: $VenvActivate"
    & $VenvActivate
} else {
    Write-Host "仮想環境なし — システム Python を使用します"
}

# PyInstaller 確認
$PyInstaller = $null
try {
    $PyInstaller = (Get-Command pyinstaller -ErrorAction Stop).Source
    Write-Host "PyInstaller: $PyInstaller"
} catch {
    Write-Error "PyInstaller が見つかりません。pip install pyinstaller を実行してください。"
    exit 1
}

# ビルド実行 (onedir 方式)
Write-Host "ビルド開始..."
pyinstaller `
    --name "DashcamVideoJoiner" `
    --onedir `
    --windowed `
    --noconfirm `
    "src\dashcam_joiner.py"

if ($LASTEXITCODE -ne 0) {
    Write-Error "ビルドに失敗しました (終了コード: $LASTEXITCODE)"
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "ビルド完了: dist\DashcamVideoJoiner\"
Write-Host ""
Write-Host "次のステップ:"
Write-Host "  1. dist\DashcamVideoJoiner\tools\ フォルダを作成"
Write-Host "  2. ffmpeg.exe と ffprobe.exe をそのフォルダにコピー"
Write-Host "  3. dist\DashcamVideoJoiner\DashcamVideoJoiner.exe を起動"
