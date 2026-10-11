"""Windows and macOS: reading what their own tools say. The samples are as real machines printed them."""

from __future__ import annotations

import json
import socket
import time
from collections import namedtuple
from types import SimpleNamespace

import pytest

from rack_dashboard.collectors import macos, portable, system, windows
from rack_dashboard.collectors.network import Sock

# --- Windows ------------------------------------------------------------------------------------

WINDOWS_ROUTES = """===========================================================================
Interface List
 14...00 22 48 c0 67 2f ......Microsoft Hyper-V Network Adapter #3
  1...........................Software Loopback Interface 1
 11...00 15 5d 45 ce 9d ......Hyper-V Virtual Ethernet Adapter
===========================================================================

IPv4 Route Table
===========================================================================
Active Routes:
Network Destination        Netmask          Gateway       Interface  Metric
          0.0.0.0          0.0.0.0         10.1.0.1       10.1.0.140     11
===========================================================================
Persistent Routes:
  None
"""


def test_windows_default_route():
    assert windows.parse_route(WINDOWS_ROUTES) == ("10.1.0.140", "10.1.0.1")


def test_windows_route_with_the_lowest_metric_wins():
    two = WINDOWS_ROUTES.replace(
        "          0.0.0.0          0.0.0.0         10.1.0.1       10.1.0.140     11\n",
        "          0.0.0.0          0.0.0.0      192.168.1.1     192.168.1.50     50\n          0.0.0.0          0.0.0.0         10.1.0.1       10.1.0.140     11\n",
    )
    assert windows.parse_route(two) == ("10.1.0.140", "10.1.0.1")


def test_windows_route_in_another_language():
    # A German Windows: every word differs, the numbers do not.
    german = "Aktive Routen:\n     Netzwerkziel    Netzwerkmaske          Gateway    Schnittstelle Metrik\n          0.0.0.0          0.0.0.0      192.168.1.1     192.168.1.50     25\n"
    assert windows.parse_route(german) == ("192.168.1.50", "192.168.1.1")
    # A route that needs no gateway says so in words ("On-link", "Auf Verbindung").
    assert windows.parse_route("          0.0.0.0          0.0.0.0   Auf Verbindung     10.8.0.2      5\n") == ("10.8.0.2", None)
    assert windows.parse_route("          0.0.0.0          0.0.0.0         On-link         10.8.0.2      5\n") == ("10.8.0.2", None)


@pytest.mark.parametrize("text", ["", "Active Routes:\n  None\n", "garbage 0.0.0.0", "          10.0.0.0        255.0.0.0         10.1.0.1       10.1.0.140     11\n"])
def test_windows_without_a_default_route(text):
    assert windows.parse_route(text) == (None, None)


# The registry of a real machine: the adapter in use, a virtual one, and three long gone.
ADAPTERS = [
    {"NameServer": "", "IPAddress": ["172.21.144.1"]},
    {"NameServer": "", "DhcpNameServer": "168.63.129.16", "DhcpIPAddress": "10.1.0.140"},
    {},
    {"NameServer": "", "DhcpNameServer": "10.9.9.9", "DhcpIPAddress": "10.0.0.10"},
    {"NameServer": ""},
]


def test_windows_name_servers_come_from_the_adapter_in_use():
    assert windows.pick_name_servers(ADAPTERS, "10.1.0.140") == ["168.63.129.16"]


def test_windows_name_servers_set_by_hand_win_over_dhcp():
    adapters = [{"NameServer": "9.9.9.9,1.1.1.1", "DhcpNameServer": "192.168.1.1", "DhcpIPAddress": "192.168.1.50"}]
    assert windows.pick_name_servers(adapters, "192.168.1.50") == ["9.9.9.9", "1.1.1.1"]
    # Separated by spaces, with a fixed address held as a list.
    adapters = [{"NameServer": "9.9.9.9 1.1.1.1 9.9.9.9", "IPAddress": ["192.168.1.50"]}]
    assert windows.pick_name_servers(adapters, "192.168.1.50") == ["9.9.9.9", "1.1.1.1"]


def test_windows_name_servers_when_the_adapter_is_not_known():
    # Without knowing which adapter is in use, the first that has servers is the best guess.
    assert windows.pick_name_servers(ADAPTERS, None) == ["168.63.129.16"]
    assert windows.pick_name_servers(ADAPTERS, "203.0.113.9") == ["168.63.129.16"]
    assert windows.pick_name_servers([], "10.1.0.140") == []
    assert windows.pick_name_servers([{"NameServer": "not an address"}], None) == []


WINDOWS_INVENTORY = ('{"board":"Virtual Machine","vendor":"Microsoft Corporation","bios":"Hyper-V UEFI Release v4.1","bios_date":"2026-04-22",'
                     '"cpu":"AMD EPYC 7763 64-Core Processor                ","cores":2,"threads":4,"ram":17179869184,'
                     '"disks":[{"id":"1","name":"Msft Virtual Disk","bytes":161061273600,"media":"Unspecified","bus":"SAS"},'
                     '{"id":"0","name":"Samsung SSD 990 PRO 2TB","bytes":2000398934016,"media":"SSD","bus":"NVMe"}]}')


def test_windows_inventory():
    assert windows.parse_inventory(WINDOWS_INVENTORY) == {
        "board": "Virtual Machine", "vendor": "Microsoft Corporation", "bios": "Hyper-V UEFI Release v4.1", "bios_date": "2026-04-22",
        "cpu": {"model": "AMD EPYC 7763 64-Core Processor", "cores": 2, "threads": 4},
        "ram_bytes": 17179869184,
        "disks": [
            {"name": "disk0", "bytes": 2000398934016, "model": "Samsung SSD 990 PRO 2TB", "kind": "nvme"},
            {"name": "disk1", "bytes": 161061273600, "model": "Msft Virtual Disk", "kind": "disk"},
        ],
    }


def test_windows_inventory_with_one_disk_and_gaps():
    # PowerShell writes a list of one as the thing itself, and leaves out what it could not find.
    text = '{"board":null,"vendor":"","bios":null,"bios_date":null,"cpu":null,"cores":null,"threads":0,"ram":null,"disks":{"id":"0","name":"WDC WD40EFRX","bytes":4000787030016,"media":"HDD","bus":"SATA"}}'
    result = windows.parse_inventory(text)
    assert result["cpu"] == {"model": None, "cores": None, "threads": None} and result["ram_bytes"] is None and result["board"] is None and result["vendor"] is None
    assert result["disks"] == [{"name": "disk0", "bytes": 4000787030016, "model": "WDC WD40EFRX", "kind": "hdd"}]
    assert windows.parse_inventory('{"disks":[{"id":"0","name":"empty card reader","bytes":0}]}')["disks"] == []


@pytest.mark.parametrize(("media", "bus", "kind"), [("SSD", "NVMe", "nvme"), ("SSD", "SATA", "ssd"), ("HDD", "SATA", "hdd"), ("Unspecified", "SAS", "disk"), (None, None, "disk"), ("3", "17", "disk")])
def test_windows_disk_kinds(media, bus, kind):
    assert windows.disk_kind(media, bus) == kind


def test_windows_inventory_is_fetched_off_to_the_side():
    asked = []

    def slow():
        asked.append(1)
        time.sleep(0.15)
        return WINDOWS_INVENTORY

    inventory = windows.Inventory(query=slow, fallback=lambda: {"cpu": {"model": None, "cores": 2, "threads": 4}, "ram_bytes": 17174360064})
    first = inventory.read()
    # At once, with what is known already, marked as not the last word.
    assert first["pending"] is True and first["board"] is None and first["cpu"]["threads"] == 4 and first["ram_bytes"] == 17174360064
    assert inventory.read()["pending"] is True
    deadline = time.time() + 5
    while inventory.read().get("pending") and time.time() < deadline:
        time.sleep(0.02)
    final = inventory.read()
    assert "pending" not in final and final["board"] == "Virtual Machine" and final["cpu"] == {"model": "AMD EPYC 7763 64-Core Processor", "cores": 2, "threads": 4}
    # What the machine says is installed beats what the system says it can use.
    assert final["ram_bytes"] == 17179869184
    assert asked == [1]


@pytest.mark.parametrize("failure", [FileNotFoundError("powershell"), TimeoutError(), ValueError("not json")])
def test_windows_inventory_that_cannot_be_fetched(failure):
    def broken():
        raise failure

    inventory = windows.Inventory(query=broken, fallback=lambda: {"cpu": {"model": None, "cores": 8, "threads": 16}, "ram_bytes": 34359738368})
    deadline = time.time() + 5
    while inventory.read().get("pending") and time.time() < deadline:
        time.sleep(0.02)
    final = inventory.read()
    # Still an answer, built from what could be had without PowerShell.
    assert "pending" not in final and final["board"] is None and final["disks"] == []
    assert final["cpu"] == {"model": None, "cores": 8, "threads": 16} and final["ram_bytes"] == 34359738368


def test_windows_ping_goes_through_the_systems_service():
    class Service:
        def __init__(self, status):
            self.status, self.asked = status, []

        def send(self, address, timeout_ms):
            self.asked.append((address, timeout_ms))
            return self.status

    answering = Service(0)
    rtt = windows.echo(answering, "192.0.2.7", 3, 1.5)
    assert rtt is not None and 0 <= rtt < 1000 and answering.asked == [("192.0.2.7", 1500)]
    # 11010 is the service's "timed out"; None is no answer at all.
    assert windows.echo(Service(11010), "192.0.2.7", 4, 1.0) is None
    assert windows.echo(Service(None), "192.0.2.7", 5, 1.0) is None
    # A name is looked up first: the service only takes addresses.
    by_name = Service(0)
    windows.echo(by_name, "localhost", 6, 0.0001)
    assert by_name.asked == [("127.0.0.1", 1)]


# --- macOS --------------------------------------------------------------------------------------

MAC_ROUTE = """   route to: default
destination: default
       mask: default
    gateway: 192.168.64.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
 recvpipe  sendpipe  ssthresh  rtt,msec    rttvar  hopcount      mtu     expire
       0         0         0         0         0         0      1500         0 
"""


def test_mac_default_route():
    assert macos.parse_route(MAC_ROUTE) == ("en0", "192.168.64.1")


def test_mac_route_without_a_gateway():
    # A VPN's tunnel is a default route with a link for a gateway, not an address.
    assert macos.parse_route(MAC_ROUTE.replace("192.168.64.1", "link#22").replace("en0", "utun4")) == ("utun4", None)
    assert macos.parse_route("") == (None, None)
    assert macos.parse_route("route: writing to routing socket: not in table\n") == (None, None)


MAC_TCP = """Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address                                 Foreign Address                               (state)          rxbytes      txbytes  rhiwat  shiwat          process:pid    state  options           gencnt    flags   flags1 usecnt rtncnt fltrs
tcp4       0      0  192.168.64.9.49172     140.82.114.23.443      ESTABLISHED        13066         3654  131072  132432 provjobd22727638:956    00182 00000008 0000000000000d13 00000080 04000900      2      0 000000
tcp4       0      0  192.168.64.9.49156     20.85.130.105.443      ESTABLISHED        11234         3906  131072  132432  Runner.Listener:1133   00102 00000000 0000000000000a7d 00000080 04000900      2      0 000000
tcp6       0      0  fe80::1%lo0.8021       *.*                    LISTEN                 0            0  131072  131072 Google Chrome He:4410   00180 00000006 0000000000000011 00000000 00000800      1      0 000000
tcp4       0      0  *.22                   *.*                    LISTEN                 0            0  131072  131072          launchd:1      00180 00000006 0000000000000010 00000000 00000800      1      0 000000
tcp46      0      0  *.5000                 *.*                    LISTEN                 0            0  131072  131072    ControlCenter:612    00180 00000006 000000000000000f 00000000 00000800      1      0 000000
tcp4       0      0  192.168.64.9.49170     140.82.114.23.443      TIME_WAIT            120          340  131072  132432          launchd:1      00180 00000006 0000000000000010 00000000 00000800      1      0 000000
"""
MAC_UDP = """Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address                                 Foreign Address                               (state)          rxbytes      txbytes  rhiwat  shiwat          process:pid    state  options           gencnt    flags   flags1 usecnt rtncnt fl
udp46      0      0  *.55665                *.*                                           0            0  786896    9216      replicatord:441    00100 00000204 00000000000005b7 00000000 00080800      1      0 000000
udp4       0      0  *.5353                 *.*                                           0         1188  786896    9216     mDNSResponder:321    00180 00000000 00000000000005b3 00000000 04000800      1      0 000002
udp4       0      0  *.*                    *.*                                           0            0  786896    9216         wifip2pd:475    00080 00000000 000000000000042b 00000000 00000800      1      0 000000
udp4       0      0  192.168.64.9.61000     17.253.4.125.123                             90           90  786896    9216            timed:150    00080 00000000 000000000000042c 00000000 00000800      1      0 000000
"""


def test_mac_tcp_sockets():
    sockets = macos.parse_netstat(MAC_TCP, Sock)

    assert sockets[0] == Sock("tcp", "ESTAB", "192.168.64.9", 49172, "140.82.114.23", 443, 3654, 13066, "provjobd22727638")
    assert sockets[2] == Sock("tcp", "LISTEN", "fe80::1", 8021, "*", None, 0, 0, "Google Chrome He")
    assert sockets[3] == Sock("tcp", "LISTEN", "*", 22, "*", None, 0, 0, "launchd")
    assert sockets[4].local_port == 5000 and sockets[4].proto == "tcp" and sockets[4].process == "ControlCenter"
    assert sockets[5].state == "TIME-WAIT"
    assert len(sockets) == 6


def test_mac_udp_sockets():
    sockets = macos.parse_netstat(MAC_UDP, Sock)

    assert sockets[1] == Sock("udp", "UNCONN", "*", 5353, "*", None, 1188, 0, "mDNSResponder")
    # A socket bound to nothing in particular is not listening on any port.
    assert sockets[2].local_port is None
    assert sockets[3] == Sock("udp", "ESTAB", "192.168.64.9", 61000, "17.253.4.125", 123, 90, 90, "timed")


def test_mac_sockets_feed_the_same_summaries_as_linux():
    from rack_dashboard.collectors import network

    sockets = macos.parse_netstat(MAC_TCP, Sock) + macos.parse_netstat(MAC_UDP, Sock)
    ports = {(entry["port"], entry["proto"]): entry for entry in network.listening(sockets, network.listener_names(sockets))}
    assert ports[(22, "tcp")] == {"port": 22, "proto": "tcp", "scope": "*", "who": "launchd"}
    assert ports[(8021, "tcp")]["scope"] == "lan" and ports[(5353, "udp")]["who"] == "mDNSResponder"
    assert network.socket_counts(sockets) == {"tcp_estab": 2, "tcp_listen": 3, "tcp_time_wait": 1, "tcp_other": 0, "udp": 4}
    # And, since macOS counts each connection's bytes, who is being talked to can be told.
    talkers = network.Talkers()
    talkers.update(sockets, now=0)
    moved = [s._replace(received=s.received + 125_000, acked=s.acked + 12_500) if s.peer == "140.82.114.23" and s.state == "ESTAB" else s for s in sockets]
    assert talkers.update(moved, now=1)[0]["host"] == "140.82.114.23"


def test_mac_sockets_from_an_older_system_or_cut_short():
    # Without the verbose columns there are no byte counts and no names, but the sockets are there.
    plain = "tcp4       0      0  *.22                   *.*                    LISTEN\ntcp4  0  0  10.0.0.5.50000  1.2.3.4.443  ESTABLISHED\n"
    assert macos.parse_netstat(plain, Sock) == [Sock("tcp", "LISTEN", "*", 22, "*", None, None, None, None), Sock("tcp", "ESTAB", "10.0.0.5", 50000, "1.2.3.4", 443, None, None, None)]
    # Separate pid columns instead of "name:pid".
    older = "tcp4       0      0  *.22                   *.*                    LISTEN        0   0  131072  131072      1      0 0x0180 0x00000006\n"
    assert macos.parse_netstat(older, Sock) == [Sock("tcp", "LISTEN", "*", 22, "*", None, 0, 0, None)]
    assert macos.parse_netstat("", Sock) == [] and macos.parse_netstat("Active Internet connections\nProto Recv-Q\ntcp4 0\n", Sock) == []


MAC_HARDWARE = json.dumps({"SPHardwareDataType": [{
    "_name": "hardware_overview", "boot_rom_version": "18000.161.10", "chip_type": "Apple M1 (Virtual)", "machine_model": "VirtualMac2,1", "machine_name": "Apple Virtual Machine 1",
    "number_processors": 3, "physical_memory": "7 GB", "platform_UUID": "AAD034A0-0000-0000-0000-5E861619A921", "provisioning_UDID": "1eb8c818f2204719168538cc18ffa7ebabf6cfa8", "serial_number": "Z64WKWYHKM"}]})
MAC_STORAGE = json.dumps({"SPNVMeDataType": [], "SPSerialATADataType": [], "SPStorageDataType": [
    {"_name": "Data", "bsd_name": "disk2s5", "mount_point": "/System/Volumes/Data", "physical_drive": {"is_internal_disk": "yes", "media_name": "AppleAPFSMedia", "medium_type": "ssd"}, "size_in_bytes": 343073095680},
    {"_name": "Macintosh HD", "bsd_name": "disk2s1s1", "mount_point": "/", "physical_drive": {"is_internal_disk": "yes", "media_name": "AppleAPFSMedia", "medium_type": "ssd"}, "size_in_bytes": 343073095680},
    {"_name": "Backup", "bsd_name": "disk5s1", "mount_point": "/Volumes/Backup", "physical_drive": {"is_internal_disk": "no", "media_name": "My Passport", "medium_type": "hdd"}, "size_in_bytes": 2000000000000}]})


def test_mac_hardware_names_and_never_its_serial_number():
    hardware = macos.parse_hardware(MAC_HARDWARE)

    assert hardware == {"board": "VirtualMac2,1", "vendor": "Apple", "bios": "18000.161.10", "bios_date": None, "cpu_model": "Apple M1 (Virtual)", "ram_bytes": 7 * 2**30}
    assert "Z64WKWYHKM" not in json.dumps(hardware) and "AAD034A0" not in json.dumps(hardware) and "1eb8c818" not in json.dumps(hardware)


@pytest.mark.parametrize("text", ["", "not json", "{}", '{"SPHardwareDataType": []}', '{"SPHardwareDataType": [{}]}'])
def test_mac_hardware_that_cannot_be_read(text):
    hardware = macos.parse_hardware(text)
    assert hardware["board"] is None and hardware["cpu_model"] is None and hardware["ram_bytes"] is None


def test_mac_drives():
    # Nothing listed as a drive of its own: the internal drive under the volumes, once.
    assert macos.parse_disks(MAC_STORAGE) == [{"name": "disk2", "bytes": 343073095680, "model": "AppleAPFSMedia", "kind": "ssd"}]
    # Drives listed properly are taken as they are.
    listed = json.dumps({"SPNVMeDataType": [{"_items": [{"_name": "APPLE SSD AP1024Z", "bsd_name": "disk0", "device_model": "APPLE SSD AP1024Z", "size_in_bytes": 1000555581440}]}],
                         "SPSerialATADataType": [{"_items": [{"_name": "ST4000DM004", "bsd_name": "disk4", "device_model": "ST4000DM004-2CV104", "size_in_bytes": 4000787030016, "spsata_medium_type": "Rotational"},
                                                               {"_name": "Samsung SSD 870", "bsd_name": "disk5", "size_in_bytes": 1000204886016, "spsata_medium_type": "Solid State"}]}], "SPStorageDataType": []})
    assert macos.parse_disks(listed) == [
        {"name": "disk0", "bytes": 1000555581440, "model": "APPLE SSD AP1024Z", "kind": "nvme"},
        {"name": "disk4", "bytes": 4000787030016, "model": "ST4000DM004-2CV104", "kind": "hdd"},
        {"name": "disk5", "bytes": 1000204886016, "model": "Samsung SSD 870", "kind": "ssd"},
    ]
    assert macos.parse_disks("") == [] and macos.parse_disks("{}") == []


def test_mac_inventory(monkeypatch):
    def fake_run(command, timeout=5.0):
        return MAC_HARDWARE if command[1:] == ["SPHardwareDataType", "-json"] else MAC_STORAGE

    monkeypatch.setattr(macos, "run", fake_run)
    inventory = macos.read_inventory(basics=lambda: {"cpu": {"model": None, "cores": 3, "threads": 3}, "ram_bytes": 7516192768})

    assert inventory == {"board": "VirtualMac2,1", "vendor": "Apple", "bios": "18000.161.10", "bios_date": None, "cpu": {"model": "Apple M1 (Virtual)", "cores": 3, "threads": 3},
                         "ram_bytes": 7 * 2**30, "disks": [{"name": "disk2", "bytes": 343073095680, "model": "AppleAPFSMedia", "kind": "ssd"}]}


def test_mac_inventory_when_the_tool_fails(monkeypatch):
    def broken(command, timeout=5.0):
        raise FileNotFoundError(command[0])

    monkeypatch.setattr(macos, "run", broken)
    inventory = macos.read_inventory(basics=lambda: {"cpu": {"model": None, "cores": 8, "threads": 8}, "ram_bytes": 17179869184})

    assert inventory["board"] is None and inventory["disks"] == [] and inventory["cpu"] == {"model": None, "cores": 8, "threads": 8} and inventory["ram_bytes"] == 17179869184


# --- the readers built on psutil --------------------------------------------------------------------

Stats = namedtuple("Stats", "isup duplex speed mtu flags")
Counters = namedtuple("Counters", "bytes_sent bytes_recv packets_sent packets_recv errin errout dropin dropout")
Address = namedtuple("Address", "family address netmask")
Endpoint = namedtuple("Endpoint", "ip port")
Conn = namedtuple("Conn", "fd family type laddr raddr status pid")


def fake_psutil(**over):
    """psutil as it answered on a real Windows machine."""
    link, full, half = -1, 2, 1
    names = {3936: "mqsvc.exe", 4: "System", 8632: "StartMenuExperienceHost.exe"}

    def process(pid):
        if pid not in names:
            raise PermissionError(pid)
        return SimpleNamespace(name=lambda: names[pid])

    base = SimpleNamespace(
        AF_LINK=link, NIC_DUPLEX_FULL=full, NIC_DUPLEX_HALF=half,
        net_if_stats=lambda: {"Ethernet 3": Stats(True, full, 1000, 1500, ""), "vEthernet (nat)": Stats(True, full, 4294, 1500, ""), "Wi-Fi": Stats(False, 0, 0, 0, "")},
        net_if_addrs=lambda: {"Ethernet 3": [Address(link, "00-22-48-C0-67-2F", None), Address(socket.AF_INET, "10.1.0.140", "255.255.240.0")], "vEthernet (nat)": [Address(socket.AF_INET, "172.21.144.1", None)]},
        net_io_counters=lambda pernic: {"Ethernet 3": Counters(4125308, 42897863, 5623, 17375, 1, 2, 3, 4)},
        net_connections=lambda kind: [
            Conn(-1, socket.AF_INET, socket.SOCK_STREAM, Endpoint("0.0.0.0", 49671), (), "LISTEN", 3936),
            Conn(-1, socket.AF_INET6, socket.SOCK_STREAM, Endpoint("::", 3389), (), "LISTEN", 1344),
            Conn(-1, socket.AF_INET, socket.SOCK_STREAM, Endpoint("10.1.0.140", 49951), Endpoint("20.99.129.183", 443), "ESTABLISHED", 8632),
            Conn(-1, socket.AF_INET6, socket.SOCK_STREAM, Endpoint("::ffff:10.1.0.140", 50001), Endpoint("::ffff:140.82.114.21", 443), "TIME_WAIT", 0),
            Conn(-1, socket.AF_INET, socket.SOCK_STREAM, Endpoint("10.1.0.140", 50002), Endpoint("1.2.3.4", 443), "SYN_SENT", None),
            Conn(-1, socket.AF_INET, socket.SOCK_DGRAM, Endpoint("10.1.0.140", 138), (), "NONE", 4),
            Conn(-1, socket.AF_INET6, socket.SOCK_DGRAM, (), (), "NONE", 4),
        ],
        Process=process,
        boot_time=lambda: 1791756858.1640923,
        cpu_times=lambda: SimpleNamespace(user=100.0, system=50.0, idle=800.0, interrupt=5.0, dpc=1.0, __iter__=lambda self=None: iter((100.0, 50.0, 800.0, 5.0, 1.0))),
        cpu_count=lambda logical=True: 4 if logical else 2,
        virtual_memory=lambda: SimpleNamespace(total=17174360064),
        sensors_battery=lambda: None,
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def test_portable_link():
    ps = fake_psutil()

    assert portable.read_link("Ethernet 3", ps) == {
        "name": "Ethernet 3", "state": "up", "speed": 1000, "duplex": "full", "mtu": 1500, "mac": "00:22:48:c0:67:2f", "flaps": None,
        # psutil lists what was sent before what was received; the two must not be swapped.
        "rx_bytes": 42897863, "tx_bytes": 4125308, "rx_errs": 1, "tx_errs": 2, "rx_drop": 3, "tx_drop": 4,
    }
    assert portable.read_link("nope", ps) is None


def test_portable_link_with_no_counters_and_a_made_up_speed():
    ps = fake_psutil()

    virtual = portable.read_link("vEthernet (nat)", ps)
    # 4294 megabits is a counter's largest number, not a speed.
    assert virtual["speed"] is None and virtual["rx_bytes"] is None and virtual["mac"] is None and virtual["state"] == "up"
    down = portable.read_link("Wi-Fi", ps)
    assert down["state"] == "down" and down["speed"] is None and down["duplex"] is None and down["mtu"] is None


def test_portable_interface_is_found_by_its_address():
    ps = fake_psutil()

    assert portable.interface_of("10.1.0.140", ps) == "Ethernet 3"
    assert portable.interface_of("192.0.2.1", ps) is None and portable.interface_of(None, ps) is None


def test_portable_sockets():
    sockets = portable.read_sockets(Sock, with_processes=True, ps=fake_psutil())

    assert sockets == [
        Sock("tcp", "LISTEN", "0.0.0.0", 49671, "*", None, None, None, "mqsvc"),
        # A process that may not be looked at simply has no name.
        Sock("tcp", "LISTEN", "::", 3389, "*", None, None, None, None),
        Sock("tcp", "ESTAB", "10.1.0.140", 49951, "20.99.129.183", 443, None, None, "StartMenuExperienceHost"),
        Sock("tcp", "TIME-WAIT", "::ffff:10.1.0.140", 50001, "140.82.114.21", 443, None, None, None),
        Sock("tcp", "SYN-SENT", "10.1.0.140", 50002, "1.2.3.4", 443, None, None, None),
        Sock("udp", "UNCONN", "10.1.0.140", 138, "*", None, None, None, "System"),
        Sock("udp", "UNCONN", "*", None, "*", None, None, None, "System"),
    ]
    # Names cost a look-up each, so they are only fetched when asked for.
    assert all(sock.process is None for sock in portable.read_sockets(Sock, ps=fake_psutil()))


def test_portable_sockets_feed_the_same_summaries_as_linux():
    from rack_dashboard.collectors import network

    sockets = portable.read_sockets(Sock, with_processes=True, ps=fake_psutil())
    assert network.socket_counts(sockets) == {"tcp_estab": 1, "tcp_listen": 2, "tcp_time_wait": 1, "tcp_other": 1, "udp": 2}
    listening = network.listening(sockets, network.listener_names(sockets))
    assert {"port": 3389, "proto": "tcp", "scope": "*", "who": "rdp"} in listening
    assert {"port": 138, "proto": "udp", "scope": "lan", "who": "System"} in listening
    # No byte counts on this system: nobody can be named as a talker, and nothing breaks trying.
    assert network.Talkers().update(sockets, now=0) == [] and network.Talkers().update(sockets, now=1) == []


def test_portable_boot_and_processor_time():
    ps = fake_psutil()

    boot_id, started = portable.read_boot(ps)
    assert started == 1791756858.1640923 and boot_id == portable.read_boot(fake_psutil(boot_time=lambda: 1791756859.9))[0]
    assert boot_id != portable.read_boot(fake_psutil(boot_time=lambda: 1791760000.0))[0]

    Times = namedtuple("Times", "user system idle")
    assert portable.cpu_ticks(fake_psutil(cpu_times=lambda: Times(100.0, 50.0, 800.0))) == (950.0, 800.0)
    Linux = namedtuple("Linux", "user nice system idle iowait")
    assert portable.cpu_ticks(fake_psutil(cpu_times=lambda: Linux(100.0, 0.0, 50.0, 800.0, 20.0))) == (970.0, 820.0)
    assert portable.basics(ps) == {"cpu": {"model": None, "cores": 2, "threads": 4}, "ram_bytes": 17174360064}


Battery = namedtuple("Battery", "percent secsleft power_plugged")


def test_portable_battery():
    assert portable.read_battery(fake_psutil()) == {"present": False}
    plugged = portable.read_battery(fake_psutil(sensors_battery=lambda: Battery(100, -2, True)))
    assert plugged == {"present": True, "model": "Built-in battery", "state": "fully-charged", "on_battery": False, "percent": 100.0, "runtime_s": None}
    assert portable.read_battery(fake_psutil(sensors_battery=lambda: Battery(61, -2, True)))["state"] == "charging"
    running = portable.read_battery(fake_psutil(sensors_battery=lambda: Battery(48.5, 5400, False)))
    assert running["on_battery"] is True and running["state"] == "discharging" and running["runtime_s"] == 5400 and running["percent"] == 48.5
    # "Unknown" comes as a negative number.
    assert portable.read_battery(fake_psutil(sensors_battery=lambda: Battery(48, -1, False)))["runtime_s"] is None

    def broken():
        raise RuntimeError("no battery interface")

    assert portable.read_battery(fake_psutil(sensors_battery=broken)) == {"present": False}


# --- choosing the readers ---------------------------------------------------------------------------


def test_system_names_and_what_each_cannot_read():
    assert [system.name(p) for p in ("linux", "win32", "darwin", "freebsd14")] == ["Linux", "Windows", "macOS", "freebsd14"]
    assert system.missing("linux") == [] and system.missing("win32") == ["talkers", "sensors"] and system.missing("darwin") == ["sensors"]
    assert system.missing("freebsd14") == ["talkers", "sensors"]
    # The list handed out is the caller's own.
    system.missing("win32").append("x")
    assert system.missing("win32") == ["talkers", "sensors"]


def test_portable_readers_are_used_off_linux_or_when_asked_for():
    assert not system.portable("linux", {}) and system.portable("win32", {}) and system.portable("darwin", {})
    assert system.portable("linux", {"DASH_PORTABLE": "1"}) and not system.portable("linux", {"DASH_PORTABLE": "0"})
    assert system.network_readers("linux", {}) == {} and system.hardware_readers("linux", {}) == {}


def test_readers_chosen_for_each_system():
    windows_net = system.network_readers("win32", {})
    assert set(windows_net) == {"link", "boot", "talkers", "route", "sockets", "resolvers", "pinger"} and windows_net["talkers"] is False
    mac_net = system.network_readers("darwin", {})
    assert set(mac_net) == {"link", "boot", "talkers", "route", "sockets"} and mac_net["talkers"] is True
    assert set(system.hardware_readers("win32", {})) == {"cpu_ticks", "inventory", "hwmon", "ups"}
    assert set(system.hardware_readers("darwin", {})) == {"cpu_ticks", "inventory", "hwmon", "ups"}
    # Linux in try-out mode keeps its own way of finding routes, name servers and hardware.
    assert set(system.network_readers("linux", {"DASH_PORTABLE": "1"})) == {"link", "boot", "talkers", "sockets"}
    assert set(system.hardware_readers("linux", {"DASH_PORTABLE": "1"})) == {"cpu_ticks"}


def test_docker_is_looked_for_where_each_system_keeps_it(tmp_path):
    assert system.docker_sockets("win32", tmp_path) == ["npipe:////./pipe/docker_engine", "npipe:////./pipe/dockerDesktopLinuxEngine"]
    assert system.docker_sockets("darwin", tmp_path) == ["unix:///var/run/docker.sock", f"unix://{tmp_path}/.docker/run/docker.sock"]
    assert system.docker_sockets("linux", tmp_path) == ["unix:///var/run/docker.sock", f"unix://{tmp_path}/.docker/desktop/docker.sock"]


def test_slow_answers_are_remembered_for_a_while():
    now = [100.0]
    asked = []

    def slow():
        asked.append(now[0])
        return len(asked)

    ask = system.remembered(10, slow, clock=lambda: now[0])
    assert ask() == 1 and ask() == 1
    now[0] = 109.9
    assert ask() == 1
    now[0] = 110.0
    assert ask() == 2 and asked == [100.0, 110.0]
