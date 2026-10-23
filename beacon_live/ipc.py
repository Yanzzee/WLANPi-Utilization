"""Bounded JSON messages and typed publications for trusted local consumers."""

from __future__ import annotations

import json
import math
import socket
import struct
import types
from dataclasses import fields, is_dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Union, get_args, get_origin, get_type_hints

MAX_MESSAGE_BYTES = 8 * 1024 * 1024
PROTOCOL_VERSION = 1
DEFAULT_SOCKET = Path("/run/wlanpi-utilization/service.sock")


def to_wire(value):
    if is_dataclass(value):
        return {field.name: to_wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [to_wire(item) for item in value]
    if isinstance(value, dict):
        return {key: to_wire(item) for key, item in value.items()}
    return value


@lru_cache(maxsize=64)
def _hints(cls):
    return get_type_hints(cls)


def from_wire(cls, value):
    """Rebuild only declared types; do not deserialize executable objects."""
    origin, args = get_origin(cls), get_args(cls)
    if origin in (types.UnionType, Union):
        for candidate in args:
            try:
                return from_wire(candidate, value)
            except (ValueError, TypeError):
                pass
        raise ValueError(f"Invalid value for {cls}")
    if cls is type(None):
        if value is not None:
            raise ValueError("Expected null")
        return None
    if origin is tuple:
        if not isinstance(value, list):
            raise ValueError("Expected an array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(from_wire(args[0], item) for item in value)
        if len(value) != len(args):
            raise ValueError("Invalid tuple length")
        return tuple(from_wire(kind, item) for kind, item in zip(args, value))
    if isinstance(cls, type) and issubclass(cls, Enum):
        return cls(value)
    if cls is Path:
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("Expected a path string")
        return Path(value)
    if is_dataclass(cls):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ValueError(f"Invalid fields for {cls.__name__}")
        hints = _hints(cls)
        return cls(**{key: from_wire(hints[key], item) for key, item in value.items()})
    if cls is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Expected a finite number")
        return float(value)
    if cls in (str, int, bool):
        if type(value) is not cls:
            raise ValueError(f"Expected {cls.__name__}")
        return value
    raise ValueError(f"Unsupported IPC type {cls}")


def encode(message: dict) -> bytes:
    payload = json.dumps(to_wire(message), allow_nan=False, separators=(",", ":")).encode()
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("Utilization IPC message exceeds the size limit")
    return struct.pack("!I", len(payload)) + payload


def decode(payload: bytes) -> dict:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON message object")
    return value


def receive_blocking(sock: socket.socket) -> dict:
    def exact(count):
        chunks = bytearray()
        while len(chunks) < count:
            chunk = sock.recv(count - len(chunks))
            if not chunk:
                raise EOFError("Utilization service disconnected")
            chunks.extend(chunk)
        return bytes(chunks)

    size = struct.unpack("!I", exact(4))[0]
    if not 0 < size <= MAX_MESSAGE_BYTES:
        raise ValueError("Invalid Utilization IPC message size")
    return decode(exact(size))


class FramedSocket:
    """Nonblocking channel with at most a partial write and one latest message."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        sock.setblocking(False)
        self.buffer = bytearray()
        self.output = b""
        self.offset = 0
        self.next_output = b""
        self.eof = False

    @property
    def writing(self):
        return bool(self.output or self.next_output)

    def queue(self, message):
        payload = encode(message)
        if not self.output or self.offset == 0:
            self.output, self.offset = payload, 0
        else:
            self.next_output = payload

    def flush(self):
        if not self.output:
            return
        try:
            count = self.sock.send(memoryview(self.output)[self.offset:])
        except BlockingIOError:
            return
        if count == 0:
            raise EOFError("Utilization IPC write closed")
        self.offset += count
        if self.offset == len(self.output):
            self.output, self.next_output, self.offset = self.next_output, b"", 0

    def receive(self):
        messages = []
        # A fixed byte budget keeps slow or noisy clients out of the owner loop.
        for _ in range(4):
            try:
                chunk = self.sock.recv(65536)
            except BlockingIOError:
                break
            if not chunk:
                self.eof = True
                break
            self.buffer.extend(chunk)
            while len(self.buffer) >= 4:
                size = struct.unpack("!I", self.buffer[:4])[0]
                if not 0 < size <= MAX_MESSAGE_BYTES:
                    raise ValueError("Invalid Utilization IPC message size")
                if len(self.buffer) < size + 4:
                    break
                messages.append(decode(bytes(self.buffer[4:size + 4])))
                del self.buffer[:size + 4]
        if self.eof and self.buffer:
            raise ValueError("Incomplete Utilization IPC message")
        return messages
