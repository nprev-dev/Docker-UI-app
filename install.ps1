# Installs Rack Dashboard on Windows for the person running this script. It needs no administrator
# rights. (On Linux and macOS, use install.sh.)
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/nprev-dev/Docker-UI-app/main/install.ps1 | iex"
#
# Everything goes into one folder, rack-dashboard in your user folder: the program, the Python it
# runs on, your settings, what it saves, its log, and a script that removes it all again
# (double-click uninstall.cmd).
#
# The one thing outside that folder is the entry among the programs Windows starts when you sign
# in, because Windows keeps those in the registry. PATH is not changed.
#
# To pass options, run it as a block instead:
#
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/nprev-dev/Docker-UI-app/main/install.ps1))) -Lan
#
#   -Lan          let other machines on the network open the page (the default is this machine only)
#   -Port N       listen on port N (default 8787)
#   -Dir PATH     use another folder than rack-dashboard in your user folder
#   -NoStartup    install only; do not set it up to start by itself
#   -Uninstall    remove it: the start-up entry and the folder with everything in it
#   -Ref NAME     install a branch, tag or commit other than main
#
# The program is fetched with uv (https://docs.astral.sh/uv/), of which the folder gets its own copy.

param(
    [switch]$Lan,
    [int]$Port = 0,
    [string]$Dir = "",
    [switch]$NoStartup,
    [switch]$Uninstall,
    [string]$Ref = "main",
    # For trying out a copy that is not on GitHub: a folder, a built package or an address.
    [string]$Source = "",
    [switch]$Help
)

$RackDashboard = @{
    Repo = "https://github.com/nprev-dev/Docker-UI-app"
    Name = "rack-dashboard"
    # The name of the entry among the programs Windows starts at sign-in.
    Entry = "RackDashboard"
    RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    # A file of this name marks a folder as made by this script. The dashboard looks for it too
    # (rack_dashboard/paths.py), and nothing is ever deleted from a folder without it.
    Marker = ".rack-dashboard"
    # The Python the dashboard is installed with: one the tests run on, not whatever came out last.
    Python = "3.14"
}

# The script that takes everything away again. It is written into the folder, and it is also what
# -Uninstall runs, so there is one way of removing things and not two.
$RackDashboardUninstaller = @'
# Removes Rack Dashboard: the entry that starts it when you sign in, and this folder with
# everything in it (the program, its Python, your settings, the saved history and the log).
#
# Double-click uninstall.cmd beside this file, or run:
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1

param([string]$Folder = "", [switch]$Wait)

$ErrorActionPreference = "Stop"
$entry = "RackDashboard"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$marker = ".rack-dashboard"

try {
    # Told a folder, that one; otherwise the folder this script is in.
    if (-not $Folder) { $Folder = $PSScriptRoot }
    $Folder = $Folder.TrimEnd("\")
    $inside = $Folder + "\"
    $removed = $false

    $started = (Get-ItemProperty -Path $runKey -Name $entry -ErrorAction SilentlyContinue).$entry
    if ($started) {
        $program = "$started".Trim('"')
        # Ours to remove if it starts the copy in this folder, or a copy that is no longer there.
        if ($program.StartsWith($inside, [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $program)) {
            Remove-ItemProperty -Path $runKey -Name $entry
            Write-Host "Removed the start-up entry."
            $removed = $true
        }
    }

    if (-not (Test-Path -LiteralPath $Folder)) {
        if (-not $removed) { Write-Host "Rack Dashboard is not installed (no folder at $Folder)." }
    } elseif (-not (Test-Path -LiteralPath (Join-Path $Folder $marker) -PathType Leaf)) {
        throw "$Folder was not made by the Rack Dashboard installer, so it was left alone."
    } else {
        # Windows will not delete the files of a program that is running. Everything the dashboard
        # runs is inside its folder: the command, and the Python that does the work.
        $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID -and $_.Path -and $_.Path.StartsWith($inside, [StringComparison]::OrdinalIgnoreCase) })
        foreach ($process in $running) {
            try { Stop-Process -Id $process.Id -Force -ErrorAction Stop } catch { }
        }
        foreach ($process in $running) {
            try { Wait-Process -Id $process.Id -Timeout 10 -ErrorAction Stop } catch { }
        }
        # Nor a folder that something is standing in.
        if ((Get-Location).Path.StartsWith($Folder, [StringComparison]::OrdinalIgnoreCase)) { Set-Location -LiteralPath $env:USERPROFILE }
        [Environment]::CurrentDirectory = $env:TEMP
        $tries = 0
        while (Test-Path -LiteralPath $Folder) {
            try {
                Remove-Item -LiteralPath $Folder -Recurse -Force -ErrorAction Stop
            } catch {
                $tries += 1
                if ($tries -ge 5) { throw "could not remove $Folder (is something in it still open?): $($_.Exception.Message)" }
                Start-Sleep -Seconds 1
            }
        }
        Write-Host "Removed $Folder and everything in it. Nothing of Rack Dashboard is left on this machine."
    }
} catch {
    Write-Host "uninstall: $($_.Exception.Message)"
    if ($Wait) { Start-Sleep -Seconds 8 }
    throw
}
# Started by a double-click, the window would close before anyone could read the above.
if ($Wait) { Start-Sleep -Seconds 4 }
'@

# What a double-click runs. It steps out of the folder first. The "(goto)" ends this file's turn
# while the rest of the line still runs: without it, Windows would come back to read the next line
# of a file that has just been deleted, and complain.
$RackDashboardUninstallerCmd = @'
@echo off
rem Removes Rack Dashboard: the start-up entry and this folder with everything in it.
cd /d "%TEMP%" & (goto) 2>nul & powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" -Wait
'@

# Runs a program and hands back its exit code and what it printed. Windows PowerShell treats
# anything a program writes to its error stream as a failure of the script; here it is just text.
function Invoke-RackTool {
    param([string]$Program, [string[]]$Arguments)
    $ErrorActionPreference = "Continue"
    $printed = & $Program @Arguments 2>&1 | ForEach-Object { "$_" }
    return [pscustomobject]@{ Code = $LASTEXITCODE; Printed = ($printed -join "`n") }
}

# The folder the start-up entry on this machine points to, if it is one of ours.
function Get-RackInstalledFolder {
    $started = (Get-ItemProperty -Path $RackDashboard.RunKey -Name $RackDashboard.Entry -ErrorAction SilentlyContinue).($RackDashboard.Entry)
    if (-not $started) { return $null }
    $program = "$started".Trim('"')
    $tail = "\bin\$($RackDashboard.Name)w.exe"
    if (-not $program.EndsWith($tail, [StringComparison]::OrdinalIgnoreCase)) { return $null }
    $folder = $program.Substring(0, $program.Length - $tail.Length)
    if (Test-Path -LiteralPath (Join-Path $folder $RackDashboard.Marker) -PathType Leaf) { return $folder }
    return $null
}

# Stops a running dashboard: Windows will not replace the files of a program that is running.
function Stop-RackDashboard {
    param([string]$Folder)
    $inside = $Folder + "\"
    $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID -and $_.Path -and $_.Path.StartsWith($inside, [StringComparison]::OrdinalIgnoreCase) })
    foreach ($process in $running) {
        try { Stop-Process -Id $process.Id -Force -ErrorAction Stop } catch { }
    }
    foreach ($process in $running) {
        try { Wait-Process -Id $process.Id -Timeout 10 -ErrorAction Stop } catch { }
    }
    return $running.Count
}

# Sets a KEY=value line in the settings file, replacing an earlier one for the same key.
function Set-RackSetting {
    param([string]$File, [string]$Key, [string]$Value)
    $lines = @()
    if (Test-Path -LiteralPath $File) { $lines = @([IO.File]::ReadAllLines($File)) }
    $found = $false
    $lines = @($lines | ForEach-Object { if ($_.StartsWith("$Key=")) { $found = $true; "$Key=$Value" } else { $_ } })
    if (-not $found) { $lines += "$Key=$Value" }
    [IO.File]::WriteAllLines($File, [string[]]$lines)
}

function Test-RackAnswering {
    param([int]$OnPort)
    try {
        $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://127.0.0.1:$OnPort/api/snapshot"
        return $true
    } catch {
        return $false
    }
}

function Install-RackDashboard {
    $ErrorActionPreference = "Stop"
    # The progress bar of Windows PowerShell slows a download down many times over.
    $ProgressPreference = "SilentlyContinue"
    $name = $RackDashboard.Name

    if ($Help) {
        Write-Host @"
Installs Rack Dashboard for the person running it, into one folder. No administrator rights needed.

  -Lan          let other machines on the network open the page (default: this machine only)
  -Port N       listen on port N (default 8787)
  -Dir PATH     use another folder than rack-dashboard in your user folder
  -NoStartup    install only; do not set it up to start by itself
  -Uninstall    remove it: the start-up entry and the folder with everything in it
  -Ref NAME     install a branch, tag or commit other than main
"@
        return
    }
    if ($PSVersionTable.PSVersion.Major -ge 6 -and -not $IsWindows) {
        throw "this script is for Windows; on Linux and macOS use install.sh (see the readme)"
    }
    if ($Port -ne 0 -and ($Port -lt 1 -or $Port -gt 65535)) {
        throw "-Port needs a number from 1 to 65535"
    }

    # Which folder. One that the start-up entry already points to is the one meant, unless told otherwise.
    $existing = Get-RackInstalledFolder
    if ($Dir) {
        $folder = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Dir)
    } elseif ($existing) {
        $folder = $existing
    } else {
        $folder = Join-Path $env:USERPROFILE $name
    }
    $folder = $folder.TrimEnd("\")
    if (-not $folder -or $folder -eq $env:USERPROFILE.TrimEnd("\") -or $folder -eq [IO.Path]::GetPathRoot($folder).TrimEnd("\")) {
        throw "choose a folder of its own for -Dir, not $folder\"
    }

    if ($Uninstall) {
        & ([scriptblock]::Create($RackDashboardUninstaller)) -Folder $folder
        return
    }

    if ($Dir -and $existing -and ($existing -ne $folder) -and (Test-Path -LiteralPath $existing)) {
        throw "Rack Dashboard is already installed in $existing. Remove it first (uninstall.cmd in that folder), or leave out -Dir to update it."
    }
    $marker = Join-Path $folder $RackDashboard.Marker
    if ((Test-Path -LiteralPath $folder) -and -not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        if (-not (Test-Path -LiteralPath $folder -PathType Container) -or @(Get-ChildItem -LiteralPath $folder -Force).Count -gt 0) {
            throw "$folder exists and was not made by this installer. Choose another folder with -Dir."
        }
    }
    $null = New-Item -ItemType Directory -Force -Path $folder
    [IO.File]::WriteAllText($marker, "Rack Dashboard is installed in this folder. Everything it uses is in here; uninstall.cmd removes it.`r`n")
    [IO.File]::WriteAllText((Join-Path $folder "uninstall.ps1"), (($RackDashboardUninstaller -replace "`r?`n", "`r`n") + "`r`n"), [Text.Encoding]::ASCII)
    [IO.File]::WriteAllText((Join-Path $folder "uninstall.cmd"), (($RackDashboardUninstallerCmd -replace "`r?`n", "`r`n") + "`r`n"), [Text.Encoding]::ASCII)

    # Older copies of Windows PowerShell do not offer the encryption GitHub asks for unless told to.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    # uv is told through the environment where to put things, its temporary files included: those
    # go into the folder as well and are removed at the end. What was set before is put back
    # afterwards, so that nothing lingers in the PowerShell this was pasted into.
    $bin = Join-Path $folder "bin"
    $scratchFolder = Join-Path $folder ".tmp"
    Remove-Item -LiteralPath $scratchFolder -Recurse -Force -ErrorAction SilentlyContinue
    $null = New-Item -ItemType Directory -Force -Path $scratchFolder
    $told = @{
        UV_UNMANAGED_INSTALL = $bin
        UV_PYTHON_INSTALL_DIR = Join-Path $folder "python"
        UV_TOOL_DIR = Join-Path $folder "tools"
        UV_TOOL_BIN_DIR = $bin
        TMP = $scratchFolder
        TEMP = $scratchFolder
    }
    $before = @{}
    foreach ($key in $told.Keys) {
        $before[$key] = [Environment]::GetEnvironmentVariable($key, "Process")
        [Environment]::SetEnvironmentVariable($key, $told[$key], "Process")
    }
    try {
        # 1. uv, which fetches Python and the dashboard. The folder gets its own copy, put there
        #    without touching PATH.
        $uv = Join-Path $bin "uv.exe"
        if (-not (Test-Path -LiteralPath $uv)) {
            Write-Host "Fetching uv (the installer tool) from astral.sh ..."
            $scratch = Join-Path $scratchFolder "uv-install.ps1"
            try {
                try {
                    Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/install.ps1" -OutFile $scratch
                } catch {
                    throw "could not download uv from astral.sh: $($_.Exception.Message)"
                }
                $shell = (Get-Process -Id $PID).Path
                $fetched = Invoke-RackTool $shell @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $scratch)
                if ($fetched.Code -ne 0 -or -not (Test-Path -LiteralPath $uv)) {
                    throw "could not install uv; see https://docs.astral.sh/uv/getting-started/installation/`n$($fetched.Printed)"
                }
            } finally {
                Remove-Item -LiteralPath $scratch -ErrorAction SilentlyContinue
            }
        }

        # 2. Python and the dashboard itself, both kept inside the folder. Nothing is left in a cache.
        if (-not $Source) { $Source = "$name @ $($RackDashboard.Repo)/archive/$Ref.tar.gz" }
        if ((Stop-RackDashboard $folder) -gt 0) { Write-Host "Stopped the dashboard that was running." }
        Write-Host "Installing $name into $folder ..."
        $python = Invoke-RackTool $uv @("python", "install", $RackDashboard.Python, "--no-bin", "--no-registry", "--no-cache", "--quiet")
        if ($python.Code -ne 0) { throw "could not fetch Python $($RackDashboard.Python):`n$($python.Printed)" }
        $installed = Invoke-RackTool $uv @("tool", "install", "--force", "--quiet", "--no-cache", "--managed-python", "--python", $RackDashboard.Python, $Source)
        if ($installed.Code -ne 0) { throw "could not install ${name}:`n$($installed.Printed)" }
    } finally {
        foreach ($key in $told.Keys) { [Environment]::SetEnvironmentVariable($key, $before[$key], "Process") }
        Remove-Item -LiteralPath $scratchFolder -Recurse -Force -ErrorAction SilentlyContinue
    }
    $command = Join-Path $bin "$name.exe"
    # The same program without a console window, for running in the background.
    $quiet = Join-Path $bin "${name}w.exe"
    if (-not (Test-Path -LiteralPath $command) -or -not (Test-Path -LiteralPath $quiet)) {
        throw "the $name command did not appear in $bin"
    }
    Write-Host "Installed $((Invoke-RackTool $command @('--version')).Printed.Trim())"

    # 3. Settings: one file of KEY=value lines, which the dashboard reads as it starts. An earlier
    #    one is kept.
    $settings = Join-Path $folder "env"
    $log = Join-Path $folder "$name.log"
    if (-not (Test-Path -LiteralPath $settings)) {
        [IO.File]::WriteAllLines($settings, [string[]]@(
            "# Settings for Rack Dashboard, one KEY=value per line. The readme lists them all.",
            "# They take effect when the dashboard is next started."
        ))
    }
    if ($Lan) { Set-RackSetting $settings "DASH_HOST" "0.0.0.0" }
    if ($Port -ne 0) { Set-RackSetting $settings "DASH_PORT" "$Port" }
    $current = @([IO.File]::ReadAllLines($settings))
    $listening = 8787
    foreach ($line in $current) {
        if ($line -match "^DASH_PORT=(\d+)\s*$") { $listening = [int]$Matches[1] }
    }

    if ($NoStartup) {
        Write-Host "Not set up to start by itself. Start it with: $command"
        Write-Host "To remove it: double-click uninstall.cmd in $folder"
        return
    }

    # 4. Have Windows start it at sign-in, and start it now.
    if (-not (Test-Path -Path $RackDashboard.RunKey)) { $null = New-Item -Path $RackDashboard.RunKey -Force }
    Set-ItemProperty -Path $RackDashboard.RunKey -Name $RackDashboard.Entry -Value ('"{0}"' -f $quiet)
    $since = Get-Date
    Start-Process -FilePath $quiet

    # 5. Is it answering? A first start takes a while: Windows looks every new program over.
    $tries = 0
    while (-not (Test-RackAnswering $listening)) {
        $tries += 1
        if ($tries -ge 40) {
            $said = ""
            if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).LastWriteTime -ge $since.AddSeconds(-2)) {
                $said = (@(Get-Content -LiteralPath $log -Tail 20) -join "`n")
            }
            # 10048 is the number Windows gives "that address is already in use".
            if ($said -match "10048|address already in use") {
                throw "port $listening is already used by another program. Choose another: run this again with -Port N"
            }
            throw "it was started but is not answering. Look at: $log"
        }
        Start-Sleep -Seconds 1
    }
    Write-Host ""
    Write-Host "Rack Dashboard is running: http://127.0.0.1:$listening"
    if ($current -match "^DASH_HOST=0\.0\.0\.0") {
        $address = $null
        try {
            $address = @(Get-NetIPConfiguration -ErrorAction Stop | Where-Object { $_.IPv4DefaultGateway } | ForEach-Object { $_.IPv4Address.IPAddress })[0]
        } catch { }
        if ($address) { Write-Host "From other machines:       http://${address}:$listening" }
        Write-Host "Windows may ask whether to let it through the firewall; other machines cannot open the page until it is allowed."
    } else {
        Write-Host "Only this machine can open it. Run the installer again with -Lan to change that."
    }
    Write-Host "Everything is in $folder (settings: env, log: $name.log)."
    Write-Host "To remove it: double-click uninstall.cmd in that folder."
    Write-Host "It starts when you sign in to Windows (Task Manager, Startup apps, lists it)."
}

Install-RackDashboard
