from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Callable, Iterable
from typing import Any


SERVICE_TYPE = "_allday-pc._tcp.local."
SERVICE_TYPE_HARMONY = "_allday-pc._tcp"
SERVICE_PROTOCOL = "ALL_DAY_RECORDING_TRANSFER"
SERVICE_VERSION = "2"
NETWORK_POLL_SECONDS = 2.0


def receiver_id_from_fingerprint(fingerprint: str) -> str:
    normalized = fingerprint.replace(":", "").strip().lower()
    if len(normalized) != 64 or any(value not in "0123456789abcdef" for value in normalized):
        raise ValueError("接收端 CA 指纹无效")
    return normalized


class TransferServiceAdvertiser:
    """Advertise one stable receiver identity and follow IPv4 network changes."""

    def __init__(
        self,
        *,
        receiver_id: str,
        port: int,
        address_provider: Callable[[], Iterable[str]],
        before_address_update: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        self.receiver_id = receiver_id_from_fingerprint(receiver_id)
        self.port = int(port)
        self._address_provider = address_provider
        self._before_address_update = before_address_update
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._zeroconf: Any = None
        self._service_info: Any = None
        self._addresses: tuple[str, ...] = ()
        self._lock = threading.Lock()

    @property
    def service_name(self) -> str:
        return f"AllDayRecording-{self.receiver_id[:12]}"

    def start(self) -> bool:
        addresses = self._current_addresses()
        if not addresses:
            return False
        with self._lock:
            if self._thread is not None:
                return True
            self._register(addresses)
            self._addresses = addresses
            self._thread = threading.Thread(
                target=self._monitor,
                name="transfer-mdns-monitor",
                daemon=True,
            )
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lock:
            self._close_registration()
            self._thread = None

    def _monitor(self) -> None:
        while not self._stop.wait(NETWORK_POLL_SECONDS):
            addresses = self._current_addresses()
            if not addresses or addresses == self._addresses:
                continue
            try:
                if self._before_address_update is not None:
                    self._before_address_update(addresses)
                with self._lock:
                    self._close_registration()
                    self._register(addresses)
                    self._addresses = addresses
                print(f"[transfer] 网络已变化，自动发现地址更新为：{', '.join(addresses)}")
            except Exception as exc:
                print(f"[transfer] 更新网络发现失败，将自动重试：{exc}")

    def _register(self, addresses: tuple[str, ...]) -> None:
        try:
            from zeroconf import IPVersion, ServiceInfo, Zeroconf
        except ImportError as exc:
            raise RuntimeError(
                "缺少 zeroconf 依赖，无法启动电脑自动发现"
            ) from exc
        hostname = f"allday-{self.receiver_id[:12]}.local."
        properties = {
            "protocol": SERVICE_PROTOCOL,
            "version": SERVICE_VERSION,
            "receiver_id": self.receiver_id,
            "transport": "tls",
        }
        info = ServiceInfo(
            SERVICE_TYPE,
            f"{self.service_name}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(address) for address in addresses],
            port=self.port,
            properties=properties,
            server=hostname,
        )
        zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        try:
            zeroconf.register_service(info, allow_name_change=False)
        except Exception:
            zeroconf.close()
            raise
        self._zeroconf = zeroconf
        self._service_info = info

    def _close_registration(self) -> None:
        zeroconf = self._zeroconf
        info = self._service_info
        self._zeroconf = None
        self._service_info = None
        if zeroconf is None:
            return
        try:
            if info is not None:
                zeroconf.unregister_service(info)
        finally:
            zeroconf.close()

    def _current_addresses(self) -> tuple[str, ...]:
        result: set[str] = set()
        for value in self._address_provider():
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if (
                address.version == 4
                and not address.is_loopback
                and not address.is_link_local
                and not address.is_unspecified
            ):
                result.add(str(address))
        return tuple(sorted(result))
