#Requires -Version 5.1
<# Local relay supervisor. @author lyt @date 2026-10-24 #>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$StateDir)
$ErrorActionPreference = 'Stop'
$state = Get-Content -LiteralPath (Join-Path $StateDir 'install.json') -Raw | ConvertFrom-Json
$mutex = New-Object System.Threading.Mutex($false, "Local\CodexOverloadRelay-$($state.Port)")
$owned = $false
try {
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { exit 0 }
    $relayArgs = @('"' + $state.RelayPath + '"', '--port', $state.Port,
        '--log', '"' + (Join-Path $StateDir 'relay.log') + '"')
    if ($state.Proxy) { $relayArgs += @('--proxy', '"' + $state.Proxy + '"') }
    while ($true) {
        $occupied = @(Get-NetTCPConnection -State Listen -LocalPort $state.Port -ErrorAction SilentlyContinue)
        if ($occupied.Count) {
            Add-Content -LiteralPath (Join-Path $StateDir 'watch.log') -Value 'Port occupied; supervisor stopped.'
            break
        }
        $child = Start-Process -FilePath $state.PythonPath -ArgumentList $relayArgs -WindowStyle Hidden -PassThru
        $child.WaitForExit()
        Start-Sleep -Seconds 3
    }
} finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
