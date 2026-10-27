# Flixer 启动器（带日志，便于排查）
# 路径自适应：放在哪个目录就启动哪个目录的项目，克隆到别处也能直接用
$ErrorActionPreference = 'SilentlyContinue'
$root = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$port = 7100
$log = Join-Path $root 'launch.log'

function Write-Log($msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $log -Value "[$ts] $msg" -Encoding UTF8
}

function Test-Port {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

Write-Log "--- 启动请求（$root）---"

if (-not (Test-Port)) {
    Write-Log '服务未运行，开始启动 python server.py'
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'python'
    $psi.Arguments = 'server.py'
    $psi.WorkingDirectory = $root
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
        Write-Log "已启动 python，PID=$($p.Id)"
    } catch {
        Write-Log "启动失败: $($_.Exception.Message)"
    }
} else {
    Write-Log '服务已在运行，跳过启动'
}

$ready = $false
for ($i = 0; $i -lt 80; $i++) {
    if (Test-Port) { $ready = $true; break }
    Start-Sleep -Milliseconds 500
}
Write-Log ("端口就绪: " + $ready)

if ($ready) {
    try {
        $shell = New-Object -ComObject Shell.Application
        $shell.ShellExecute('http://127.0.0.1:7100', '', '', 'open', 1)
        Write-Log '已调用浏览器打开 http://127.0.0.1:7100'
    } catch {
        Write-Log ("打开浏览器失败: " + $_.Exception.Message)
        try { Start-Process 'http://127.0.0.1:7100' } catch {}
    }
} else {
    Write-Log '超时：服务没有起来'
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            "Flixer 启动超时。`n`n请查看日志：`n$log`n`n常见原因：`n· python 不在 PATH`n· 端口 7100 被占用`n· data/quark_cookie.json 缺失导致启动报错",
            'Flixer', 'OK', 'Warning') | Out-Null
    } catch {}
}
