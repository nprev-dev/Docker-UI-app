# Installs Rack Dashboard on Windows for the person running this script. It needs no administrator
# rights and puts nothing outside that person's own folders. (On Linux and macOS, use install.sh.)
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/nprev-dev/Docker-UI-app/main/install.ps1 | iex"
#
# To pass options, run it as a block instead:
#
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/nprev-dev/Docker-UI-app/main/install.ps1))) -Lan
#
#   -Lan          let other machines on the network open the page (the default is this machine only)
#   -Port N       listen on port N (default 8787)
#   -NoStartup    install the command only; do not set it up to start by itself
#   -Uninstall    remove the start-up entry and the command (settings and saved data stay)
#   -Ref NAME     install a branch, tag or commit other than main
#
# What it does: fetches uv (https://docs.astral.sh/uv/) if it is not there, which brings its own
# Python; has uv install the dashboard from GitHub; and adds it to the programs Windows starts when
# this person signs in.

param(
    [switch]$Lan,
    [int]$Port = 0,
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
}

function Find-RackUv {
    $found = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Path }
    $usual = Join-Path $HOME ".local\bin\uv.exe"
    if (Test-Path -LiteralPath $usual) { return $usual }
    return $null
}

# Runs a program and hands back its exit code and what it printed. Windows PowerShell treats
# anything a program writes to its error stream as a failure of the script; here it is just text.
function Invoke-RackTool {
    param([string]$Program, [string[]]$Arguments)
    $ErrorActionPreference = "Continue"
    $printed = & $Program @Arguments 2>&1 | ForEach-Object { "$_" }
    return [pscustomobject]@{ Code = $LASTEXITCODE; Printed = ($printed -join "`n") }
}

# Stops a running dashboard: Windows will not replace the files of a program that is running.
function Stop-RackDashboard {
    $name = $RackDashboard.Name
    $running = @(Get-Process -Name $name, "${name}w" -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
    try {
        # The command is a small launcher; the work is done by a Python it starts.
        $running += @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -and $_.CommandLine.Contains($name) } | ForEach-Object { [int]$_.ProcessId })
    } catch { }
    $running = @($running | Where-Object { $_ -ne $PID } | Select-Object -Unique)
    foreach ($id in $running) {
        try { Stop-Process -Id $id -Force -ErrorAction Stop } catch { }
    }
    foreach ($id in $running) {
        try { Wait-Process -Id $id -Timeout 10 -ErrorAction Stop } catch { }
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

function Uninstall-RackDashboard {
    $name = $RackDashboard.Name
    if (Get-ItemProperty -Path $RackDashboard.RunKey -Name $RackDashboard.Entry -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $RackDashboard.RunKey -Name $RackDashboard.Entry
        Write-Host "Removed the start-up entry."
    }
    if ((Stop-RackDashboard) -gt 0) { Write-Host "Stopped the dashboard." }
    $uv = Find-RackUv
    if ($uv) {
        if ((Invoke-RackTool $uv @("tool", "uninstall", $name)).Code -eq 0) {
            Write-Host "Removed the $name command."
        } else {
            Write-Host "The $name command was not installed."
        }
    }
    Write-Host "Settings in $(Join-Path $env:APPDATA $name) and saved data in $(Join-Path $env:LOCALAPPDATA $name) were left alone."
}

function Install-RackDashboard {
    $ErrorActionPreference = "Stop"
    # The progress bar of Windows PowerShell slows a download down many times over.
    $ProgressPreference = "SilentlyContinue"
    $name = $RackDashboard.Name

    if ($Help) {
        Write-Host @"
Installs Rack Dashboard for the person running it. No administrator rights needed.

  -Lan          let other machines on the network open the page (default: this machine only)
  -Port N       listen on port N (default 8787)
  -NoStartup    install the command only; do not set it up to start by itself
  -Uninstall    remove the start-up entry and the command (settings and saved data stay)
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
    if ($Uninstall) {
        Uninstall-RackDashboard
        return
    }
    # Older copies of Windows PowerShell do not offer the encryption GitHub asks for unless told to.
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    # 1. uv, which installs the dashboard into its own corner and supplies Python if need be.
    $uv = Find-RackUv
    if (-not $uv) {
        Write-Host "Fetching uv (the installer tool) from astral.sh ..."
        $scratch = Join-Path ([IO.Path]::GetTempPath()) ("uv-install-" + [guid]::NewGuid().ToString("N") + ".ps1")
        try {
            try {
                Invoke-WebRequest -UseBasicParsing -Uri "https://astral.sh/uv/install.ps1" -OutFile $scratch
            } catch {
                throw "could not download uv from astral.sh: $($_.Exception.Message)"
            }
            $shell = (Get-Process -Id $PID).Path
            $fetched = Invoke-RackTool $shell @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $scratch)
            if ($fetched.Code -ne 0) {
                throw "could not install uv; see https://docs.astral.sh/uv/getting-started/installation/`n$($fetched.Printed)"
            }
        } finally {
            Remove-Item -LiteralPath $scratch -ErrorAction SilentlyContinue
        }
        $uv = Find-RackUv
        if (-not $uv) { throw "uv was installed but cannot be found; open a new terminal and run this again" }
    }

    # 2. The dashboard itself.
    if (-not $Source) { $Source = "$name @ $($RackDashboard.Repo)/archive/$Ref.tar.gz" }
    if ((Stop-RackDashboard) -gt 0) { Write-Host "Stopped the dashboard that was running." }
    Write-Host "Installing $name ..."
    $installed = Invoke-RackTool $uv @("tool", "install", "--force", "--quiet", $Source)
    if ($installed.Code -ne 0) { throw "could not install ${name}:`n$($installed.Printed)" }
    $folder = (Invoke-RackTool $uv @("tool", "dir", "--bin")).Printed.Trim()
    $command = Join-Path $folder "$name.exe"
    # The same program without a console window, for running in the background.
    $quiet = Join-Path $folder "${name}w.exe"
    if (-not (Test-Path -LiteralPath $command) -or -not (Test-Path -LiteralPath $quiet)) {
        throw "the $name command did not appear in $folder"
    }
    Write-Host "Installed $((Invoke-RackTool $command @('--version')).Printed.Trim()) in $folder"

    # 3. Settings: one file of KEY=value lines, which the dashboard reads as it starts. An earlier
    #    one is kept.
    $settingsFolder = Join-Path $env:APPDATA $name
    $settings = Join-Path $settingsFolder "env"
    $null = New-Item -ItemType Directory -Force -Path $settingsFolder
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
        Write-Host "(In a new terminal, plain `"$name`" works too.)"
        return
    }

    # 4. Have Windows start it at sign-in, and start it now.
    if (-not (Test-Path -Path $RackDashboard.RunKey)) { $null = New-Item -Path $RackDashboard.RunKey -Force }
    Set-ItemProperty -Path $RackDashboard.RunKey -Name $RackDashboard.Entry -Value ('"{0}"' -f $quiet)
    Start-Process -FilePath $quiet

    # 5. Is it answering? A first start takes a while: Windows looks every new program over.
    $log = Join-Path (Join-Path $env:LOCALAPPDATA $name) "$name.log"
    $tries = 0
    while (-not (Test-RackAnswering $listening)) {
        $tries += 1
        if ($tries -ge 40) {
            $said = ""
            if (Test-Path -LiteralPath $log) { $said = [IO.File]::ReadAllText($log) }
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
    Write-Host "Settings: $settings    Log: $log"
    Write-Host "It starts when you sign in to Windows (Task Manager, Startup apps, lists it)."
}

Install-RackDashboard
