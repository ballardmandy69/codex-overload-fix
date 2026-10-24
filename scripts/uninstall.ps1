#Requires -Version 5.1
<# Remove only processes owned by this local installation. @author lyt @date 2026-10-24 #>
[CmdletBinding()]
param(
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA 'codex-overload-fix'),
    [switch]$Purge
)
$ErrorActionPreference = 'Stop'
$StateDir = [IO.Path]::GetFullPath($StateDir).TrimEnd('\')
$manifest = Join-Path $StateDir 'install.json'
if (-not (Test-Path -LiteralPath $manifest)) {
    throw 'Installation manifest missing; refusing broad process termination.'
}
$state = Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$entry = Get-ItemProperty -Path $runKey -Name 'CodexOverloadRelay' -ErrorAction SilentlyContinue
$launcher = '"' + (Join-Path $StateDir 'start-relay.vbs') + '"'
if ($entry -and $entry.CodexOverloadRelay.Contains($launcher)) {
    Remove-ItemProperty -Path $runKey -Name 'CodexOverloadRelay'
}
$processes = @(Get-CimInstance Win32_Process)
foreach ($process in $processes) {
    $line = $process.CommandLine
    if ($process.ProcessId -eq $PID -or -not $line) { continue }
    $watch = $process.Name -in @('powershell.exe', 'pwsh.exe') -and
        $line.Contains('"' + $state.WatchPath + '"') -and $line.Contains('"' + $StateDir + '"')
    $vbs = $process.Name -eq 'wscript.exe' -and $line.Contains($launcher)
    if ($watch -or $vbs) { Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue }
}
foreach ($process in $processes) {
    if ($process.ExecutablePath -eq $state.PythonPath -and $process.CommandLine -and
        $process.CommandLine.Contains('"' + $state.RelayPath + '"') -and
        $process.CommandLine.Contains('"' + (Join-Path $StateDir 'relay.log') + '"')) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
if ($Purge) {
    $resolved = (Resolve-Path -LiteralPath $StateDir).Path.TrimEnd('\')
    if ($resolved -ne $StateDir -or $resolved -eq [IO.Path]::GetPathRoot($resolved).TrimEnd('\') -or
        -not (Test-Path -LiteralPath (Join-Path $resolved 'start-relay.vbs')) -or
        ((Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Unsafe purge directory; remove generated files manually.'
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
Write-Host 'Stopped this installation. Restore your previous top-level model_provider in config.toml.'
