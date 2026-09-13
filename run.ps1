# 一键启动：双击本文件或在终端执行 .\run.ps1
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$python = Join-Path $PSScriptRoot 'condaenv1\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = 'python' }

if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.env'))) {
    Write-Host '提示：还没有 .env，把 .env.example 复制成 .env 并填上 API Key 再启动。' -ForegroundColor Yellow
}

Write-Host "启动中… 浏览器打开 http://127.0.0.1:8000" -ForegroundColor Cyan
& $python -m app.main
