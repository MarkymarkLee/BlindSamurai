"""Bluetooth link to an El Jefe control module.

El Jefe passes Modbus RTU frames between its Nordic UART service and the dots, so
BleSerial only has to look like a pyserial port for minimalmodbus to use it unchanged.
"""
import asyncio
import threading
import time
from typing import Dict, List, Optional

from bleak import BleakClient, BleakScanner

NAME_PREFIX = "DataFeel"
UART_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # we write requests here
UART_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # responses arrive as notifications

# bleak is async; run it on one background loop and block on it from the sync Modbus code.
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True, name="datafeel-ble").start()


def _run(coro, timeout):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


class BleSerial:
    def __init__(self, device, timeout=0.5):
        self.device = device
        self.port = device.name or device.address  # minimalmodbus keys its timing state on this
        self.timeout = timeout
        self.baudrate = 115200
        self._buf = bytearray()
        self._cond = threading.Condition()
        self._client = None
        self.open()

    def _on_data(self, _, data: bytearray):
        with self._cond:
            self._buf += data
            self._cond.notify_all()

    async def _connect(self):
        self._client = BleakClient(self.device)
        await self._client.connect()
        await self._client.start_notify(UART_TX, self._on_data)
        # Right after subscribing, El Jefe sometimes drops the first replies.
        await asyncio.sleep(0.3)

    @property
    def is_open(self):
        return self._client is not None and self._client.is_connected

    def open(self):
        if not self.is_open:
            _run(self._connect(), 20)

    def close(self):
        if self.is_open:
            _run(self._client.disconnect(), 10)

    def write(self, data):
        _run(self._client.write_gatt_char(UART_RX, bytes(data), response=False), 5)
        return len(data)

    def read(self, size):
        # Like pyserial: return once `size` bytes are in, or whatever arrived by the timeout.
        deadline = time.monotonic() + self.timeout
        with self._cond:
            while len(self._buf) < size:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            out = bytes(self._buf[:size])
            del self._buf[:size]
        return out

    def reset_input_buffer(self):
        with self._cond:
            self._buf.clear()

    def reset_output_buffer(self):
        pass

    def flush(self):
        pass


# A connected El Jefe stops advertising, so keep links open across rescans.
_links: Dict[str, BleSerial] = {}


def discover_links(names: Optional[List[str]] = None, scan_seconds=5.0) -> List[BleSerial]:
    """
    Return a link to every El Jefe in range, reusing ones that are already connected.
    If names is given, only El Jefes advertising one of those names are used.
    """
    found = _run(BleakScanner.discover(timeout=scan_seconds, return_adv=True), scan_seconds + 10)
    for device, adv in found.values():
        name = device.name or adv.local_name or ""
        wanted = name in names if names else name.startswith(NAME_PREFIX)
        if wanted and device.address not in _links:
            try:
                _links[device.address] = BleSerial(device)
            except Exception as e:
                print(f"Could not connect to {name}: {e}")
    for address, link in list(_links.items()):
        if not link.is_open:
            try:
                link.open()
            except Exception:
                del _links[address]
    return list(_links.values())


def close_all():
    for link in _links.values():
        try:
            link.close()
        except Exception:
            pass
    _links.clear()
