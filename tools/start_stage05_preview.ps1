param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [switch]$UseLocalServices
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Frontend = Join-Path $ProjectRoot "web\dist\index.html"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "项目虚拟环境不存在：$Python"
}
if (-not (Test-Path -LiteralPath $Frontend)) {
    throw "前端尚未构建，请先在 web 目录运行 pnpm run build。"
}

Set-Location -LiteralPath $ProjectRoot
Write-Host "页伴开发预览：http://127.0.0.1:$Port/"
if ($UseLocalServices) {
    if (-not $env:READING_AGENT_STAGE05_POSTGRES_DSN) {
        $env:READING_AGENT_STAGE05_POSTGRES_DSN = "postgresql://postgres@127.0.0.1:15432/reading_agent_stage05"
    }
    if (-not $env:READING_AGENT_STAGE05_MINIO_ENDPOINT) {
        $env:READING_AGENT_STAGE05_MINIO_ENDPOINT = "127.0.0.1:19000"
    }
    if (-not $env:READING_AGENT_STAGE05_MINIO_ACCESS_KEY) {
        $env:READING_AGENT_STAGE05_MINIO_ACCESS_KEY = "reading_agent_local"
    }
    if (-not $env:READING_AGENT_STAGE05_MINIO_SECRET_KEY) {
        $env:READING_AGENT_STAGE05_MINIO_SECRET_KEY = "stage05-local-minio-secret"
    }
}
if (-not $env:READING_AGENT_PREVIEW_EMAIL) {
    $env:READING_AGENT_PREVIEW_EMAIL = "reader@example.local"
}
if (-not $env:READING_AGENT_PREVIEW_PASSWORD) {
    $env:READING_AGENT_PREVIEW_PASSWORD = "reading-demo"
}
Write-Host "演示账号：$env:READING_AGENT_PREVIEW_EMAIL / $env:READING_AGENT_PREVIEW_PASSWORD"
& $Python -m uvicorn reading_agent.stage05:app --app-dir src --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
