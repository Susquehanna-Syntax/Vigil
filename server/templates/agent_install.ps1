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
# The agent's data_dir default is /var/lib/vigil-agent on every platform. On
# Windows that resolves to C:\var\lib\vigil-agent — a drive root where the
# default ACL lets any authenticated user create directories. That directory
# holds the nonce store and the pinned server public key, so it does not belong
# there. Pin it under ProgramData and lock it below.
$DataDir     = Join-Path $ConfigDir "data"

Write-Host "Installing Vigil agent for $Platform..."

# Create directories
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
New-Item -ItemType Directory -Force -Path $ConfigDir  | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir    | Out-Null

# Download to a temp file and verify it before anything makes it the service
# binary. This binary becomes a LocalSystem service, so an unverified download
# is a full machine compromise for anyone who can substitute the bytes in
# flight. install.sh has refused an unverified binary since 2026.12.0 and says
# so in as many words; this script did not, which left Windows accepting the
# same attack at a higher privilege level than Linux.
$DownloadUrl = "$VigilServer/agent/download/$Platform/"
$TmpAgent    = Join-Path $env:TEMP ("vigil-agent-" + [guid]::NewGuid().ToString("N") + ".exe")
Write-Host "Downloading from $DownloadUrl"

try {
    $Response = Invoke-WebRequest -Uri $DownloadUrl -OutFile $TmpAgent -UseBasicParsing -PassThru
} catch {
    # Write-Host, not Write-Error: Write-Error still wraps the message in
    # PowerShell's error-record formatting (CategoryInfo, FullyQualifiedErrorId,
    # a caret diagram of this very line), which is the noise this replaced.
    # install.sh fails with one line; match that.
    Write-Host "ERROR: could not download the agent from $DownloadUrl"
    Write-Host "  $($_.Exception.Message)"
    if (Test-Path $TmpAgent) { Remove-Item -Force $TmpAgent }
    exit 1
}

# Header lookup is case-insensitive here because PowerShell's header dictionary
# is, but do not rely on that: the Linux side was broken for months by assuming
# a case-insensitive match it did not actually have.
$ExpectedSha = $null
foreach ($k in $Response.Headers.Keys) {
    if ($k -and $k.ToLowerInvariant() -eq "x-vigil-sha256") {
        $v = $Response.Headers[$k]
        if ($v -is [array]) { $v = $v[0] }
        $ExpectedSha = ("$v").Trim().ToLowerInvariant()
    }
}

if ([string]::IsNullOrWhiteSpace($ExpectedSha)) {
    Write-Host "ERROR: the server did not publish a SHA-256 for this agent binary."
    Write-Host "Refusing to install an unverified binary that would run as LocalSystem."
    Write-Host "Upload the agent through Settings so its digest is recorded, or set"
    Write-Host "VIGIL_ALLOW_UNVERIFIED_AGENT=1 to override (not recommended)."
    if ($env:VIGIL_ALLOW_UNVERIFIED_AGENT -ne "1") {
        Remove-Item -Force $TmpAgent
        exit 1
    }
} else {
    $ActualSha = (Get-FileHash -Path $TmpAgent -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualSha -ne $ExpectedSha) {
        Write-Host "ERROR: agent binary failed SHA-256 verification."
        Write-Host "  expected: $ExpectedSha"
        Write-Host "  actual:   $ActualSha"
        Write-Host "The download was corrupted or tampered with. Nothing was installed."
        Remove-Item -Force $TmpAgent
        exit 1
    }
    Write-Host "Verified agent binary (sha256 $ActualSha)."
}

# Only now does it become the service binary. Stop the service first: a running
# agent holds its own exe open, and replacing it would fail after verification,
# leaving a verified binary that never got installed.
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing -and $existing.Status -eq "Running") {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# Two artifact shapes. A .zip is a PyInstaller --onedir build, which is the
# only one that can host a Windows service: a --onefile executable extracts
# and re-executes itself, so the process the SCM is watching never calls
# StartServiceCtrlDispatcher and Start-Service fails with 1053.
#
# Sniff the magic bytes rather than trusting the filename — the server sets
# Content-Disposition from whatever is on disk, and a mislabelled artifact
# should not decide how the service is installed.
$magic = [System.IO.File]::ReadAllBytes($TmpAgent)[0..1]
$IsZip = ($magic[0] -eq 0x50 -and $magic[1] -eq 0x4B)   # "PK"

if ($IsZip) {
    $ZipPath = "$TmpAgent.zip"
    Move-Item -Force -Path $TmpAgent -Destination $ZipPath
    # Clear the previous install so a renamed or removed file from an older
    # build cannot linger next to the new one.
    if (Test-Path $InstallDir) {
        Get-ChildItem -Path $InstallDir -Force | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
    Expand-Archive -Path $ZipPath -DestinationPath $InstallDir -Force
    Remove-Item -Force $ZipPath

    # PyInstaller --onedir nests everything under a directory named for the
    # build. Find the agent executable wherever it landed.
    $found = Get-ChildItem -Path $InstallDir -Filter "vigil-agent*.exe" -Recurse -File |
             Select-Object -First 1
    if (-not $found) {
        Write-Host "ERROR: the downloaded archive contains no vigil-agent executable."
        exit 1
    }
    $BinaryPath = $found.FullName
    Write-Host "Installed a onedir build; service binary: $BinaryPath"
} else {
    Move-Item -Force -Path $TmpAgent -Destination $BinaryPath
    Write-Host "WARNING: this is a --onefile build. It runs fine from a console,"
    Write-Host "but Windows cannot host it as a service — Start-Service will fail"
    Write-Host "with error 1053. Publish a --onedir zip to install the service."
}

# Write config if not present
if (-not (Test-Path $ConfigPath)) {
    # Generate the token rather than leaving a placeholder. The server stores
    # whatever the agent presents, so a literal "REPLACE_WITH_TOKEN" left in
    # place is a working credential published in this very script — and
    # register() being idempotent on the token means a second machine using it
    # inherits this host's already-approved identity with no admin action.
    # RNGCryptoServiceProvider, not Get-Random: this is a credential, and
    # PowerShell 5.1 on .NET Framework has no RandomNumberGenerator.Fill.
    if ($env:VIGIL_TOKEN) {
        $token = $env:VIGIL_TOKEN
    } else {
        $rngBytes = New-Object byte[] 32
        $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
        try { $rng.GetBytes($rngBytes) } finally { $rng.Dispose() }
        $token = -join ($rngBytes | ForEach-Object { $_.ToString("x2") })
    }
    # Single quotes around the Windows paths below, not double. A
    # double-quoted YAML scalar processes backslash escapes like JSON, so
    # "C:\ProgramData\Vigil\data" fails to parse on \P and \V and the agent
    # dies at startup with a ScannerError. Single-quoted YAML takes
    # backslashes literally.
    @"
server_url: "$VigilServer"
agent_token: "$token"
mode: monitor
checkin_interval: 30
data_dir: '$DataDir'
"@ | ForEach-Object {
        # Not Set-Content -Encoding UTF8: on PowerShell 5.1 — which is what
        # ships with Windows — that writes a UTF-8 BOM. The BOM becomes part
        # of the first YAML key, so the agent reads no server_url and exits
        # with "server_url is required" against a config that plainly has one.
        [System.IO.File]::WriteAllText(
            $ConfigPath, $_, (New-Object System.Text.UTF8Encoding($false)))
    }

    # agent.yml holds the agent token. install.sh writes it 0600; the Windows
    # default ACL on C:\ProgramData lets any local user read it. Strip
    # inheritance and grant only SYSTEM and Administrators.
    & icacls.exe $ConfigPath /inheritance:r /grant "SYSTEM:(F)" /grant "Administrators:(F)" | Out-Null

    if ($env:VIGIL_TOKEN) {
        Write-Host "Agent token configured from VIGIL_TOKEN."
    } else {
        Write-Host "Config written to $ConfigPath with a generated agent token."
    }
} elseif ($env:VIGIL_TOKEN) {
    # Re-adding a machine: keep its config but take the token the wizard is waiting for.
    # [^\r\n]* rather than .* so a CRLF file keeps its \r. No BOM, same as the first write.
    $existingConfig = [System.IO.File]::ReadAllText($ConfigPath)
    $existingConfig = $existingConfig -replace '(?m)^agent_token:[^\r\n]*', "agent_token: `"$($env:VIGIL_TOKEN)`""
    [System.IO.File]::WriteAllText(
        $ConfigPath, $existingConfig, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Existing config kept; agent token replaced from VIGIL_TOKEN."
}

# Install / update Windows service
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    & sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

# --service is required. Without it the SCM starts a console process that never
# calls StartServiceCtrlDispatcher, waits ~30s, and fails with 1053 — which is
# what this installer produced for its whole life, because no Windows binary
# existed to try starting.
# -c explicitly: the service must not depend on default-path resolution. The
# agent looked only at /etc/vigil/agent.yml and ./agent.yml, so on Windows it
# found nothing and exited immediately after the SCM started it.
$BinPathArg = "`"$BinaryPath`" --service -c `"$ConfigPath`""
# The same value as cmd.exe needs to see it: sc's binPath is one argument, so
# the inner quotes around the paths are escaped for cmd, not for PowerShell.
$BinPathQuoted = '"\"' + $BinaryPath + '\" --service -c \"' + $ConfigPath + '\""' 

# Mirror install.sh: monitor mode gives up privilege, task-executing modes
# cannot. NT SERVICE\vigil-agent is a virtual account — the SCM creates and
# manages it, there is no password to store, and it is scoped to this service
# alone rather than shared like LocalService.
$AgentMode = "monitor"
if (Test-Path $ConfigPath) {
    $modeLine = Select-String -Path $ConfigPath -Pattern '^\s*mode:\s*(\S+)' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($modeLine) { $AgentMode = $modeLine.Matches[0].Groups[1].Value.Trim('"').Trim("'") }
}

if ($AgentMode -eq "monitor") {
    $ServiceAccount = "NT SERVICE\$ServiceName"
    # Route sc.exe through cmd.exe. PowerShell splits `obj= "NT SERVICE\name"`
    # in a way sc rejects with 1639 and a usage dump — the value contains a
    # space and PowerShell's native argument passing does not preserve what
    # sc's own parser expects.
    $create = 'sc create ' + $ServiceName + ' binPath= ' + $BinPathQuoted + ' start= auto obj= "' + $ServiceAccount + '" DisplayName= "Vigil Monitoring Agent"'
    cmd.exe /c $create | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Could not create the service under a virtual account; falling back to LocalSystem."
        cmd.exe /c ('sc create ' + $ServiceName + ' binPath= ' + $BinPathQuoted + ' start= auto DisplayName= "Vigil Monitoring Agent"') | Out-Null
        $ServiceAccount = "LocalSystem"
    } else {
        # The virtual account exists only once the service does, so grant its
        # access now: read the config and binary, write its own state.
        & icacls.exe $ConfigPath /grant "$($ServiceAccount):(R)" | Out-Null
        & icacls.exe $DataDir /inheritance:r /grant "SYSTEM:(OI)(CI)(F)" /grant "Administrators:(OI)(CI)(F)" /grant "$($ServiceAccount):(OI)(CI)(M)" | Out-Null
        # The whole install tree, not just the exe. A onedir build is an exe
        # plus an _internal directory of DLLs and data; granting the account
        # access to the exe alone starts a process that dies immediately
        # because it cannot load anything beside it.
        & icacls.exe $InstallDir /grant "$($ServiceAccount):(OI)(CI)(RX)" /T | Out-Null
        Write-Host "Monitor mode: running the agent as the unprivileged '$ServiceAccount'."
    }
} else {
    cmd.exe /c ('sc create ' + $ServiceName + ' binPath= ' + $BinPathQuoted + ' start= auto DisplayName= "Vigil Monitoring Agent"') | Out-Null
    $ServiceAccount = "LocalSystem"
    & icacls.exe $DataDir /inheritance:r /grant "SYSTEM:(OI)(CI)(F)" /grant "Administrators:(OI)(CI)(F)" | Out-Null
    Write-Host "Mode '$AgentMode' executes tasks, so the agent runs as LocalSystem."
}
& sc.exe description $ServiceName "Vigil agent — outbound-only monitoring and managed tasks." | Out-Null
& sc.exe failure $ServiceName reset= 60 actions= restart/10000/restart/10000/restart/30000 | Out-Null

if ($env:VIGIL_TOKEN) {
    # Verify it actually reaches Running. A service that fails to start reports
    # nothing useful unless you go looking, and this installer spent its whole
    # life creating one that could never start: a PyInstaller --onefile build
    # extracts and re-executes, so the process the SCM is watching never calls
    # StartServiceCtrlDispatcher and the start times out with error 1053.
    Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    $state = (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status
    if ($state -ne "Running") {
        Write-Host ""
        Write-Host "ERROR: the service was installed but did not start (state: $state)."
        Write-Host "Check the System event log for Service Control Manager errors."
        Write-Host "A 1053 timeout here means the agent build cannot host a Windows"
        Write-Host "service — it must be a --onedir build, not --onefile."
        exit 1
    }
    Write-Host ""
    Write-Host "Vigil agent installed and started as $ServiceAccount."
    Write-Host "Approve this host in Vigil Settings > Enrollment Queue."
} else {
    Write-Host ""
    Write-Host "Vigil agent installed."
    Write-Host "  1. Start-Service $ServiceName"
    Write-Host "  2. Approve the host in Vigil Settings > Enrollment Queue"
}
{% endautoescape %}
