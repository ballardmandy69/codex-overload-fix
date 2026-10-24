#Requires -Version 5.1
<#
.SYNOPSIS
    Install a local relay with optional Clash HTTP/mixed proxy routing.
.DESCRIPTION
    Creates a hidden single-instance supervisor; prints the Codex configuration.
    No proxy means direct routing; Windows system proxy settings are not imported.
    @author lyt @date 2026-10-24
.EXAMPLE
    .\scripts\install.ps1 -ClashPort 7890 -PythonPath C:\Python312\python.exe
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 8787,
    [string]$Proxy = '',
    [ValidateRange(0, 65535)][int]$ClashPort = 0,
    [ValidateSet('127.0.0.1', 'localhost')][string]$ClashHost = '127.0.0.1',
    [string]$PythonPath = '',
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA 'codex-overload-fix'),
    [switch]$NoAutostart,
    [switch]$NoStart
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$relayPath = (Resolve-Path -LiteralPath (Join-Path $root 'relay\relay.py')).Path
$watchPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'watch.ps1')).Path
$StateDir = [IO.Path]::GetFullPath($StateDir)
if ($Proxy -and $ClashPort) { throw 'Use -Proxy or -ClashPort, not both.' }
if ($ClashPort) { $Proxy = 'http://' + $ClashHost + ':' + $ClashPort }
if ($Proxy) {
    if ($Proxy -notmatch '://') { $Proxy = 'http://' + $Proxy }
    $proxyUri = [Uri]$Proxy
    if ($proxyUri.Scheme -ne 'http' -or $proxyUri.Host -notin @('127.0.0.1', 'localhost', '[::1]') -or
        $proxyUri.UserInfo -or $proxyUri.Query -or $proxyUri.Fragment -or $proxyUri.AbsolutePath -ne '/' -or
        $proxyUri.Port -lt 1 -or $proxyUri.Port -gt 65535) {
        throw 'Use a loopback HTTP proxy URL and the Clash HTTP/mixed port (no SOCKS/HTTPS proxy).'
    }
    if ($proxyUri.Port -eq $Port) { throw 'Relay and Clash must use different ports.' }
    $Proxy = $proxyUri.GetLeftPart([UriPartial]::Authority)
}
if (-not $PythonPath) {
    foreach ($candidate in @('py', 'python', 'python3')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $command -or $command.Source -match 'WindowsApps') { continue }
        $prefix = @()
        if ($candidate -eq 'py') { $prefix = @('-3') }
        $found = & $command.Source @prefix -c 'import sys;print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) { $PythonPath = [string]($found | Select-Object -Last 1); break }
    }
}
if (-not $PythonPath) { throw 'Python 3.9+ required. Supply -PythonPath with a real Python executable.' }
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
& $PythonPath -c 'import sys;sys.exit(0 if sys.version_info >= (3,9) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.9+ required.' }
foreach ($value in @($PythonPath, $relayPath, $watchPath, $StateDir, $Proxy)) {
    if ($value -match '["\r\n]') { throw 'Paths and proxy values cannot contain quotes or newlines.' }
}
$occupied = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($occupied.Count) { throw "Port $Port occupied. Stop the existing relay before reinstalling." }
if ($Proxy) {
    $probe = New-Object Net.Sockets.TcpClient
    try {
        $task = $probe.ConnectAsync($proxyUri.Host.Trim('[', ']'), $proxyUri.Port)
        if (-not $task.Wait(2000) -or -not $probe.Connected) { throw 'Clash port is not listening.' }
    } finally { $probe.Dispose() }
    Write-Host "Proxy: $Proxy (HTTP/mixed port must be enabled in Clash)"
}
New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
$launcher = Join-Path $StateDir 'start-relay.vbs'
$state = @{ Port = $Port; Proxy = $Proxy; RelayPath = $relayPath; PythonPath = $PythonPath; WatchPath = $watchPath }
$state | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $StateDir 'install.json') -Encoding UTF8
$powerShellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$watchCommand = '"' + $powerShellPath + '" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    $watchPath + '" -StateDir "' + $StateDir + '"'
$vbs = 'CreateObject("WScript.Shell").Run "' + $watchCommand.Replace('"', '""') + '", 0, False'
Set-Content -LiteralPath $launcher -Value $vbs -Encoding Unicode
if (-not $NoStart) {
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $launcher + '"') -WindowStyle Hidden
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 250
        try {
            $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/health")
            $request.Proxy = $null
            $request.Timeout = 500
            $response = $request.GetResponse()
            try {
                $reader = New-Object IO.StreamReader($response.GetResponseStream())
                try { $health = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
            } finally { $response.Dispose() }
            $listener = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
            $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener[0].OwningProcess)"
            $ready = $health.service -eq 'codex-overload-fix' -and
                $process.CommandLine.Contains('"' + $relayPath + '"')
            if ($ready) { break }
        } catch { }
    }
    if (-not $ready) { throw "Relay failed to start. Inspect $StateDir\watch.log and relay.log." }
}
if (-not $NoAutostart) {
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    New-Item -Path $runKey -Force | Out-Null
    Set-ItemProperty -Path $runKey -Name 'CodexOverloadRelay' -Value ('wscript.exe "' + $launcher + '"')
}
Write-Host "Prepared relay at 127.0.0.1:$Port; state: $StateDir"
Write-Host 'Back up config.toml; put model_provider at the TOP LEVEL (before any [table]).'
@"
model_provider = "openai-retry"

[model_providers.openai-retry]
name = "OpenAI (local retry)"
base_url = "http://127.0.0.1:$Port"
wire_api = "responses"
requires_openai_auth = true
stream_max_retries = 0
request_max_retries = 0
"@ | Write-Host
