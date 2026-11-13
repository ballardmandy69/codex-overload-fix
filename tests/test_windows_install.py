"""Isolated Windows installer regressions. @author lyt @date 2026-11-14"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import venv


REPOSITORY = pathlib.Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
CSCRIPT = shutil.which("cscript.exe")
WSCRIPT = shutil.which("wscript.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL and CSCRIPT and WSCRIPT, "Windows Script Host required")
class WindowsInstallTest(unittest.TestCase):
    """Exercise generated launchers and scoped cleanup. @author lyt @date 2026-11-14"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix=".test-install-", dir=REPOSITORY)
        self.root = pathlib.Path(self.directory.name).resolve()
        self.assertEqual(self.root.parent, REPOSITORY)
        self.addCleanup(self.directory.cleanup)
        self.project = self.root / "project"
        (self.project / "scripts").mkdir(parents=True)
        (self.project / "relay").mkdir()
        for name in ("install.ps1", "uninstall.ps1"):
            shutil.copyfile(REPOSITORY / "scripts" / name, self.project / "scripts" / name)
        (self.project / "relay" / "relay.py").write_text("# isolated fixture\n", encoding="utf-8")
        (self.project / "scripts" / "watch.ps1").write_text(
            "param([string]$StateDir)\n"
            "Start-Sleep -Milliseconds 200\n"
            "Set-Content -LiteralPath (Join-Path $StateDir 'watch-ran') -Value 'ran'\n"
            "exit 37\n",
            encoding="utf-8-sig",
        )
        self.state = self.root / "状态 空格"

    def powershell(self, source, python=None):
        harness = self.root / "harness.ps1"
        harness.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)\n" + source,
            encoding="utf-8-sig",
        )
        environment = dict(os.environ)
        environment.update({
            "RELAY_TEST_PROJECT": str(self.project),
            "RELAY_TEST_STATE": str(self.state),
            "RELAY_TEST_PYTHON": str(python or sys.executable),
        })
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
            env=environment, capture_output=True, encoding="utf-8", errors="replace", timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def install(self, discover=False, python=None):
        discovery = ""
        python_argument = "-PythonPath $env:RELAY_TEST_PYTHON"
        if discover:
            discovery = """
function Get-Command {
    [CmdletBinding()] param([string]$Name)
    if ($Name -eq 'python') { [PSCustomObject]@{ Source = $env:RELAY_TEST_PYTHON } }
}
"""
            python_argument = ""
        self.powershell(discovery + """
function Get-NetTCPConnection {
    [CmdletBinding()] param([int]$LocalPort, [string]$State)
}
$installer = Join-Path $env:RELAY_TEST_PROJECT 'scripts/install.ps1'
& $installer -Port 28787 -StateDir $env:RELAY_TEST_STATE -NoAutostart -NoStart """
                        + python_argument + "\n", python=python)
        return json.loads((self.state / "install.json").read_text(encoding="utf-8-sig"))

    def test_discovery_preserves_unicode_python_path(self):
        environment = self.root / "解释器 空格"
        venv.EnvBuilder(with_pip=False).create(environment)
        python = environment / "Scripts" / "python.exe"
        state = self.install(discover=True, python=python)
        self.assertEqual(pathlib.Path(state["PythonPath"]), python)
        self.assertEqual(pathlib.Path(state["LauncherPath"]), self.state / "start-relay.vbs")

    def test_hidden_launcher_waits_and_propagates_exit_code(self):
        self.install()
        launcher = self.state / "start-relay.vbs"
        self.assertTrue(launcher.read_bytes().startswith(b"\xff\xfe"))
        script = launcher.read_text(encoding="utf-16")
        self.assertIn(", 0, True)", script)
        self.assertIn('""' + str(self.project / "scripts" / "watch.ps1") + '""', script)
        for executable in (CSCRIPT, WSCRIPT):
            with self.subTest(executable=executable):
                marker = self.state / "watch-ran"
                marker.unlink(missing_ok=True)
                started = time.monotonic()
                result = subprocess.run(
                    [executable, "//B", "//NoLogo", str(launcher)],
                    capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
                self.assertGreaterEqual(time.monotonic() - started, 0.2)
                self.assertTrue(marker.is_file())

    def uninstall(self, action_types, foreign_run=True):
        self.state.mkdir(exist_ok=True)
        state = {
            "TaskName": "IsolatedRelayTest", "WatchPath": str(self.project / "scripts" / "watch.ps1"),
            "PythonPath": sys.executable, "RelayPath": str(self.project / "relay" / "relay.py"),
        }
        (self.state / "install.json").write_text(json.dumps(state), encoding="utf-8-sig")
        fixture = self.project / "fixture.json"
        fixture.write_text(json.dumps({"actions": action_types, "foreignRun": foreign_run}), encoding="utf-8-sig")
        output = self.powershell("""
$fixture = Get-Content -LiteralPath (Join-Path $env:RELAY_TEST_PROJECT 'fixture.json') -Raw | ConvertFrom-Json
$state = Get-Content -LiteralPath (Join-Path $env:RELAY_TEST_STATE 'install.json') -Raw | ConvertFrom-Json
$global:relayTestOperations = New-Object 'System.Collections.Generic.List[string]'
function Get-ItemProperty {
    [CmdletBinding()] param([string]$Path, [string]$Name)
    $directory = if ($fixture.foreignRun) { Join-Path $env:RELAY_TEST_STATE 'other' } else { $env:RELAY_TEST_STATE }
    [PSCustomObject]@{ CodexOverloadRelay = 'wscript.exe "' + (Join-Path $directory 'start-relay.vbs') + '"' }
}
function Remove-ItemProperty {
    [CmdletBinding()] param([string]$Path, [string]$Name)
    $global:relayTestOperations.Add('remove-run')
}
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    if (-not $fixture.actions.Count) { return }
    $actions = @($fixture.actions | ForEach-Object {
        if ($_ -eq 'legacy') {
            [PSCustomObject]@{ Execute = 'powershell.exe';
                Arguments = '-File "' + $state.WatchPath + '" -StateDir "' + $env:RELAY_TEST_STATE + '"' }
        } else {
            $directory = if ($_ -eq 'foreign') {
                Join-Path $env:RELAY_TEST_STATE 'other'
            } else { $env:RELAY_TEST_STATE }
            [PSCustomObject]@{ Execute = 'wscript.exe';
                Arguments = '//B //NoLogo "' + (Join-Path $directory 'start-relay.vbs') + '"' }
        }
    })
    [PSCustomObject]@{ Actions = $actions }
}
function Stop-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    $global:relayTestOperations.Add('stop-task')
}
function Unregister-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName, [switch]$Confirm)
    $global:relayTestOperations.Add('unregister-task')
}
function Get-CimInstance {
    [CmdletBinding()] param([string]$ClassName)
    [PSCustomObject]@{ ProcessId = 12345; Name = 'wscript.exe'; ExecutablePath = 'wscript.exe';
        CommandLine = 'wscript.exe "' + (Join-Path $env:RELAY_TEST_STATE 'start-relay.vbs') + '"' }
    [PSCustomObject]@{ ProcessId = 12346; Name = 'wscript.exe'; ExecutablePath = 'wscript.exe';
        CommandLine = 'wscript.exe "' + (Join-Path $env:RELAY_TEST_STATE 'other/start-relay.vbs') + '"' }
}
function Stop-Process {
    [CmdletBinding()] param([int]$Id, [switch]$Force)
    $global:relayTestOperations.Add('stop-process-' + $Id)
}
& (Join-Path $env:RELAY_TEST_PROJECT 'scripts/uninstall.ps1') -StateDir $env:RELAY_TEST_STATE
ConvertTo-Json -InputObject @($global:relayTestOperations) -Compress
""")
        return json.loads(output.strip().splitlines()[-1])

    def test_uninstall_accepts_legacy_and_hidden_tasks(self):
        for kind in ("legacy", "hidden"):
            with self.subTest(kind=kind):
                operations = self.uninstall([kind], foreign_run=False)
                self.assertIn("stop-task", operations)
                self.assertIn("unregister-task", operations)
                self.assertIn("remove-run", operations)
                self.assertIn("stop-process-12345", operations)
                self.assertNotIn("stop-process-12346", operations)

    def test_uninstall_preserves_foreign_and_mixed_tasks(self):
        for actions in ([], ["foreign"], ["hidden", "foreign"]):
            with self.subTest(actions=actions):
                operations = self.uninstall(actions)
                self.assertNotIn("stop-task", operations)
                self.assertNotIn("unregister-task", operations)
                self.assertNotIn("remove-run", operations)
                self.assertNotIn("stop-process-12346", operations)

    def test_powershell_scripts_parse(self):
        output = self.powershell("""
foreach ($name in @('install.ps1', 'uninstall.ps1')) {
    $tokens = $null
    $errors = $null
    $path = Join-Path $env:RELAY_TEST_PROJECT ('scripts/' + $name)
    [void][Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors | Out-String) }
}
'PARSE_OK'
""")
        self.assertIn("PARSE_OK", output)


if __name__ == "__main__":
    unittest.main()
