"""Minimal Windows DNSAPI mDNS service registration, bound to one interface."""
from __future__ import annotations

import ctypes
import socket
import threading
from ctypes import wintypes


_REGISTER_COMPLETE = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
_DNS_QUERY_REQUEST_VERSION1 = 1
_DNS_REQUEST_PENDING = 9506


class _RegisterRequest(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.ULONG),
        ("InterfaceIndex", wintypes.ULONG),
        ("pServiceInstance", ctypes.c_void_p),
        ("pRegisterCompletionCallback", ctypes.c_void_p),
        ("pQueryContext", ctypes.c_void_p),
        ("hCredentials", ctypes.c_void_p),
        ("unicastEnabled", wintypes.BOOL),
    ]


class Registration:
    """Owns DNSAPI buffers and deregisters on close/process exit."""

    def __init__(self, address: str, interface_index: int, computer_id: str, display_name: str, port: int):
        if not hasattr(ctypes, "WinDLL"):
            raise OSError("Windows DNSAPI is unavailable")
        if interface_index <= 0:
            raise ValueError("A physical LAN interface index is required for mDNS")
        self._dns = ctypes.WinDLL("dnsapi.dll", use_last_error=True)
        self._closed = False
        self._register_event = threading.Event()
        self._deregister_event = threading.Event()
        self._register_status: int | None = None

        self._dns.DnsServiceConstructInstance.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p,
            wintypes.WORD, wintypes.WORD, wintypes.WORD, wintypes.DWORD,
            ctypes.POINTER(wintypes.LPCWSTR), ctypes.POINTER(wintypes.LPCWSTR),
        ]
        self._dns.DnsServiceConstructInstance.restype = ctypes.c_void_p
        self._dns.DnsServiceRegister.argtypes = [ctypes.POINTER(_RegisterRequest), ctypes.c_void_p]
        self._dns.DnsServiceRegister.restype = wintypes.DWORD
        self._dns.DnsServiceDeRegister.argtypes = [ctypes.POINTER(_RegisterRequest), ctypes.c_void_p]
        self._dns.DnsServiceDeRegister.restype = wintypes.DWORD
        self._dns.DnsServiceFreeInstance.argtypes = [ctypes.c_void_p]
        self._dns.DnsServiceFreeInstance.restype = None

        keys = (wintypes.LPCWSTR * 3)("computer_id", "protocol", "display_name")
        values = (wintypes.LPCWSTR * 3)(computer_id, "1", display_name[:64])
        address_bytes = socket.inet_aton(address)
        ip4 = wintypes.ULONG.from_buffer_copy(address_bytes)
        instance_name = f"SendToCopyQ-{computer_id}._sendtocopyq._tcp.local"
        # DNSAPI advertises SRV/TXT, but does not supply address records for
        # our synthetic host alias. Use Windows' dynamically obtained host name.
        dns_host = socket.gethostname().rstrip(".").split(".")[0].lower()
        if not dns_host or len(dns_host) > 63 or any(
            char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in dns_host
        ):
            raise ValueError("Windows DNS host name is not a valid mDNS label")
        host_name = f"{dns_host}.local"
        self._instance = self._dns.DnsServiceConstructInstance(
            instance_name, host_name, ctypes.byref(ip4), None, port, 0, 0, 3, keys, values
        )
        if not self._instance:
            raise ctypes.WinError(ctypes.get_last_error())

        def completed(status: int, _context: int, returned_instance: int) -> None:
            if returned_instance:
                self._dns.DnsServiceFreeInstance(returned_instance)
            self._register_status = int(status)
            self._register_event.set()

        self._register_callback = _REGISTER_COMPLETE(completed)
        self._request = _RegisterRequest(
            _DNS_QUERY_REQUEST_VERSION1, interface_index, self._instance,
            ctypes.cast(self._register_callback, ctypes.c_void_p), None, None, False,
        )
        result = self._dns.DnsServiceRegister(ctypes.byref(self._request), None)
        if result != _DNS_REQUEST_PENDING:
            self._dns.DnsServiceFreeInstance(self._instance)
            self._instance = None
            raise ctypes.WinError(result)

    def wait_registered(self, timeout: float = 5.0) -> bool:
        if not self._register_event.wait(timeout):
            return False
        return self._register_status == 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        def completed(_status: int, _context: int, returned_instance: int) -> None:
            if returned_instance:
                self._dns.DnsServiceFreeInstance(returned_instance)
            self._deregister_event.set()

        self._deregister_callback = _REGISTER_COMPLETE(completed)
        self._request.pRegisterCompletionCallback = ctypes.cast(self._deregister_callback, ctypes.c_void_p)
        try:
            result = self._dns.DnsServiceDeRegister(ctypes.byref(self._request), None)
            if result == _DNS_REQUEST_PENDING:
                self._deregister_event.wait(3.0)
        finally:
            if self._instance:
                self._dns.DnsServiceFreeInstance(self._instance)
                self._instance = None
