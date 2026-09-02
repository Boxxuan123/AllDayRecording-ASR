from __future__ import annotations

import ipaddress
import socket

import psutil


def _display_addresses(host: str) -> list[str]:
    if host not in {"0.0.0.0", "::", ""}:
        return [host]
    interface_addresses = _active_physical_ipv4_addresses()
    if interface_addresses:
        return interface_addresses
    addresses: set[str] = set()
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(result[4][0])
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    usable = sorted(
        address
        for address in addresses
        if not ipaddress.ip_address(address).is_loopback
        and not ipaddress.ip_address(address).is_link_local
        and not ipaddress.ip_address(address).is_unspecified
    )
    return usable or ["<电脑局域网 IP>"]


_VIRTUAL_INTERFACE_MARKERS = (
    "clash",
    "docker",
    "hamachi",
    "hyper-v",
    "loopback",
    "tailscale",
    "tap",
    "tun",
    "vbox",
    "vethernet",
    "virtualbox",
    "vmware",
    "vpn",
    "wsl",
    "zerotier",
)


def _active_physical_ipv4_addresses() -> list[str]:
    """Return active LAN addresses without VPN and VM host-only adapters."""
    stats = psutil.net_if_stats()
    candidates: list[tuple[str, str]] = []
    for interface_name, records in psutil.net_if_addrs().items():
        interface_stats = stats.get(interface_name)
        if interface_stats is None or not interface_stats.isup:
            continue
        for record in records:
            if record.family == socket.AF_INET:
                candidates.append((interface_name, record.address))
    return _prioritize_interface_addresses(candidates)


def _prioritize_interface_addresses(
    candidates: list[tuple[str, str]],
) -> list[str]:
    physical: set[str] = set()
    virtual: set[str] = set()
    for interface_name, value in candidates:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if (
            address.version != 4
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
        ):
            continue
        normalized_name = interface_name.casefold()
        target = (
            virtual
            if any(marker in normalized_name for marker in _VIRTUAL_INTERFACE_MARKERS)
            else physical
        )
        target.add(str(address))
    # A machine that only has a VPN/virtual adapter should remain usable, but
    # those addresses must never outrank an active Wi-Fi or Ethernet adapter.
    return sorted(physical or virtual)


def _tls_addresses(host: str) -> list[str]:
    displayed = _display_addresses(host)
    return [value for value in displayed if not value.startswith("<")]
