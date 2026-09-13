{% autoescape off %}<#
.SYNOPSIS
    Vigil Agent uninstaller for Windows — {{ base_url }}
.DESCRIPTION
    Removes the service, the agent, its config and its state.
.EXAMPLE
    irm {{ base_url }}/agent/uninstall.ps1 | iex
.EXAMPLE
    $env:VIGIL_KEEP_CONFIG = "1"; irm {{ base_url }}/agent/uninstall.ps1 | iex
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = "Continue"

# Literal paths, deliberately. Nothing is interpolated into a recursive
# delete running as administrator.
$InstallDir  = "C:\Program Files\Vigil"
$ConfigDir   = "C:\ProgramData\Vigil"
$ConfigPath  = "C:\ProgramData\Vigil\agent.yml"
$DataDir     = "C:\ProgramData\Vigil\data"
$ServiceName = "vigil-agent"
$KeepConfig  = ($env:VIGIL_KEEP_CONFIG -eq "1")

$removed = 0
function Removed($what) { Write-Host "  removed $what"; $script:removed++ }

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -eq "Running") {
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        # The service releases its files on stop; deleting the directory while
        # it still holds them leaves a half-removed install.
        for ($i = 0; $i -lt 20; $i++) {
            if ((Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status -eq "Stopped") { break }
            Start-Sleep -Seconds 1
        }
    }
    & sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
    Removed "the vigil-agent service"
}

# Leftovers from an interrupted self-update, which stages beside the install.
foreach ($stray in @("$InstallDir.new", "$InstallDir.old",
                     "C:\Program Files\vigil-agent-update.cmd")) {
    if (Test-Path $stray) { Remove-Item -Recurse -Force $stray -ErrorAction SilentlyContinue }
}

if (Test-Path $InstallDir) {
    Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
    if (-not (Test-Path $InstallDir)) { Removed $InstallDir }
    else { Write-Host "  could not remove $InstallDir — a file is still in use" }
}

# State: the nonce store and the pinned server key. A re-install must not
# inherit a key pin from a previous life.
if (Test-Path $DataDir) {
    Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue
    Removed $DataDir
}

if ($KeepConfig) {
    Write-Host "  kept $ConfigPath (VIGIL_KEEP_CONFIG=1)"
} else {
    if (Test-Path $ConfigPath) { Remove-Item -Force $ConfigPath; Removed $ConfigPath }
    if (Test-Path "$ConfigDir\agent.log") { Remove-Item -Force "$ConfigDir\agent.log" }
    # Only when empty: an operator may keep scripts for execute_script here.
    if ((Test-Path $ConfigDir) -and
        -not (Get-ChildItem $ConfigDir -Force -ErrorAction SilentlyContinue)) {
        Remove-Item -Force $ConfigDir
        Write-Host "  removed $ConfigDir (was empty)"
    }
}

Write-Host ""
if ($removed -eq 0) {
    Write-Host "Nothing to remove — no Vigil agent found on this host."
} else {
    Write-Host "Vigil agent uninstalled ($removed item(s) removed)."
    Write-Host "The host record remains in Vigil; delete it there if you want"
    Write-Host "the history gone as well."
}
{% endautoescape %}
