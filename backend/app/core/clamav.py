"""ClamAV stream scanning over the clamd INSTREAM protocol (no extra dependency).

Every client upload and every downloaded tender pack is scanned BEFORE it is written to S3.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

from ..config import get_settings

CHUNK = 64 * 1024


class VirusDetected(Exception):
    def __init__(self, signature: str):
        super().__init__(f"Malware detected: {signature}")
        self.signature = signature


class ScannerUnavailable(Exception):
    pass


@dataclass
class ScanResult:
    status: str  # CLEAN | SKIPPED_DEV
    detail: str = ""


def _instream(data: bytes, host: str, port: int, timeout: float = 30.0) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(b"zINSTREAM\0")
        for i in range(0, len(data), CHUNK):
            chunk = data[i : i + CHUNK]
            sock.sendall(struct.pack("!L", len(chunk)) + chunk)
        sock.sendall(struct.pack("!L", 0))
        reply = b""
        while not reply.endswith(b"\0"):
            part = sock.recv(4096)
            if not part:
                break
            reply += part
    return reply.rstrip(b"\0").decode(errors="replace")


def ping(host: str | None = None, port: int | None = None) -> bool:
    s = get_settings()
    try:
        with socket.create_connection((host or s.clamav_host, port or s.clamav_port), timeout=5) as sock:
            sock.sendall(b"zPING\0")
            return sock.recv(16).startswith(b"PONG")
    except OSError:
        return False


def scan_bytes(data: bytes) -> ScanResult:
    s = get_settings()
    try:
        reply = _instream(data, s.clamav_host, s.clamav_port)
    except OSError as exc:
        if s.av_required:
            raise ScannerUnavailable(f"ClamAV unreachable at {s.clamav_host}:{s.clamav_port}: {exc}") from exc
        return ScanResult("SKIPPED_DEV", f"clamd unreachable ({exc}); accepted in development only")
    # Replies: "stream: OK" | "stream: Eicar-Signature FOUND" | "... ERROR"
    if reply.endswith("FOUND"):
        raise VirusDetected(reply.split(":", 1)[-1].replace("FOUND", "").strip())
    if not reply.endswith("OK"):
        raise ScannerUnavailable(f"Unexpected clamd reply: {reply}")
    return ScanResult("CLEAN", reply)
