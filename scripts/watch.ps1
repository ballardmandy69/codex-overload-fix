#Requires -Version 5.1
<# Local relay supervisor. @author lyt @date 2026-10-24 #>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$StateDir)
$ErrorActionPreference = 'Stop'
$state = $null
$mutex = $null
$owned = $false
$watchLog = Join-Path $StateDir 'watch.log'
function Write-WatchLog([string]$Message) {
    Add-Content -LiteralPath $watchLog -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $Message)
}
try {
    $state = Get-Content -LiteralPath (Join-Path $StateDir 'install.json') -Raw | ConvertFrom-Json
    $mutex = New-Object System.Threading.Mutex($false, "Local\CodexOverloadRelay-$($state.Port)")
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { exit 0 }
    $relayArgs = @('"' + $state.RelayPath + '"', '--port', $state.Port,
        '--log', '"' + (Join-Path $StateDir 'relay.log') + '"')
    if ($state.Proxy) { $relayArgs += @('--proxy', '"' + $state.Proxy + '"') }
    while ($true) {
        $occupied = @(Get-NetTCPConnection -State Listen -LocalPort $state.Port -ErrorAction SilentlyContinue)
        if ($occupied.Count) {
            Write-WatchLog "Port $($state.Port) occupied; supervisor stopped."
            exit 2
        }
        try {
            $child = Start-Process -FilePath $state.PythonPath -ArgumentList $relayArgs -WindowStyle Hidden -PassThru
            $child.WaitForExit()
            Write-WatchLog "Relay exited with code $($child.ExitCode); restarting."
        } catch {
            Write-WatchLog "Relay launch failed: $($_.Exception.Message)"
        }
        Start-Sleep -Seconds 3
    }
} catch {
    try { Write-WatchLog "Supervisor failed: $($_.Exception.Message)" } catch { }
    exit 1
} finally {
    if ($owned -and $mutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
