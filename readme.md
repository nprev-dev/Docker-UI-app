# Rack Dashboard

A one-page status screen for a home server, built to sit full screen on a small monitor in the rack. It shows the Docker containers, network, power and hardware of the machine it runs on and refreshes every second.
No this dashboard doesn't fix anything. But clueless relatives might call you a cybercriminal from now on.

![The dashboard at 1920x1080](docs/overview.png)

The backend is Python (FastAPI) and reads everything from the local machine. The page is plain HTML, CSS and JavaScript with no build step. It runs on Linux, Windows and macOS. Linux is where it can read the most; [what the other two leave empty](#linux-windows-and-macos) is listed further down.

The screenshots here were taken with made-up containers and addresses.

## What is on the screen

The top row holds the numbers worth reading from across the room: host and uptime, CPU load and temperature, power draw, network in and out, containers running, internet latency and the clock.

On the left, the server is drawn as a globe with a line to everything it is talking to. Containers sit on one side and network peers on the other. A thicker, brighter bundle of lines means more traffic (log scale, 1 Kbps to 1 Gbps), and the dashes travel the way most of the data is going. Things on the inner dotted orbit are on the local network, things on the outer one are out on the internet. The five busiest or most troubled things on each side get a label, and a corner readout gives the totals.

The field of faint loops behind all of this carries no data of its own. It only follows CPU load: the harder the machine works, the brighter it gets and the faster it turns.

![The centrepiece](docs/centrepiece.png)

The rest of the page is detail:

| Section | Shows |
| --- | --- |
| Throughput | Receive and transmit rate of the main interface with recent history and peaks, plus totals for today and since boot |
| Link | Interface state, speed, address, gateway, error and drop counters, socket counts |
| Top talkers | The remote hosts moving the most data, taken from the kernel's per-connection byte counters (TCP only) |
| Latency / WAN | Ping to the gateway and to an outside address, DNS lookup time, public IP and how long it has been held, last speed test |
| Listening | Open ports, what owns them, and whether they can be reached from the network or only from the machine itself |
| Power | CPU and GPU watts, an estimate of the draw at the wall, energy today and this month, and the cost if a price is set |
| GPU | Load and memory of each NVIDIA card with a few minutes of history, and how many programs are using it. With several cards the first two get a line each |
| Temperature, Fans / volts | Whatever `hwmon` and `nvidia-smi` report |
| Hardware | Board, BIOS, CPU, memory, GPU, disks |
| UPS / room | A USB UPS known to `upower`, and an optional room temperature sensor |
| Containers | State, image, ports, CPU, memory and network rate for each container. There is room for nine; past that, the uneventful ones are summed up in a last line |
| Event log | What changed: containers stopping, ports opening, pings going unanswered, temperature limits crossed, the UPS switching to battery |

The Agents section is a placeholder and shows nothing yet.

## When something is wrong

Colour is kept for trouble. A healthy system is blue and white only. Amber and red appear when something needs attention, and they appear in every place that thing is shown, so an unhealthy container turns red in the top row, in the centrepiece, in the table and in the log at once.

![The same screen with a UPS on battery, an unhealthy container and no reply from the internet](docs/alerts.png)

If the page loses its connection to the backend, every reading is dimmed so that old numbers cannot pass for live ones.

## Install

Run this on the machine you want to watch, as the user who should run the dashboard. No root or administrator rights are needed, and everything stays in your own folders.

Linux and macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/nprev-dev/Docker-UI-app/main/install.sh | sh
```

Windows, in PowerShell or the command prompt:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/nprev-dev/Docker-UI-app/main/install.ps1 | iex"
```

Then open <http://127.0.0.1:8787> on that machine.

The script does three things:

- It fetches [uv](https://docs.astral.sh/uv/) from astral.sh if you do not have it. uv brings its own Python, so none has to be installed, and its installer adds the folder it uses (`~/.local/bin`) to your `PATH`.
- It has uv install the dashboard from this repository, which gives you a `rack-dashboard` command.
- It has the system start the dashboard by itself from then on:
  - **Linux** (with systemd): a service for your user, which starts when the machine boots and comes back if it crashes. For that the script switches on "lingering" for your user (`loginctl enable-linger`), which is what lets a user's services run with nobody logged in.
  - **macOS**: a launch agent in `~/Library/LaunchAgents`, which starts when you log in and comes back if it crashes.
  - **Windows**: an entry among your start-up programs (Task Manager lists it under Startup apps), which starts when you sign in. It runs without a window.

On macOS and Windows the dashboard starts at login, not at boot. A machine that should come back on its own after a power cut needs automatic login switched on.

### Opening it from other machines

By default only the machine itself can open the page. To let the rest of your network see it, install with the `lan` option:

```bash
curl -fsSL https://raw.githubusercontent.com/nprev-dev/Docker-UI-app/main/install.sh | sh -s -- --lan
```

```powershell
powershell -ExecutionPolicy ByPass -c "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/nprev-dev/Docker-UI-app/main/install.ps1))) -Lan"
```

There is no login. Anyone who can reach the machine then sees your open ports, addresses and container names, so use this only on a network you trust. On Windows the firewall will ask whether to allow it the first time; until you say yes, other machines get no answer.

### Other options, updating, removing

`--port N` (Windows: `-Port N`) listens on another port. `--no-service` (Windows: `-NoStartup`) installs the command and leaves starting it to you. Options go where `--lan` and `-Lan` are in the commands above.

Running the installer again updates to the newest version and keeps your settings. `--uninstall` (Windows: `-Uninstall`) removes the dashboard and its start-up entry. Your settings and saved data are left where they are; the script says where.

### On a monitor in the rack

Start the browser in kiosk mode, for example `firefox --kiosk http://127.0.0.1:8787`. The layout is made for a 16:9 screen. Text size follows the window, and the page has been checked from 1024x768 up to 4K.

## Requirements

- Linux, Windows 10 or 11, or macOS. It is developed on Ubuntu 26.04.
- Docker, if you want the containers section. Without it the page still runs and says so.
  - Linux: the socket has to be usable by the user that runs the dashboard, which on most systems means being in the `docker` group. Docker Engine at `/var/run/docker.sock` is tried first, then Docker Desktop.
  - Windows and macOS: Docker Desktop, running.
- Linux only: `ss` from iproute2, which practically every distribution ships. Optional: `upower` for a UPS.
- Optional: `nvidia-smi` for GPU readings. It comes with the NVIDIA driver.
- A current Firefox or Chromium-based browser to look at it.

On Linux, pings use unprivileged ICMP sockets. Ubuntu allows these by default; elsewhere check `net.ipv4.ping_group_range`.

## Linux, Windows and macOS

Windows and macOS do not let an ordinary program read everything Linux does. Where a reading cannot be had, its section says so and the rest of the page carries on.

| | Linux | Windows | macOS |
| --- | --- | --- | --- |
| Containers | yes | yes | yes |
| Throughput, link, listening ports | yes | yes | yes |
| Latency, DNS, public IP, speed test | yes | yes | yes |
| Top talkers | yes | no | yes |
| Hardware names | yes | yes | yes |
| CPU power | measured, or estimated | estimated | estimated |
| NVIDIA GPU | yes | yes | no |
| Temperatures | yes | GPU only | no |
| Fans and voltages | yes, with the board's driver | GPU fan only | no |
| UPS | a USB UPS, through `upower` | the machine's own battery | the machine's own battery |

So far Windows and macOS have only been run on GitHub's test machines, which are virtual and have neither a graphics card nor a battery. Everything else in those two columns was seen working there. The GPU and battery rows are how the code is written, and nobody has watched them work yet. If you try it on a real Windows PC or Mac, an issue saying what showed up and what did not is welcome.

## Settings

Settings live in one file, one `KEY=value` per line. The installer creates it and never overwrites it.

| | Settings file | Restart after a change |
| --- | --- | --- |
| Linux | `~/.config/rack-dashboard/env` | `systemctl --user restart rack-dashboard` |
| macOS | `~/.config/rack-dashboard/env` | `launchctl kickstart -k gui/$(id -u)/io.github.nprev-dev.rack-dashboard` |
| Windows | `%APPDATA%\rack-dashboard\env` | run the installer again |

The same names work as environment variables, and one set that way wins over the file. Started by hand, that looks like `DASH_KWH_PRICE=0.14 rack-dashboard`. The address and port can also be given as `--host` and `--port`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DASH_HOST` | `127.0.0.1` | Address the server listens on |
| `DASH_PORT` | `8787` | Port |
| `DASH_INTERVAL` | `1` | Seconds between updates |
| `DOCKER_HOST` | unset | Docker socket to use. Unset, it tries the places Docker Engine and Docker Desktop use on that system |
| `DASH_IFACE` | unset | Network interface to watch. Unset, the one holding the default route |
| `DASH_PING_TARGET` | `1.1.1.1` | Outside address to ping |
| `DASH_PING_INTERVAL` | `30` | Seconds between those pings |
| `DASH_DNS_NAME` | `example.com` | Name looked up to time DNS |
| `DASH_SPEEDTEST_HOURS` | `6` | Hours between speed tests, `0` for none |
| `DASH_PROBES` | `1` | `0` stops the dashboard from sending anything onto the network |
| `DASH_KWH_PRICE` | unset | Price of a kilowatt-hour. Set it to get a monthly cost |
| `DASH_CURRENCY` | `$` | Symbol shown with the cost |
| `DASH_POWER_BASE_W` | unset | Watts for everything that has no power sensor. Unset, a built-in allowance is used |
| `DASH_PSU_EFFICIENCY` | `0.87` | Power supply efficiency used for the wall figure |
| `DASH_CPU_IDLE_W`, `DASH_CPU_MAX_W` | `25`, `142` | CPU power at idle and at full load, used only while it cannot be measured. The defaults suit a desktop processor; see [the power figures](#about-the-power-figures) |
| `DASH_ROOM_SENSOR` | unset | Path of a file holding a temperature, as 1-wire and USB sensors provide |
| `DASH_STATE` | see below | Where totals and history are kept between restarts |

## What it sends over the network

Everything on the page is read from the machine itself, except for the checks below. With the default settings the dashboard sends:

- a ping to the default gateway every second
- a ping to `DASH_PING_TARGET` every 30 seconds
- a DNS lookup of `DASH_DNS_NAME` every 5 seconds, to the resolvers the system already uses
- one HTTPS request to `1.1.1.1/cdn-cgi/trace` every 10 minutes, to learn the public IP
- a Cloudflare speed test every 6 hours, which moves up to about 75 MB each time

`DASH_PROBES=0` turns all of it off, and `DASH_SPEEDTEST_HOURS=0` turns off only the speed test.

The state file holds the daily totals, speed test history, the event log and your public IP address. It never leaves the machine. It is `state.json` in:

- Linux: `~/.local/share/rack-dashboard/`
- macOS: `~/Library/Application Support/rack-dashboard/`
- Windows: `%LOCALAPPDATA%\rack-dashboard\`

What the dashboard prints goes to `journalctl --user -u rack-dashboard` on Linux, to `~/Library/Logs/rack-dashboard.log` on macOS, and to `rack-dashboard.log` beside the state file on Windows.

## About the power figures

There is no power meter involved. GPU power comes from `nvidia-smi`. CPU power is measured when the kernel's energy counter can be read and estimated from load when it cannot. Board, memory, disks and fans are a fixed allowance. The sum is then divided by the efficiency of the voltage regulators and the power supply to get a figure for the wall. Anything that is an estimate is marked with `~` on the screen.

Treat the wall figure as the right ballpark, not a measurement. If you have a metering plug, compare once and adjust `DASH_POWER_BASE_W` and `DASH_PSU_EFFICIENCY` until they agree.

On Windows and macOS the CPU figure is always the estimate, and the estimate is only as good as its two end points. The defaults (25 W idle, 142 W flat out) describe a 105 W desktop processor. A laptop or an Apple chip draws a fraction of that, so set `DASH_CPU_IDLE_W` and `DASH_CPU_MAX_W` to your processor's figures or the page will overstate it.

On Linux, two optional steps give better readings. Both are undone by a reboot.

```bash
# Let ordinary users read the CPU energy counter. Kernels have kept it root-only since the
# PLATYPUS side-channel attack, so decide for yourself whether that matters on your machine.
sudo chmod a+r /sys/class/powercap/intel-rapl:0/energy_uj

# Fan speeds and voltages need the driver for the board's sensor chip. nct6775 covers many
# ASUS and ASRock boards; sensors-detect from lm-sensors will tell you which one yours needs.
sudo modprobe nct6775
```

## How it is put together

```
rack_dashboard/            FastAPI app. Three collectors (containers, network, hardware) run once
                           a second and the result is pushed to every open page over
                           Server-Sent Events.
rack_dashboard/frontend/   The page: ES modules, canvas for the graphs and the centrepiece,
                           no framework.
tests/                     pytest suite, plus checks of the animation logic.
install.sh, install.ps1    The installers described above.
.github/workflows/         Runs the tests, starts the dashboard and runs both installers on
                           Linux, Windows and macOS for every push.
```

`GET /api/snapshot` returns the latest reading as JSON and `GET /api/stream` is the live stream, in case you want the data for something else.

## Working on it

```bash
git clone https://github.com/nprev-dev/Docker-UI-app.git
cd Docker-UI-app
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
./run.sh
```

Run from a copy of the source like this, the dashboard keeps its state in a `data/` folder beside the code if there is one, which is why `data/` is in `.gitignore`.

The tests:

```bash
.venv/bin/python -m pytest
```

The checks of the animation logic are written in JavaScript and run under `gjs`, which GNOME desktops have installed, or under Node.js. Without either that one test is skipped.

## Not done yet

- The Agents section is empty.
- The GPU section does not know which AI model is loaded. Its `model` line always reads `unloaded` for now.
- No view of the switch ports.
- It has only ever run on one real machine (Linux, AMD CPU, NVIDIA GPU). Other hardware will probably leave gaps in the power, temperature and fan sections, and Windows and macOS have only been tried on virtual machines.

## Licence

MIT. See `LICENSE`.

The bundled fonts, IBM Plex Sans and IBM Plex Mono, come under the SIL Open Font License. Its text is in `rack_dashboard/frontend/fonts/LICENSE.txt`.
