#!/bin/sh
# Installs Rack Dashboard for the person running this script, on Linux or macOS. It needs no root.
# (On Windows, use install.ps1.)
#
#   curl -fsSL https://raw.githubusercontent.com/nprev-dev/Docker-UI-app/main/install.sh | sh
#
# Everything goes into one folder, ~/rack-dashboard: the program, the Python it runs on, your
# settings, what it saves, its log, and a script that removes it all again:
#
#   sh ~/rack-dashboard/uninstall.sh
#
# The one thing outside that folder is the entry that tells the system to start the dashboard,
# because each system keeps those in a place of its own: a systemd service for this user on Linux,
# a launch agent on macOS. Nothing is added to PATH and no shell file is touched.
#
# To pass options, put them after "sh -s --":
#
#   --lan          let other machines on the network open the page (the default is this machine only)
#   --port N       listen on port N (default 8787)
#   --dir PATH     use another folder than ~/rack-dashboard
#   --no-service   install only; do not set it up to start by itself
#   --uninstall    remove it: the start-up entry and the folder with everything in it
#   --ref NAME     install a branch, tag or commit other than main
#
# The program is fetched with uv (https://docs.astral.sh/uv/), of which the folder gets its own copy.

set -eu

REPO="https://github.com/nprev-dev/Docker-UI-app"
NAME="rack-dashboard"
# What macOS knows the dashboard by.
LABEL="io.github.nprev-dev.rack-dashboard"
# A file of this name marks a folder as made by this script. The dashboard looks for it too
# (rack_dashboard/paths.py), and nothing is ever deleted from a folder without it.
MARKER=".rack-dashboard"
# The Python the dashboard is installed with: one the tests run on, not whatever came out last.
PYTHON="3.14"

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Installs Rack Dashboard for the person running it, into one folder. No root needed.

  --lan          let other machines on the network open the page (default: this machine only)
  --port N       listen on port N (default 8787)
  --dir PATH     use another folder than ~/rack-dashboard
  --no-service   install only; do not set it up to start by itself
  --uninstall    remove it: the start-up entry and the folder with everything in it
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

fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        fail "need curl or wget to download things"
    fi
}

# The folder's path gets written into the start-up entry and the uninstall script. These
# characters mean something there, so a path holding one of them is turned down.
plain_path() {
    case "$1" in
        *[\"\\\$%\`\<\>\&]*) return 1 ;;
        *"
"*) return 1 ;;
    esac
    return 0
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

# Where the start-up entry is kept. systemd looks in the user's settings folder.
unit_path() {
    case "${XDG_CONFIG_HOME:-}" in
        /*) printf '%s\n' "$XDG_CONFIG_HOME/systemd/user/$NAME.service" ;;
        *) printf '%s\n' "$HOME/.config/systemd/user/$NAME.service" ;;
    esac
}

plist_path() { printf '%s\n' "$HOME/Library/LaunchAgents/$LABEL.plist"; }

# The folder a start-up entry on this machine points to, if there is one.
installed_dir() {
    if [ -f "$unit" ]; then
        sed -n 's/^Environment="DASH_HOME=\(.*\)"$/\1/p' "$unit" | head -n 1
    elif [ -f "$plist" ]; then
        sed -n '/<key>DASH_HOME<\/key>/{n;s/^ *<string>\(.*\)<\/string>$/\1/p;}' "$plist" | head -n 1
    fi
}

# The script that takes everything away again. It is written into the folder, and it is also what
# --uninstall runs, so there is one way of removing things and not two.
write_uninstaller() {
    {
        printf '%s\n' '#!/bin/sh' \
            '# Removes Rack Dashboard: the entry that starts it, and this folder with everything in it' \
            '# (the program, its Python, your settings, the saved history and the log).' \
            '#' \
            '#   sh uninstall.sh' \
            ''
        printf 'UNIT="%s"\n' "$unit"
        printf 'PLIST="%s"\n' "$plist"
        printf 'LABEL="%s"\n' "$LABEL"
        printf 'NAME="%s"\n' "$NAME"
        printf 'MARKER="%s"\n' "$MARKER"
        cat <<'EOF'

set -eu

main() {
    # Told a folder, that one; otherwise the folder this script is in.
    if [ $# -ge 1 ]; then
        folder="$1"
    else
        folder="$(cd "$(dirname "$0")" && pwd)"
    fi

    removed=0
    if [ -f "$UNIT" ]; then
        # Stopping waits until the dashboard has really gone.
        systemctl --user disable --now "$NAME.service" >/dev/null 2>&1 || true
        rm -f "$UNIT"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        systemctl --user reset-failed "$NAME.service" >/dev/null 2>&1 || true
        # Folders the installer may have made for it. They only go if nothing else is in them.
        rmdir "$(dirname "$UNIT")/default.target.wants" "$(dirname "$UNIT")" "$(dirname "$(dirname "$UNIT")")" 2>/dev/null || true
        echo "Removed the service."
        removed=1
    fi
    if [ -f "$PLIST" ]; then
        domain="gui/$(id -u)"
        if launchctl bootout "$domain/$LABEL" >/dev/null 2>&1; then
            waited=0
            while launchctl print "$domain/$LABEL" >/dev/null 2>&1 && [ "$waited" -lt 10 ]; do
                sleep 1
                waited=$((waited + 1))
            done
        fi
        rm -f "$PLIST"
        rmdir "$(dirname "$PLIST")" 2>/dev/null || true
        echo "Removed the launch agent."
        removed=1
    fi

    if [ ! -d "$folder" ]; then
        [ "$removed" -eq 1 ] || echo "Rack Dashboard is not installed (no folder at $folder)."
        return 0
    fi
    if [ ! -f "$folder/$MARKER" ]; then
        echo "uninstall.sh: $folder was not made by the Rack Dashboard installer, so it was left alone." >&2
        return 1
    fi
    # Starting at boot with nobody logged in was switched on by the installer; switch it back off.
    if [ -f "$folder/.linger-set-by-installer" ]; then
        loginctl disable-linger "$(id -un)" >/dev/null 2>&1 || true
    fi
    # Not from inside the folder that is about to go.
    cd /
    rm -rf "$folder"
    echo "Removed $folder and everything in it. Nothing of Rack Dashboard is left on this machine."
}

# Everything runs from here, so the script is read to its end before it deletes itself.
main "$@"
EOF
    } > "$1"
}

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
    mkdir -p "$(dirname "$plist")"
    stop_launchd
    # The dashboard is started through a line of shell that first looks whether it is still there.
    # Someone who simply deleted the folder then gets nothing started, not an agent failing forever.
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>test -x "\$0" || exit 0; exec "\$0"</string>
        <string>$dir/bin/$NAME</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DASH_HOME</key>
        <string>$dir</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
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
    mkdir -p "$(dirname "$unit")"
    cat > "$unit" <<EOF
[Unit]
Description=Rack Dashboard
Documentation=$REPO
# Someone who simply deleted the folder gets nothing started, not a service failing forever.
ConditionPathExists=$dir/bin/$NAME

[Service]
ExecStart="$dir/bin/$NAME"
Environment="DASH_HOME=$dir"
# The dashboard calls a few system tools (ss, nvidia-smi, upower); make sure they are found.
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Restart=on-failure
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
            : > "$dir/.linger-set-by-installer"
        else
            say "Note: it will start when you log in. To have it start at boot with nobody logged in, run:"
            say "  sudo loginctl enable-linger $user"
        fi
    fi
}

main() {
    lan=0; port=""; service=1; ref="main"; source=""; remove=0; dir=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --lan) lan=1 ;;
            --port) [ $# -ge 2 ] || fail "--port needs a number"; port="$2"; shift ;;
            --dir) [ $# -ge 2 ] && [ -n "$2" ] || fail "--dir needs a folder"; dir="$2"; shift ;;
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

    unit="$(unit_path)"
    plist="$(plist_path)"
    kind="$(service_kind)"
    user="$(id -un)"
    plain_path "$unit" && plain_path "$plist" || fail "the path of your home folder holds a character this script cannot work with (one of \" \\ \$ % \` < > &)"

    # Which folder. One that a start-up entry already points to is the one meant, unless told otherwise.
    existing="$(installed_dir)"
    chosen="$dir"
    [ -n "$dir" ] || dir="${existing:-$HOME/$NAME}"
    case "$dir" in
        /*) ;;
        *) dir="$PWD/$dir" ;;
    esac
    # No slash at the end, and never the root or the home folder itself.
    while [ "${dir%/}" != "$dir" ]; do dir="${dir%/}"; done
    [ -n "$dir" ] && [ "$dir" != "$HOME" ] || fail "choose a folder of its own for --dir, not $dir/"
    plain_path "$dir" || fail "choose a folder whose path has none of these characters: \" \\ \$ % \` < > &"

    if [ "$remove" -eq 1 ]; then
        scratch="$(mktemp)"
        write_uninstaller "$scratch"
        code=0
        sh "$scratch" "$dir" || code=$?
        rm -f "$scratch"
        exit "$code"
    fi

    if [ -n "$chosen" ] && [ -n "$existing" ] && [ "$existing" != "$dir" ] && [ -d "$existing" ]; then
        fail "Rack Dashboard is already installed in $existing. Remove it first (sh \"$existing/uninstall.sh\"), or leave out --dir to update it."
    fi
    if [ -e "$dir" ] && [ ! -f "$dir/$MARKER" ]; then
        if [ ! -d "$dir" ] || [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
            fail "$dir exists and was not made by this installer. Choose another folder with --dir."
        fi
    fi
    mkdir -p "$dir" || fail "could not create $dir"
    printf '%s\n' "Rack Dashboard is installed in this folder. Everything it uses is in here; uninstall.sh removes it." > "$dir/$MARKER"
    write_uninstaller "$dir/uninstall.sh"
    chmod +x "$dir/uninstall.sh"

    # 1. uv, which fetches Python and the dashboard. The folder gets its own copy, put there
    #    without touching PATH or any shell file.
    uv_bin="$dir/bin/uv"
    if [ ! -x "$uv_bin" ]; then
        say "Fetching uv (the installer tool) from astral.sh ..."
        scratch="$(mktemp)"
        fetch https://astral.sh/uv/install.sh > "$scratch" || { rm -f "$scratch"; fail "could not download uv from astral.sh"; }
        UV_UNMANAGED_INSTALL="$dir/bin" sh "$scratch" >/dev/null 2>&1 || { rm -f "$scratch"; fail "could not install uv; see https://docs.astral.sh/uv/getting-started/installation/"; }
        rm -f "$scratch"
        [ -x "$uv_bin" ] || fail "uv did not appear in $dir/bin"
    fi

    # 2. Python and the dashboard itself, both kept inside the folder. Nothing is left in a cache.
    export UV_PYTHON_INSTALL_DIR="$dir/python" UV_TOOL_DIR="$dir/tools" UV_TOOL_BIN_DIR="$dir/bin"
    [ -n "$source" ] || source="$NAME @ $REPO/archive/$ref.tar.gz"
    say "Installing $NAME into $dir ..."
    "$uv_bin" python install "$PYTHON" --no-bin --no-registry --no-cache --quiet || fail "could not fetch Python $PYTHON"
    "$uv_bin" tool install --force --quiet --no-cache --managed-python --python "$PYTHON" "$source" || fail "could not install $NAME"
    command_path="$dir/bin/$NAME"
    [ -x "$command_path" ] || fail "the $NAME command did not appear in $dir/bin"
    say "Installed $("$command_path" --version)"

    # 3. Settings: one file of KEY=value lines, which the dashboard reads as it starts. An earlier
    #    one is kept.
    settings="$dir/env"
    log="$dir/$NAME.log"
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
        say "To remove it: sh \"$dir/uninstall.sh\""
        exit 0
    fi

    # 4. Have the system start it: now, and every time from here on.
    case "$kind" in
        systemd) start_systemd ;;
        launchd)
            if ! start_launchd; then
                say "macOS would not take it on as a launch agent (is this a remote session with nobody at the desk?)."
                say "It is set to start at the next login. To start it now: $command_path"
                exit 0
            fi ;;
        *)
            say "This system has no systemd user services, so it was not set up to start by itself."
            say "Start it with: $command_path"
            say "To remove it: sh \"$dir/uninstall.sh\""
            exit 0 ;;
    esac

    # 5. Is it answering?
    tries=0
    until fetch "http://127.0.0.1:$listening_port/api/snapshot" >/dev/null 2>&1; do
        tries=$((tries + 1))
        if [ "$tries" -ge 20 ]; then
            if tail -n 20 "$log" 2>/dev/null | grep -qi "address already in use"; then
                say "Port $listening_port is already used by another program. Choose another: run this again with --port N"
            else
                say "It was started but is not answering. Look at: $log"
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
    say "Everything is in $dir (settings: env, log: $NAME.log)."
    say "To remove it: sh \"$dir/uninstall.sh\""
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
