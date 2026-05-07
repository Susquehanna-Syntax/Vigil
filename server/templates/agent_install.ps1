{% autoescape off %}<#
.SYNOPSIS
    Vigil Agent installer for Windows — {{ base_url }}
.DESCRIPTION
    Downloads and installs the Vigil agent as a Windows service.
.EXAMPLE
    irm {{ base_url }}/agent/install.ps1 | iex
.EXAMPLE
    $env:VIGIL_TOKEN = "<token>"; irm {{ base_url }}/agent/install.ps1 | iex
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$VigilServer = "{{ base_url }}"
$InstallDir  = "C:\Program Files\Vigil"
$ConfigDir   = "C:\ProgramData\Vigil"
$BinaryPath  = Join-Path $InstallDir "vigil-agent.exe"
$ConfigPath  = Join-Path $ConfigDir  "agent.yml"
$ServiceName = "vigil-agent"
$Platform    = "windows-amd64"

Write-Host "Installing Vigil agent for $Platform..."

# Create directories
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $ConfigDir  | Out-Null

# Download binary
$DownloadUrl = "$VigilServer/agent/download/$Platform/"
Write-Host "Downloading from $DownloadUrl"
Invoke-WebRequest -Uri $DownloadUrl -OutFile $BinaryPath -UseBasicParsing

# Write config if not present
if (-not (Test-Path $ConfigPath)) {
    $token = if ($env:VIGIL_TOKEN) { $env:VIGIL_TOKEN } else { "REPLACE_WITH_TOKEN" }
    @"
server_url: "$VigilServer"
agent_token: "$token"
mode: monitor
checkin_interval: 30
"@ | Set-Content -Path $ConfigPath -Encoding UTF8

    if ($env:VIGIL_TOKEN) {
        Write-Host "Agent token configured from VIGIL_TOKEN."
    } else {
        Write-Host "Config written to $ConfigPath — set agent_token before starting."
    }
}

# Install / update Windows service
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    & sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

& sc.exe create $ServiceName binPath= "`"$BinaryPath`"" start= auto DisplayName= "Vigil Monitoring Agent" | Out-Null
& sc.exe description $ServiceName "Vigil agent — outbound-only monitoring and managed tasks." | Out-Null
& sc.exe failure $ServiceName reset= 60 actions= restart/10000/restart/10000/restart/30000 | Out-Null

if ($env:VIGIL_TOKEN) {
    Start-Service -Name $ServiceName
    Write-Host ""
    Write-Host "Vigil agent installed and started."
    Write-Host "Approve this host in Vigil Settings > Enrollment Queue."
} else {
    Write-Host ""
    Write-Host "Vigil agent installed."
    Write-Host "  1. Edit $ConfigPath and set agent_token"
    Write-Host "  2. Start-Service $ServiceName"
    Write-Host "  3. Approve the host in Vigil Settings > Enrollment Queue"
}
{% endautoescape %}
