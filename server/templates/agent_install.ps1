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

# Create directories (the config folder is created further down, once it is
# known not to be a link someone planted).
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# Lock the config tree (SEC-2). C:\ProgramData's default ACL lets any local user
# create files and folders in a new subfolder, and the agent runs scripts from
# $ConfigDir\scripts as LocalSystem: a standard user who created that folder
# first owned it, and every script in it. So: no inherited ACL on $ConfigDir;
# SYSTEM and Administrators full; Users may only list the folder itself, never
# read or create anything inside it. SIDs, not names: group names are localised.
$ScriptsDir = Join-Path $ConfigDir "scripts"
$LogPath    = Join-Path $ConfigDir "agent.log"
$Admins     = "*S-1-5-32-544"

# Every lock step must succeed. A failed one is not a warning: the tree would
# be left as open as before, so the install stops (fail closed).
function Invoke-AclStep {
    & icacls.exe @args | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not secure $ConfigDir (icacls $($args -join ' ') exited $LASTEXITCODE). Nothing was installed."
    }
}

function Test-Link([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return ($null -ne $item) -and [bool]($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
}

$Trusted = @("S-1-5-18", "S-1-5-32-544", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464")
# Write-type rights (the same set the agent checks in scripttrust.py).
$WriteMask = 0x2 -bor 0x4 -bor 0x10 -bor 0x100 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x40000000 -bor 0x10000000

# True when someone other than SYSTEM, Administrators or TrustedInstaller owns
# the file or may change it: the agent would refuse to run it, so the installer
# must not adopt it either.
function Test-Untrusted([string]$Path) {
    $sid = [System.Security.Principal.SecurityIdentifier]
    $acl = Get-Acl -LiteralPath $Path
    if ($Trusted -notcontains $acl.GetOwner($sid).Value) { return $true }
    foreach ($rule in $acl.GetAccessRules($true, $true, $sid)) {
        if ($rule.AccessControlType -ne 'Allow') { continue }
        if ($rule.PropagationFlags -band [System.Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
        if (($Trusted -notcontains $rule.IdentityReference.Value) -and ([int64]$rule.FileSystemRights -band $WriteMask)) { return $true }
    }
    return $false
}

# Move an untrusted file to the quarantine folder, keeping its path relative to
# the scripts folder so two files with one name cannot overwrite each other. If
# it cannot be moved it is deleted; only if that fails too does the install stop.
function Move-ToQuarantine([string]$Path) {
    $rel = $Path.Substring($ScriptsDir.Length).TrimStart('\')
    $dest = Join-Path $Quarantine $rel
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path $dest -Parent) | Out-Null
        Move-Item -LiteralPath $Path -Destination $dest -ErrorAction Stop
        Write-Host "Quarantined a script a non-administrator could change: $Path"
    } catch {
        try {
            Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
            Write-Host "Deleted a script a non-administrator could change (it could not be moved): $Path"
        } catch {
            throw "A script under $ScriptsDir is not an administrator's and can be neither moved nor deleted: $Path"
        }
    }
}

# Take every item under $Root back and make it inherit the locked parent, top
# down, one item at a time. icacls /T follows directory junctions even with /L
# (measured on the Windows VM), so it is never used. A folder is locked before
# it is listed, so nothing can be added, renamed or swapped in it behind the
# walk. Links are deleted (the link, not its target) and never entered. A file
# under scripts that a non-administrator owns or may change is quarantined
# rather than adopted: taking ownership would make it look trusted.
#
# What this cannot do: revoke a handle someone opened before the lock (Windows
# checks access when a handle is opened, not on each write), so a file added
# through one after the walk is possible. That is why the agent checks again,
# at the moment it runs a script, that the file and every folder above it are
# owned by and writable only by SYSTEM, Administrators or TrustedInstaller
# (vigil_agent/scripttrust.py): a file added that way is the user's, and is
# refused there. The installer narrows the window; the agent is the guarantee.
function Lock-Tree([string]$Root) {
    $pending = New-Object System.Collections.Stack
    $pending.Push($Root)
    while ($pending.Count -gt 0) {
        $dir = $pending.Pop()
        foreach ($child in Get-ChildItem -LiteralPath $dir -Force -ErrorAction Stop) {
            if ($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                if ($child.PSIsContainer) { [System.IO.Directory]::Delete($child.FullName) }
                else { [System.IO.File]::Delete($child.FullName) }
                Write-Host "Removed a link someone planted in the agent's folder: $($child.FullName)"
                continue
            }
            $inScripts = $child.FullName.StartsWith($ScriptsDir + '\', [System.StringComparison]::OrdinalIgnoreCase)
            if (-not $child.PSIsContainer -and $inScripts -and (Test-Untrusted $child.FullName)) {
                Move-ToQuarantine $child.FullName
                continue
            }
            Invoke-AclStep $child.FullName /setowner $Admins /L
            Invoke-AclStep $child.FullName /reset /L
            if ($child.PSIsContainer) { $pending.Push($child.FullName) }
        }
    }
}

# The folder itself may be a link a user made before the first install. Check
# before anything is created inside it, or the first folder would land in the
# link's target.
if (Test-Link $ConfigDir) {
    [System.IO.Directory]::Delete($ConfigDir)
    Write-Host "Removed a link someone planted at $ConfigDir"
}
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null

# 1. Take the top folder and lock it, so nobody else can add anything to it.
#    /L: act on a link itself, never its target.
Invoke-AclStep $ConfigDir /setowner $Admins /L
Invoke-AclStep $ConfigDir /inheritance:r /grant "*S-1-5-18:(OI)(CI)(F)" /grant "$($Admins):(OI)(CI)(F)" /grant "*S-1-5-32-545:(RX)" /L
$Quarantine = Join-Path $ConfigDir ("quarantine-" + (Get-Date -Format "yyyyMMddHHmmss"))
# 2. Everything already inside: owned by Administrators, inheriting the lock,
#    links removed. An owner can always rewrite an ACL, and icacls /grant only
#    adds, so anything a user pre-created would otherwise stay theirs. agent.yml,
#    data and the service account's grants are re-applied explicitly below.
Lock-Tree $ConfigDir
New-Item -ItemType Directory -Force -Path $DataDir    | Out-Null
New-Item -ItemType Directory -Force -Path $ScriptsDir | Out-Null
if (-not (Test-Path $LogPath)) { New-Item -ItemType File -Path $LogPath | Out-Null }

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
# Actions a managed-mode agent may run. Monitor mode ignores this list; these
# defaults are read-only, so switching to managed can hunt and inventory at once.
allowlist:
  - app_inventory
  - check_service
  - container_logs
  - hunt_content
  - hunt_file
  - hunt_package
  - hunt_port
  - hunt_process
  - hunt_registry
  - hunt_service
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
    Invoke-AclStep $ConfigPath /inheritance:r /grant "*S-1-5-18:(F)" /grant "*S-1-5-32-544:(F)" /L

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

# The server's public key arrives in this script, over the same TLS download as
# the agent binary, and goes into agent.yml (SEC-3). The agent then accepts only
# that key: no trust on first use. Replaced on every install, so re-running the
# installer is how an intended key rotation reaches an agent.
$ServerPublicKey = "{{ public_key }}"
if ($ServerPublicKey) {
    $cfgText = [System.IO.File]::ReadAllText($ConfigPath)
    if ($cfgText -match '(?m)^server_public_key:') {
        $cfgText = $cfgText -replace '(?m)^server_public_key:[^\r\n]*', "server_public_key: `"$ServerPublicKey`""
    } else {
        if ($cfgText -and -not $cfgText.EndsWith("`n")) { $cfgText += "`r`n" }
        $cfgText += "server_public_key: `"$ServerPublicKey`"`r`n"
    }
    [System.IO.File]::WriteAllText(
        $ConfigPath, $cfgText, (New-Object System.Text.UTF8Encoding($false)))
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
        Invoke-AclStep $ConfigPath /grant "$($ServiceAccount):(R)" /L
        # The service writes its own log; the locked config folder no longer
        # lets anyone but SYSTEM and Administrators create files in it.
        Invoke-AclStep $LogPath /grant "$($ServiceAccount):(M)" /L
        Invoke-AclStep $DataDir /inheritance:r /grant "*S-1-5-18:(OI)(CI)(F)" /grant "*S-1-5-32-544:(OI)(CI)(F)" /grant "$($ServiceAccount):(OI)(CI)(M)" /L
        # The whole install tree, not just the exe. A onedir build is an exe
        # plus an _internal directory of DLLs and data; granting the account
        # access to the exe alone starts a process that dies immediately
        # because it cannot load anything beside it.
        & icacls.exe $InstallDir /grant "$($ServiceAccount):(OI)(CI)(RX)" /T | Out-Null
        # CPU load and swap come from performance counters (PDH), which a virtual
        # account cannot open until it is in Performance Monitor Users (S-1-5-32-558).
        # By SID, not name: the group name is localised.
        Add-LocalGroupMember -SID S-1-5-32-558 -Member $ServiceAccount -ErrorAction SilentlyContinue
        Write-Host "Monitor mode: running the agent as the unprivileged '$ServiceAccount'."
    }
} else {
    cmd.exe /c ('sc create ' + $ServiceName + ' binPath= ' + $BinPathQuoted + ' start= auto DisplayName= "Vigil Monitoring Agent"') | Out-Null
    $ServiceAccount = "LocalSystem"
    Invoke-AclStep $DataDir /inheritance:r /grant "*S-1-5-18:(OI)(CI)(F)" /grant "*S-1-5-32-544:(OI)(CI)(F)" /L
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
