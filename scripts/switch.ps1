#Requires -Version 5.1
<# Windows direct/relay shortcuts and guarded switching. @author lyt @date 2026-11-16 #>
[CmdletBinding(DefaultParameterSetName = 'Switch')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Switch')]
    [ValidateSet('Direct', 'Relay')][string]$Mode,
    [Parameter(Mandatory, ParameterSetName = 'Shortcuts')][switch]$CreateShortcuts,
    [string]$StateDir = (Join-Path $env:LOCALAPPDATA 'codex-overload-fix'),
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex\config.toml'),
    [switch]$Notify
)
$ErrorActionPreference = 'Stop'
$mutex = $null
$owned = $false
try {
    $StateDir = [IO.Path]::GetFullPath($StateDir)
    $state = Get-Content -LiteralPath (Join-Path $StateDir 'install.json') -Raw | ConvertFrom-Json
    $helper = Join-Path $PSScriptRoot 'switch_config.py'
    if ($CreateShortcuts) {
        $shell = New-Object -ComObject WScript.Shell
        $desktop = [Environment]::GetFolderPath('Desktop')
        $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        foreach ($choice in @('Direct', 'Relay')) {
            $link = $shell.CreateShortcut((Join-Path $desktop "Codex $choice.lnk"))
            $link.TargetPath = $powershell
            $link.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
                $PSCommandPath + '" -Mode ' + $choice + ' -StateDir "' + $StateDir +
                '" -ConfigPath "' + $ConfigPath + '" -Notify'
            $link.WorkingDirectory = $PSScriptRoot
            $link.WindowStyle = 7
            $link.Description = "Switch Codex to $choice; restart Codex after switching."
            $link.Save()
        }
        Write-Host 'Created desktop shortcuts: Codex Direct / Codex Relay.'
        return
    }
    $mutex = New-Object Threading.Mutex($false, "Local\CodexOverloadSwitch-$($state.Port)")
    try { $owned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) { throw 'Another switch is running. Please wait.' }
    & $state.PythonPath $helper $ConfigPath $Mode.ToLowerInvariant() --port $state.Port --check
    if ($LASTEXITCODE -ne 0) { throw 'Configuration validation failed; no changes made.' }
    if ($Mode -eq 'Relay') {
        $task = Get-ScheduledTask -TaskName $state.TaskName -ErrorAction Stop
        $launcher = '"' + (Join-Path $StateDir 'start-relay.vbs') + '"'
        $actions = @($task.Actions)
        if ($actions.Count -ne 1 -or
            [IO.Path]::GetFileName($actions[0].Execute) -ne 'wscript.exe' -or
            -not ([string]$actions[0].Arguments).Contains($launcher)) {
            throw 'Scheduled task is not owned by this installation.'
        }
        Enable-ScheduledTask -TaskName $state.TaskName | Out-Null
        Start-ScheduledTask -TaskName $state.TaskName
        $ready = $false
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            try {
                $request = [Net.HttpWebRequest]::Create("http://127.0.0.1:$($state.Port)/health")
                $request.Proxy = $null
                $request.Timeout = 500
                $response = $request.GetResponse()
                try {
                    $reader = New-Object IO.StreamReader($response.GetResponseStream())
                    try { $health = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
                } finally { $response.Dispose() }
                $listeners = @(Get-NetTCPConnection -LocalPort $state.Port -State Listen -ErrorAction Stop)
                $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listeners[0].OwningProcess)"
                $ready = $health.service -eq 'codex-overload-fix' -and
                    $process.ExecutablePath -eq $state.PythonPath -and
                    $process.CommandLine.Contains('"' + $state.RelayPath + '"')
                if ($ready) { break }
            } catch { }
            Start-Sleep -Milliseconds 250
        }
        if (-not $ready) {
            & $state.PythonPath $helper $ConfigPath direct --port $state.Port
            if ($LASTEXITCODE -ne 0) { throw 'Relay failed and direct fallback failed; inspect config.toml.' }
            & (Join-Path $PSScriptRoot 'uninstall.ps1') -StateDir $StateDir -KeepTask
            throw 'Relay health check failed; direct mode restored. Inspect watch.log / relay.log.'
        }
    }
    & $state.PythonPath $helper $ConfigPath $Mode.ToLowerInvariant() --port $state.Port
    if ($LASTEXITCODE -ne 0) { throw 'Configuration update failed; inspect the error above.' }
    if ($Mode -eq 'Direct') {
        & (Join-Path $PSScriptRoot 'uninstall.ps1') -StateDir $StateDir -KeepTask
    }
    Add-Content -LiteralPath (Join-Path $StateDir 'switch.log') -Value (
        (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + " mode=$Mode config=$ConfigPath")
    $message = "Codex mode: $Mode. Fully close and restart Codex to load the new endpoint."
    Write-Host $message
    if ($Notify) { (New-Object -ComObject WScript.Shell).Popup($message, 0, 'Codex Switch', 64) | Out-Null }
} catch {
    if ($Notify) {
        (New-Object -ComObject WScript.Shell).Popup($_.Exception.Message, 0, 'Codex Switch failed', 16) | Out-Null
    }
    Write-Error $_
    exit 1
} finally {
    if ($owned) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
