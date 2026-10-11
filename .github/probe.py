"""Temporary: prints what a Windows or macOS machine's own tools say, to write the readers against."""
import ctypes, json, locale, os, platform, socket, struct, subprocess, sys, time


def section(title):
    print(f"\n===== {title} =====", flush=True)


def run(*command, limit=60, timeout=40, shell=False):
    started = time.perf_counter()
    try:
        out = subprocess.run(command if not shell else command[0], capture_output=True, text=True, timeout=timeout, shell=shell)
        text = (out.stdout or "") + (("\n[stderr] " + out.stderr) if out.stderr.strip() else "")
        code = out.returncode
    except Exception as exc:  # noqa: BLE001
        text, code = f"{type(exc).__name__}: {exc}", None
    took = (time.perf_counter() - started) * 1000
    lines = text.splitlines()
    print(f"$ {' '.join(command) if not shell else command[0]}   [exit {code}, {took:.0f} ms, {len(lines)} lines]")
    for line in lines[:limit]:
        print("  |" + line)
    if len(lines) > limit:
        print(f"  | ... {len(lines) - limit} more")


def attempt(label, function):
    started = time.perf_counter()
    try:
        value = function()
        print(f"{label}: {value!r}   [{(time.perf_counter() - started) * 1000:.1f} ms]")
        return value
    except Exception as exc:  # noqa: BLE001
        print(f"{label}: RAISED {type(exc).__name__}: {exc}   [{(time.perf_counter() - started) * 1000:.1f} ms]")
        return None


section("system")
print(sys.platform, "|", platform.platform(), "|", platform.machine(), "| python", sys.version.split()[0], "| locale", locale.getlocale(), "| processor", repr(platform.processor()))
print("hostname", socket.gethostname(), "| cpu_count", os.cpu_count())
for port, proto in ((22, "tcp"), (53, "udp"), (80, "tcp"), (443, "tcp"), (3389, "tcp"), (5353, "udp"), (8096, "tcp")):
    attempt(f"getservbyport {port}/{proto}", lambda: socket.getservbyport(port, proto))
attempt("address used towards 1.1.1.1", lambda: (lambda s: (s.connect(("1.1.1.1", 9)), s.getsockname()[0])[1])(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)))

section("psutil")
import psutil
print("psutil", psutil.__version__)
attempt("boot_time", psutil.boot_time)
attempt("cpu_percent(0.2)", lambda: psutil.cpu_percent(0.2))
attempt("cpu_count logical/physical", lambda: (psutil.cpu_count(), psutil.cpu_count(logical=False)))
attempt("cpu_freq", lambda: psutil.cpu_freq())
attempt("virtual_memory.total", lambda: psutil.virtual_memory().total)
attempt("sensors_battery", lambda: psutil.sensors_battery())
attempt("has sensors_temperatures", lambda: hasattr(psutil, "sensors_temperatures"))
attempt("net_if_stats", lambda: {k: tuple(v) for k, v in psutil.net_if_stats().items()})
attempt("net_if_addrs", lambda: {k: [(str(a.family), a.address, a.netmask) for a in v] for k, v in psutil.net_if_addrs().items()})
attempt("net_io_counters(pernic)", lambda: {k: tuple(v) for k, v in psutil.net_io_counters(pernic=True).items()})
attempt("disk_partitions", lambda: [tuple(p)[:3] for p in psutil.disk_partitions()])
conns = attempt("net_connections(inet) count", lambda: len(psutil.net_connections(kind="inet")))
try:
    listed = psutil.net_connections(kind="inet")
    kinds = {}
    for c in listed:
        key = (str(c.type).split(".")[-1], c.status)
        kinds[key] = kinds.get(key, 0) + 1
    print("kinds:", kinds)
    for c in [c for c in listed if c.status == "LISTEN"][:6] + [c for c in listed if c.status == "ESTABLISHED"][:3] + [c for c in listed if c.status == "NONE"][:4]:
        name = None
        try:
            name = psutil.Process(c.pid).name() if c.pid else None
        except Exception as exc:  # noqa: BLE001
            name = f"<{type(exc).__name__}>"
        print("  ", str(c.family).split(".")[-1], str(c.type).split(".")[-1], tuple(c.laddr) if c.laddr else None, tuple(c.raddr) if c.raddr else None, c.status, c.pid, name)
except Exception as exc:  # noqa: BLE001
    print("net_connections detail RAISED", type(exc).__name__, exc)
own = attempt("own process connections", lambda: len(psutil.Process().net_connections()) if hasattr(psutil.Process, "net_connections") else len(psutil.Process().connections()))

section("ICMP with a datagram socket (the Linux way)")
def dgram_ping(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    s.settimeout(2)
    def checksum(data):
        total = sum(struct.unpack(f"!{len(data) // 2}H", data))
        total = (total >> 16) + (total & 0xFFFF); total += total >> 16
        return ~total & 0xFFFF
    body = struct.pack("!BBHHH", 8, 0, 0, 0x1234, 7) + b"rack-dashboard--"
    packet = struct.pack("!BBHHH", 8, 0, checksum(body), 0x1234, 7) + b"rack-dashboard--"
    for label, data in (("kernel fills checksum", body), ("own checksum", packet)):
        try:
            started = time.perf_counter()
            s.sendto(data, (host, 0))
            reply, peer = s.recvfrom(512)
            print(f"  {host} [{label}]: {len(reply)} bytes in {(time.perf_counter() - started) * 1000:.2f} ms from {peer}; first bytes {reply[:28].hex()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {host} [{label}]: {type(exc).__name__}: {exc}")
    s.close()
for host in ("127.0.0.1", "1.1.1.1"):
    try:
        dgram_ping(host)
    except Exception as exc:  # noqa: BLE001
        print(f"  {host}: socket could not be opened: {type(exc).__name__}: {exc}")

section("docker")
for path in ("/var/run/docker.sock", os.path.expanduser("~/.docker/run/docker.sock"), os.path.expanduser("~/.docker/desktop/docker.sock"), r"\\.\pipe\docker_engine", r"\\.\pipe\dockerDesktopLinuxEngine"):
    print(f"  {path}: exists={os.path.exists(path)}")
def docker_ping(url):
    import docker
    client = docker.DockerClient(base_url=url, timeout=5)
    try:
        return client.ping(), client.version().get("Os"), len(client.containers.list(all=True))
    finally:
        client.close()
for url in (["npipe:////./pipe/docker_engine"] if sys.platform == "win32" else ["unix:///var/run/docker.sock"]):
    attempt(f"docker at {url}", lambda: docker_ping(url))

if sys.platform == "win32":
    section("windows: routes")
    run("route", "print", "-4", "0.0.0.0")
    run("route", "print", "-6", "::/0", limit=30)
    section("windows: dns servers in the registry")
    import winreg
    base = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
        for index in range(winreg.QueryInfoKey(root)[0]):
            guid = winreg.EnumKey(root, index)
            with winreg.OpenKey(root, guid) as key:
                values = {}
                for name in ("NameServer", "DhcpNameServer", "DhcpIPAddress", "IPAddress", "DhcpDefaultGateway", "DefaultGateway", "EnableDHCP"):
                    try:
                        values[name] = winreg.QueryValueEx(key, name)[0]
                    except OSError:
                        pass
                print("  ", guid, values)
    attempt("global NameServer", lambda: winreg.QueryValueEx(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters"), "NameServer")[0])
    section("windows: adapters (first lines of ipconfig /all)")
    run("ipconfig", "/all", limit=45)
    section("windows: ping through IcmpSendEcho")
    def icmp(host, timeout_ms=1500):
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        iphlpapi.IcmpCreateFile.restype = ctypes.c_void_p
        iphlpapi.IcmpSendEcho.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint16, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        iphlpapi.IcmpSendEcho.restype = ctypes.c_uint32
        iphlpapi.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
        handle = iphlpapi.IcmpCreateFile()
        data = b"rack-dashboard--"
        buffer = ctypes.create_string_buffer(28 + len(data) + 8 + 64)
        address = struct.unpack("<I", socket.inet_aton(host))[0]
        started = time.perf_counter()
        count = iphlpapi.IcmpSendEcho(handle, address, data, len(data), None, buffer, len(buffer), timeout_ms)
        took = (time.perf_counter() - started) * 1000
        error = ctypes.get_last_error()
        status, rtt = struct.unpack_from("<II", buffer.raw, 4)
        iphlpapi.IcmpCloseHandle(handle)
        return {"replies": count, "status": status, "rtt_field_ms": rtt, "measured_ms": round(took, 2), "last_error": error}
    for host in ("127.0.0.1", "1.1.1.1", "192.0.2.1"):
        attempt(f"IcmpSendEcho {host}", lambda: icmp(host))
    section("windows: hardware through CIM (PowerShell)")
    ps = "powershell -NoProfile -NonInteractive -Command "
    run(ps + '"Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product | ConvertTo-Json -Compress"', shell=True)
    run(ps + '"Get-CimInstance Win32_BIOS | Select-Object SMBIOSBIOSVersion,@{n=\'Date\';e={$_.ReleaseDate.ToString(\'yyyy-MM-dd\')}} | ConvertTo-Json -Compress"', shell=True)
    run(ps + '"Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json -Compress"', shell=True)
    run(ps + '"Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,TotalPhysicalMemory | ConvertTo-Json -Compress"', shell=True)
    run(ps + '"Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity | ConvertTo-Json -Compress"', shell=True)
    run(ps + '"Get-PhysicalDisk | Select-Object DeviceId,FriendlyName,MediaType,BusType,Size | ConvertTo-Json -Compress"', shell=True)
    run(ps + '"Get-CimInstance Win32_DiskDrive | Select-Object Index,Model,Size,MediaType,InterfaceType | ConvertTo-Json -Compress"', shell=True)
    run(ps + '"Get-CimInstance Win32_Battery | Select-Object Name,EstimatedChargeRemaining,BatteryStatus | ConvertTo-Json -Compress"', shell=True)
    one = ('"$b=Get-CimInstance Win32_BaseBoard;$i=Get-CimInstance Win32_BIOS;$p=@(Get-CimInstance Win32_Processor);$d=@(Get-PhysicalDisk);'
           '[pscustomobject]@{board=$b.Product;vendor=$b.Manufacturer;bios=$i.SMBIOSBIOSVersion;bios_date=$i.ReleaseDate.ToString(\'yyyy-MM-dd\');cpu=$p[0].Name;cores=($p|Measure-Object NumberOfCores -Sum).Sum;'
           'threads=($p|Measure-Object NumberOfLogicalProcessors -Sum).Sum;disks=@($d|ForEach-Object{[pscustomobject]@{name=$_.FriendlyName;bytes=$_.Size;media=[string]$_.MediaType;bus=[string]$_.BusType}})}|ConvertTo-Json -Compress -Depth 4"')
    run(ps + one, shell=True)
    run("where", "nvidia-smi")
    run("where", "docker")

if sys.platform == "darwin":
    section("macos: routes")
    run("route", "-n", "get", "default")
    run("netstat", "-rn", "-f", "inet", limit=14)
    section("macos: sockets")
    run("netstat", "-anv", "-p", "tcp", limit=16)
    run("netstat", "-anv", "-p", "udp", limit=10)
    run("netstat", "-an", "-p", "tcp", limit=8)
    run("lsof", "-nP", "-iTCP", "-sTCP:LISTEN", limit=12)
    run("lsof", "-nP", "-iUDP", limit=8)
    section("macos: dns")
    run("cat", "/etc/resolv.conf")
    run("scutil", "--dns", limit=30)
    section("macos: hardware")
    run("sysctl", "-n", "machdep.cpu.brand_string", "hw.physicalcpu", "hw.logicalcpu", "hw.memsize", "hw.model", "kern.boottime", "kern.osproductversion")
    run("system_profiler", "SPHardwareDataType", "-json", limit=40)
    run("system_profiler", "SPNVMeDataType", "SPSerialATADataType", "-json", limit=60)
    run("system_profiler", "SPStorageDataType", "-json", limit=50)
    run("diskutil", "list", limit=25)
    run("pmset", "-g", "batt")
    run("ifconfig", "en0")
    run("networksetup", "-listallhardwareports", limit=20)
    run("which", "docker", "nvidia-smi")
