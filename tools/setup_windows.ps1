#Requires -Version 5.1
# 红米 G Pro 2024 / RTX 4060 Laptop 一键环境（Windows 10/11）
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Assert-Command($Name, $Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "缺少 $Name。$Hint"
    }
}

Write-Host "=== BiliArchiver Windows / RTX 4060 安装 ===" -ForegroundColor Cyan

$py = $null
foreach ($candidate in @("py -3.12", "py -3.11", "python")) {
    try {
        $ver = Invoke-Expression "$candidate -c `"import sys; print(sys.version)`""
        if ($ver -match "^3\.(11|12|13)") {
            $py = $candidate
            Write-Host "使用 Python：$ver"
            break
        }
    } catch {}
}
if (-not $py) {
    throw "需要 Python 3.11 或 3.12（推荐 3.12）。请从 https://www.python.org/downloads/windows/ 安装，并勾选 Add python.exe to PATH。"
}

if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Write-Warning "没有 nvidia-smi。请先安装 NVIDIA 显卡驱动，再重跑本脚本。"
} else {
    nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
}

if (-not (Test-Path .venv)) {
    Invoke-Expression "$py -m venv .venv"
}
$venvPython = Join-Path $PWD ".venv\Scripts\python.exe"
& $venvPython -m pip install -U pip
& $venvPython -m pip install -r requirements-gpu-windows.txt

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "尝试用 winget 安装 ffmpeg..."
    try {
        winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
    } catch {
        Write-Warning "winget 安装 ffmpeg 失败。请手动安装，或把 ffmpeg.exe 放到 tools\bin\ffmpeg.exe"
    }
}

Write-Host "`n检测 GPU / Whisper：" -ForegroundColor Cyan
& $venvPython tools\check_gpu.py
Write-Host "`n启动：" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "  python run.py"
Write-Host "浏览器打开 http://127.0.0.1:8765"
Write-Host "首次使用请到 Windows 图形设置，把 .venv\Scripts\python.exe 指定为高性能 NVIDIA。"
