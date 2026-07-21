# PowerShell から aws_env.sh を実行するラッパー。
#   .\scripts\aws_env.ps1 token
#   .\scripts\aws_env.ps1 status
#
# Windows は .sh を直接実行できない（PowerShell で `scripts/aws_env.sh` と打つと
# 「認識されません」になるか、WSL の bash に渡って aws/uv が見つからず落ちる）。
# ここでは Git for Windows 同梱の bash（aws/uv と同じ Windows PATH を引き継ぐ）を
# 明示的に探して転送する。
$ErrorActionPreference = "Stop"

$candidates = @()
try {
    $gitPath = (Get-Command git -ErrorAction Stop).Source
    # 例: C:\Program Files\Git\cmd\git.exe → C:\Program Files\Git\bin\bash.exe
    $gitRoot = Split-Path (Split-Path $gitPath -Parent) -Parent
    $candidates += (Join-Path $gitRoot "bin\bash.exe")
} catch {}
$candidates += "C:\Program Files\Git\bin\bash.exe"
$candidates += "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe"

$bash = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $bash) {
    Write-Error "Git Bash が見つかりません。Git for Windows を入れるか、Git Bash から 'bash scripts/aws_env.sh <command>' を実行してください。"
    exit 1
}

$script = Join-Path $PSScriptRoot "aws_env.sh"
& $bash $script @args
exit $LASTEXITCODE
