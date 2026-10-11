#!/bin/sh
# Installs Rack Dashboard for the person running this script. It needs no root and puts nothing
# outside that person's home folder.
#
#   curl -fsSL https://raw.githubusercontent.com/nprev-dev/Docker-UI-app/main/install.sh | sh
#
# To pass options, put them after "sh -s --":
#
#   --lan          let other machines on the network open the page (the default is this machine only)
#   --port N       listen on port N (default 8787)
#   --no-service   install the command only; do not set it up to start at boot
#   --uninstall    remove the service and the command (settings and saved data stay)
#   --ref NAME     install a branch, tag or commit other than main
#
# What it does: fetches uv (https://docs.astral.sh/uv/) if it is not there, which brings its own
# Python; has uv install the dashboard from GitHub; and, on Linux with systemd, registers a service
# for this user so that the dashboard starts when the machine does.

set -eu

REPO="https://github.com/nprev-dev/Docker-UI-app"
NAME="rack-dashboard"

say() { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Installs Rack Dashboard for the person running it. No root needed.

  --lan          let other machines on the network open the page (default: this machine only)
  --port N       listen on port N (default 8787)
  --no-service   install the command only; do not set it up to start at boot
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

have_user_services() {
    [ "$(uname -s)" = "Linux" ] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

uninstall() {
    config="${XDG_CONFIG_HOME:-$HOME/.config}"
    unit="$config/systemd/user/$NAME.service"
    if have_user_services && [ -f "$unit" ]; then
        systemctl --user disable --now "$NAME.service" >/dev/null 2>&1 || true
        rm -f "$unit"
        systemctl --user daemon-reload || true
        say "Removed the service."
    fi
    # Starting at boot without anyone logged in was switched on by this script; switch it back off.
    if [ -f "$config/$NAME/.linger-set-by-installer" ]; then
        loginctl disable-linger "$(id -un)" >/dev/null 2>&1 || true
        rm -f "$config/$NAME/.linger-set-by-installer"
    fi
    if uv_bin="$(find_uv)"; then
        "$uv_bin" tool uninstall "$NAME" >/dev/null 2>&1 && say "Removed the $NAME command." || say "The $NAME command was not installed."
    fi
    say "Settings in $config/$NAME and saved data in ${XDG_DATA_HOME:-$HOME/.local/share}/$NAME were left alone."
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

    # 3. Settings: one file of KEY=value lines, read by the service. An earlier one is kept.
    config="${XDG_CONFIG_HOME:-$HOME/.config}"
    settings="$config/$NAME/env"
    mkdir -p "$config/$NAME"
    if [ ! -f "$settings" ]; then
        cat > "$settings" <<EOF
# Settings for Rack Dashboard, one KEY=value per line. The readme lists them all.
# After a change: systemctl --user restart $NAME
EOF
    fi
    [ "$lan" -eq 0 ] || set_setting DASH_HOST 0.0.0.0 "$settings"
    [ -z "$port" ] || set_setting DASH_PORT "$port" "$settings"
    listening_port="$(sed -n 's/^DASH_PORT=//p' "$settings" | tail -n 1)"
    listening_port="${listening_port:-8787}"

    if [ "$service" -eq 0 ]; then
        say "Not set up as a service. Start it with: $command_path"
        say "(In a new terminal, plain \"$NAME\" works too.)"
        exit 0
    fi
    if ! have_user_services; then
        say "This system has no systemd user services, so it was not set up to start at boot."
        say "Start it with: $command_path"
        exit 0
    fi

    # 4. A service for this user.
    unit_dir="$config/systemd/user"
    mkdir -p "$unit_dir"
    cat > "$unit_dir/$NAME.service" <<EOF
[Unit]
Description=Rack Dashboard
Documentation=$REPO

[Service]
ExecStart="$command_path"
EnvironmentFile=-$settings
# The dashboard calls a few system tools (ss, nvidia-smi, upower); make sure they are found.
Environment=PATH=$bin_dir:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "$NAME.service" >/dev/null 2>&1 || fail "could not enable the service"
    systemctl --user restart "$NAME.service" || fail "could not start the service; see: journalctl --user -u $NAME"

    # Without this a user's services only run while that user is logged in.
    user="$(id -un)"
    if [ "$(loginctl show-user "$user" -p Linger 2>/dev/null)" != "Linger=yes" ]; then
        if loginctl enable-linger "$user" >/dev/null 2>&1; then
            : > "$config/$NAME/.linger-set-by-installer"
        else
            say "Note: it will start when you log in. To have it start at boot with nobody logged in, run:"
            say "  sudo loginctl enable-linger $user"
        fi
    fi

    # 5. Is it answering?
    tries=0
    until fetch "http://127.0.0.1:$listening_port/api/snapshot" >/dev/null 2>&1; do
        tries=$((tries + 1))
        if [ "$tries" -ge 20 ]; then
            if journalctl --user -u "$NAME" -n 20 --no-pager 2>/dev/null | grep -qi "address already in use"; then
                say "Port $listening_port is already used by another program. Choose another: run this again with --port N"
            else
                say "The service was started but is not answering. Look at: journalctl --user -u $NAME"
            fi
            exit 1
        fi
        sleep 1
    done
    say ""
    say "Rack Dashboard is running: http://127.0.0.1:$listening_port"
    if grep -q '^DASH_HOST=0\.0\.0\.0' "$settings"; then
        address="$(hostname -I 2>/dev/null | awk '{print $1}')"
        [ -z "$address" ] || say "From other machines:       http://$address:$listening_port"
    else
        say "Only this machine can open it. Run the installer again with --lan to change that."
    fi
    say "Settings: $settings    Logs: journalctl --user -u $NAME"
    if command -v docker >/dev/null 2>&1 && ! [ -r /var/run/docker.sock ] && ! [ -S "$HOME/.docker/desktop/docker.sock" ]; then
        say "Note: $user may not use Docker, so the containers section will be empty."
        say "  The usual fix: sudo usermod -aG docker $user   (then log out and back in)"
    fi
}

# Everything runs from here, so that a download cut short runs nothing at all.
main "$@"
