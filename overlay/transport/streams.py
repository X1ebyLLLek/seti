"""Абстракция байтового потока.

Один и тот же код кадрирования и TLS работает поверх разных потоков:
  * PlainStream — TCP-соединение asyncio;
  * TlsStream   — TLS 1.3 через ssl.MemoryBIO поверх любого другого потока;
  * TunnelStream (overlay.tunnel) — байтовый поток через цепочку ретрансляторов.
write() синхронный и сохраняет порядок; drain() ожидает отправки буфера.
"""
from __future__ import annotations

import asyncio
import ssl

READ_CHUNK = 65536


class ByteStream:
    peer_desc = "?"

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    async def drain(self) -> None:
        raise NotImplementedError

    async def recv(self) -> bytes:
        """Очередная порция байтов; b"" — конец потока."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def is_closing(self) -> bool:
        raise NotImplementedError


class PlainStream(ByteStream):
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        peer = writer.get_extra_info("peername")
        self.peer_desc = f"{peer[0]}:{peer[1]}" if peer else "?"

    def write(self, data: bytes) -> None:
        if not self.writer.is_closing():
            self.writer.write(data)

    async def drain(self) -> None:
        if not self.writer.is_closing():
            await self.writer.drain()

    async def recv(self) -> bytes:
        try:
            return await self.reader.read(READ_CHUNK)
        except (ConnectionError, OSError):
            return b""

    def close(self) -> None:
        if not self.writer.is_closing():
            self.writer.close()

    def is_closing(self) -> bool:
        return self.writer.is_closing()


class TlsStream(ByteStream):
    """TLS поверх произвольного ByteStream (MemoryBIO): используется и для
    межузловых TCP-соединений, и для end-to-end сессии внутри туннеля."""

    def __init__(self, inner: ByteStream, ctx: ssl.SSLContext, server_side: bool):
        self.inner = inner
        self.incoming = ssl.MemoryBIO()
        self.outgoing = ssl.MemoryBIO()
        self.obj = ctx.wrap_bio(self.incoming, self.outgoing, server_side=server_side)
        self.peer_desc = inner.peer_desc
        self._closed = False

    def _flush(self) -> None:
        data = self.outgoing.read()
        if data:
            self.inner.write(data)

    def feed_raw(self, data: bytes) -> None:
        """Байты, уже прочитанные из inner до начала TLS."""
        if data:
            self.incoming.write(data)

    async def handshake(self) -> None:
        while True:
            try:
                self.obj.do_handshake()
                self._flush()
                await self.inner.drain()
                return
            except ssl.SSLWantReadError:
                self._flush()
                await self.inner.drain()
                data = await self.inner.recv()
                if not data:
                    raise ConnectionError("connection closed during TLS handshake")
                self.incoming.write(data)

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        self.obj.write(data)
        self._flush()

    async def drain(self) -> None:
        await self.inner.drain()

    async def recv(self) -> bytes:
        while True:
            try:
                data = self.obj.read(READ_CHUNK)
                self._flush()
                return data
            except ssl.SSLWantReadError:
                self._flush()
                raw = await self.inner.recv()
                if not raw:
                    return b""
                self.incoming.write(raw)
            except ssl.SSLZeroReturnError:
                return b""

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.obj.unwrap()
            except ssl.SSLError:
                pass
            self._flush()
            self.inner.close()

    def is_closing(self) -> bool:
        return self._closed or self.inner.is_closing()

    @property
    def version(self) -> str | None:
        return self.obj.version()

    @property
    def cipher(self) -> str | None:
        c = self.obj.cipher()
        return c[0] if c else None
