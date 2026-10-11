#!/bin/sh
# Installs Rack Dashboard for the person running this script, on Linux or macOS. It needs no root
# and puts nothing outside that person's home folder. (On Windows, use install.ps1.)
#
#   curl -fsSL https://raw.githubusercontent.com/nprev-dev/Docker-UI-app/main/install.sh | sh
#
# To pass options, put them after "sh -s --":
#
#   --lan          let other machines on the network open the page (the default is this machine only)
#   --port N       listen on port N (default 8787)
#   --no-service   install the command only; do not set it up to start by itself
#   --uninstall    remove the service and the command (settings and saved data stay)
#   --ref NAME     install a branch, tag or commit other than main
#
# What it does: fetches uv (https://docs.astral.sh/uv/) if it is not there, which brings its own
# Python; has uv install the dashboard from GitHub; and registers it with the system so that it
# starts by itself: a systemd service for this user on Linux, a launch agent on macOS.

set -eu

REPO="https://github.com/nprev-dev/Docker-UI-app"
NAME="rack-dashboard"
# What macOS knows the dashboard by.
LABEL="io.github.nprev-dev.rack-dashboard"

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Installs Rack Dashboard for the person running it. No root needed.

  --lan          let other machines on the network open the page (default: this machine only)
  --port N       listen on port N (default 8787)
  --no-service   install the command only; do not set it up to start by itself
  --uninstall    remove the service and the command (settings and saved data stay)
  --ref NAME     install a branch, tag or commit other than main
EOF
}

# Sets a KEY=value line in the settings file, replacing an earlier one for the same key.
set_setting() {
    key="$1"; value="$2"; file="$3"
    if grep -q "^$key=" "$file" 2>/dev/null; then
        scratch="$file.tmp"
        sed "s|^$key=.*|$key=$value|" "$file" > "$scratch" && mv "$scratch" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
    elif [ -x "$HOME/.local/bin/uv" ]; then
        printf '%s\n' "$HOME/.local/bin/uv"
    else
        return 1
    fi
}

fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        fail "need curl or wget to download things"
    fi
}

# Where the settings and the saved data live. The dashboard works these out by the same rules
# (rack_dashboard/paths.py), so the two always look in the same place.
config_home() {
    if [ "$(uname -s)" = "Linux" ]; then
        case "${XDG_CONFIG_HOME:-}" in /*) printf '%s\n' "$XDG_CONFIG_HOME"; return ;; esac
    fi
    printf '%s\n' "$HOME/.config"
}

data_home() {
    if [ "$(uname -s)" = "Darwin" ]; then
        printf '%s\n' "$HOME/Library/Application Support"
        return
    fi
    case "${XDG_DATA_HOME:-}" in /*) printf '%s\n' "$XDG_DATA_HOME"; return ;; esac
    printf '%s\n' "$HOME/.local/share"
}

# How this system starts a program for one user: systemd, launchd, or not at all.
service_kind() {
    case "$(uname -s)" in
        Linux)
            if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
                echo systemd
                return
            fi ;;
        Darwin)
            if command -v launchctl >/dev/null 2>&1; then
                echo launchd
                return
            fi ;;
    esac
    echo none
}

# Text as it may stand inside a launch agent's file.
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

stop_launchd() {
    domain="gui/$(id -u)"
    launchctl bootout "$domain/$LABEL" >/dev/null 2>&1 || return 0
    # Unloading takes a moment, and loading the agent again before it is done fails.
    waited=0
    while launchctl print "$domain/$LABEL" >/dev/null 2>&1 && [ "$waited" -lt 10 ]; do
        sleep 1
        waited=$((waited + 1))
    done
}

start_launchd() {
    agents="$HOME/Library/LaunchAgents"
    plist="$agents/$LABEL.plist"
    mkdir -p "$agents" "$HOME/Library/Logs"
    stop_launchd
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(xml "$command_path")</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(xml "$bin_dir"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>$(xml "$log")</string>
    <key>StandardErrorPath</key>
    <string>$(xml "$log")</string>
</dict>
</plist>
EOF
    # Someone who switched it off by hand earlier is asking for it back by installing again.
    launchctl enable "$domain/$LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "$domain" "$plist" >/dev/null 2>&1 && return 0
    sleep 2
    launchctl bootstrap "$domain" "$plist" >/dev/null 2>&1
}

start_systemd() {
    unit_dir="$config/systemd/user"
    mkdir -p "$unit_dir"
    # A service does not see what a terminal has set; if the folders were moved there, say so here
    # too, so that the service reads the same settings and saves to the same place.
    moved=""
    case "${XDG_CONFIG_HOME:-}" in /*) moved="${moved}Environment=\"XDG_CONFIG_HOME=$XDG_CONFIG_HOME\"
" ;; esac
    case "${XDG_DATA_HOME:-}" in /*) moved="${moved}Environment=\"XDG_DATA_HOME=$XDG_DATA_HOME\"
" ;; esac
    cat > "$unit_dir/$NAME.service" <<EOF
[Unit]
Description=Rack Dashboard
Documentation=$REPO

[Service]
ExecStart="$command_path"
# The dashboard calls a few system tools (ss, nvidia-smi, upower); make sure they are found.
Environment="PATH=$bin_dir:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
${moved}Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "$NAME.service" >/dev/null 2>&1 || fail "could not enable the service"
    systemctl --user restart "$NAME.service" || fail "could not start the service; see: journalctl --user -u $NAME"

    # Without this a user's services only run while that user is logged in.
    if [ "$(loginctl show-user "$user" -p Linger 2>/dev/null)" != "Linger=yes" ]; then
        if loginctl enable-linger "$user" >/dev/null 2>&1; then
            : > "$config/$NAME/.linger-set-by-installer"
        else
            say "Note: it will start when you log in. To have it start at boot with nobody logged in, run:"
            say "  sudo loginctl enable-linger $user"
        fi
    fi
}

# Did the last start fail because another program holds the port?
port_taken() {
    if [ "$kind" = "systemd" ]; then
        journalctl --user -u "$NAME" -n 20 --no-pager 2>/dev/null | grep -qi "address already in use"
    else
        tail -n 20 "$log" 2>/dev/null | grep -qi "address already in use"
    fi
}

uninstall() {
    config="$(config_home)"
    kind="$(service_kind)"
    unit="$config/systemd/user/$NAME.service"
    plist="$HOME/Library/LaunchAgents/$LABEL.plist"
    if [ "$kind" = "systemd" ] && [ -f "$unit" ]; then
        systemctl --user disable --now "$NAME.service" >/dev/null 2>&1 || true
        rm -f "$unit"
        systemctl --user daemon-reload || true
        say "Removed the service."
    fi
    if [ "$kind" = "launchd" ] && [ -f "$plist" ]; then
        stop_launchd
        rm -f "$plist"
        say "Removed the launch agent."
    fi
    # Starting at boot without anyone logged in was switched on by this script; switch it back off.
    if [ -f "$config/$NAME/.linger-set-by-installer" ]; then
        loginctl disable-linger "$(id -un)" >/dev/null 2>&1 || true
        rm -f "$config/$NAME/.linger-set-by-installer"
    fi
    if uv_bin="$(find_uv)"; then
        "$uv_bin" tool uninstall "$NAME" >/dev/null 2>&1 && say "Removed the $NAME command." || say "The $NAME command was not installed."
    fi
    say "Settings in $config/$NAME and saved data in $(data_home)/$NAME were left alone."
}

main() {
    lan=0; port=""; service=1; ref="main"; source=""; remove=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --lan) lan=1 ;;
            --port) [ $# -ge 2 ] || fail "--port needs a number"; port="$2"; shift ;;
            --no-service) service=0 ;;
            --uninstall) remove=1 ;;
            --ref) [ $# -ge 2 ] || fail "--ref needs a name"; ref="$2"; shift ;;
            # For trying out a copy that is not on GitHub: a folder, a built package or an address.
            --source) [ $# -ge 2 ] || fail "--source needs a path or address"; source="$2"; shift ;;
            -h|--help) usage; exit 0 ;;
            *) fail "unknown option: $1 (see --help)" ;;
        esac
        shift
    done
    case "$port" in
        ""|*[!0-9]*) [ -z "$port" ] || fail "--port needs a number from 1 to 65535" ;;
    esac
    if [ -n "$port" ] && { [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; }; then
        fail "--port needs a number from 1 to 65535"
    fi

    system="$(uname -s)"
    case "$system" in
        Linux|Darwin) ;;
        *) fail "this script is for Linux and macOS; on Windows use install.ps1 (see the readme)" ;;
    esac
    [ -n "${HOME:-}" ] || fail "HOME is not set"

    if [ "$remove" -eq 1 ]; then
        uninstall
        exit 0
    fi

    # 1. uv, which installs the dashboard into its own corner and supplies Python if need be.
    if ! uv_bin="$(find_uv)"; then
        say "Fetching uv (the installer tool) from astral.sh ..."
        scratch="$(mktemp)"
        fetch https://astral.sh/uv/install.sh > "$scratch" || { rm -f "$scratch"; fail "could not download uv from astral.sh"; }
        sh "$scratch" >/dev/null 2>&1 || { rm -f "$scratch"; fail "could not install uv; see https://docs.astral.sh/uv/getting-started/installation/"; }
        rm -f "$scratch"
        uv_bin="$(find_uv)" || fail "uv was installed but cannot be found; open a new terminal and run this again"
    fi

    # 2. The dashboard itself.
    [ -n "$source" ] || source="$NAME @ $REPO/archive/$ref.tar.gz"
    say "Installing $NAME ..."
    "$uv_bin" tool install --force --quiet "$source" || fail "could not install $NAME"
    bin_dir="$("$uv_bin" tool dir --bin)"
    command_path="$bin_dir/$NAME"
    [ -x "$command_path" ] || fail "the $NAME command did not appear in $bin_dir"
    say "Installed $("$command_path" --version) in $bin_dir"

    # 3. Settings: one file of KEY=value lines, which the dashboard reads as it starts. An earlier
    #    one is kept.
    config="$(config_home)"
    settings="$config/$NAME/env"
    mkdir -p "$config/$NAME"
    if [ ! -f "$settings" ]; then
        cat > "$settings" <<EOF
# Settings for Rack Dashboard, one KEY=value per line. The readme lists them all.
# They take effect when the dashboard is next started.
EOF
    fi
    [ "$lan" -eq 0 ] || set_setting DASH_HOST 0.0.0.0 "$settings"
    [ -z "$port" ] || set_setting DASH_PORT "$port" "$settings"
    listening_port="$(sed -n 's/^DASH_PORT=//p' "$settings" | tail -n 1)"
    listening_port="${listening_port:-8787}"

    if [ "$service" -eq 0 ]; then
        say "Not set up to start by itself. Start it with: $command_path"
        say "(In a new terminal, plain \"$NAME\" works too.)"
        exit 0
    fi

    # 4. Have the system start it: now, and every time from here on.
    kind="$(service_kind)"
    user="$(id -un)"
    case "$kind" in
        systemd)
            start_systemd
            logs="journalctl --user -u $NAME" ;;
        launchd)
            log="$HOME/Library/Logs/$NAME.log"
            logs="$log"
            if ! start_launchd; then
                say "macOS would not take it on as a launch agent (is this a remote session with nobody at the desk?)."
                say "It is set to start at the next login. To start it now: $command_path"
                exit 0
            fi ;;
        *)
            say "This system has no systemd user services, so it was not set up to start by itself."
            say "Start it with: $command_path"
            exit 0 ;;
    esac

    # 5. Is it answering?
    tries=0
    until fetch "http://127.0.0.1:$listening_port/api/snapshot" >/dev/null 2>&1; do
        tries=$((tries + 1))
        if [ "$tries" -ge 20 ]; then
            if port_taken; then
                say "Port $listening_port is already used by another program. Choose another: run this again with --port N"
            else
                say "It was started but is not answering. Look at: $logs"
            fi
            exit 1
        fi
        sleep 1
    done
    say ""
    say "Rack Dashboard is running: http://127.0.0.1:$listening_port"
    if grep -q '^DASH_HOST=0\.0\.0\.0' "$settings"; then
        if [ "$system" = "Darwin" ]; then
            address="$(ipconfig getifaddr "$(route -n get default 2>/dev/null | awk '/interface:/ {print $2}')" 2>/dev/null || true)"
        else
            address="$(hostname -I 2>/dev/null | awk '{print $1}')"
        fi
        [ -z "$address" ] || say "From other machines:       http://$address:$listening_port"
    else
        say "Only this machine can open it. Run the installer again with --lan to change that."
    fi
    say "Settings: $settings    Logs: $logs"
    if [ "$kind" = "launchd" ]; then
        say "It starts when you log in to this Mac."
    fi
    if [ "$system" = "Linux" ] && command -v docker >/dev/null 2>&1 && ! [ -r /var/run/docker.sock ] && ! [ -S "$HOME/.docker/desktop/docker.sock" ]; then
        say "Note: $user may not use Docker, so the containers section will be empty."
        say "  The usual fix: sudo usermod -aG docker $user   (then log out and back in)"
    fi
}

# Everything runs from here, so that a download cut short runs nothing at all.
main "$@"
