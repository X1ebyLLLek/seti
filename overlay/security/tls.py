"""Защищённый канал: TLS 1.3 со взаимной аутентификацией и привязкой к NodeID.

Схема (подробно — PROTOCOL.md, раздел «Защищённая сессия»):
 1. Инициатор отправляет открытый кадр SECURE_HELLO {node_id, certificate, nonce, purpose}.
 2. Ответчик проверяет: ключ сертификата — Ed25519 и SHA-256(canonical_encode(pk)) == node_id;
    отвечает SECURE_HELLO_ACK {node_id, certificate, nonce}.
 3. Инициатор проверяет то же и, если цель известна заранее, что node_id == ожидаемому NodeID.
 4. TLS 1.3 (только 1.3, без session tickets и 0-RTT): каждая сторона создаёт контекст,
    доверяющий ровно одному сертификату — закреплённому сертификату собеседника
    (CERT_REQUIRED). Самоподписанный сертификат принимается не потому, что он
    самоподписан, а потому что его ключ хэшируется в ожидаемый/заявленный NodeID,
    а владение закрытым ключом доказывается подписью CertificateVerify.
 5. После рукопожатия: версия == TLSv1.3 и сертификат собеседника побайтно равен закреплённому.
 6. Подтверждение транскрипта: каждая сторона первыми байтами внутри TLS отправляет
    T = SHA-256("overlay-hello-transcript-v1" || payload(SECURE_HELLO) || payload(SECURE_HELLO_ACK))
    и сверяет значение собеседника. Так открытые HELLO-кадры (nonce, purpose) оказываются
    под защитой аутентифицированного канала: их подмена MITM обнаруживается.
session_id = SHA-256("overlay-session-v1" || nonce_c || nonce_s)[:16].
Защиту от replay внутри сессии и между сессиями обеспечивают номера записей TLS и
свежие ephemeral-ключи каждого рукопожатия; прикладная дедупликация — по message_id.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import ssl
from dataclasses import dataclass

from ..identity import Identity, node_id_from_public_key, public_key_from_certificate
from ..metrics import NULL_LOG, short
from ..protocol import FrameDecoder, MsgType, ProtocolError, decode_payload, encode_frame, encode_payload
from ..protocol.frame import Frame, new_request_id
from ..transport.streams import ByteStream, TlsStream


class SecureHandshakeError(Exception):
    pass


class IdentityMismatch(SecureHandshakeError):
    pass


@dataclass
class SecureResult:
    stream: TlsStream
    peer_node_id: bytes
    peer_public_key: bytes
    session_id: bytes
    version: str
    cipher: str


async def _read_one_frame(stream: ByteStream, max_payload: int = 4096) -> tuple[Frame, bytes]:
    dec = FrameDecoder(max_payload)
    while True:
        item = dec.next()
        if isinstance(item, Frame):
            rest = bytes(dec._buf)
            return item, rest
        if isinstance(item, ProtocolError):
            raise SecureHandshakeError(f"bad hello frame: {item.code}")
        data = await stream.recv()
        if not data:
            raise SecureHandshakeError("connection closed during hello")
        dec.feed(data)


def _check_binding(node_id: bytes, cert_der: bytes) -> bytes:
    try:
        pk = public_key_from_certificate(cert_der)
    except Exception as exc:
        raise SecureHandshakeError(f"bad certificate: {type(exc).__name__}") from None
    if node_id_from_public_key(pk) != node_id:
        raise IdentityMismatch("identity mismatch: certificate key does not hash to claimed NodeID")
    return pk


class SecurityProvider:
    def __init__(self, identity: Identity, log=NULL_LOG):
        self.identity = identity
        self.log = log
        self.cert_der = identity.certificate_der()
        self.cert_path, self.key_path = identity.tls_files()

    def _context(self, server_side: bool, peer_cert_der: bytes) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server_side else ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3        # TLS 1.2 и ниже отключены
        ctx.maximum_version = ssl.TLSVersion.TLSv1_3
        ctx.options |= ssl.OP_NO_TICKET                     # нет возобновления -> нет 0-RTT
        ctx.load_cert_chain(self.cert_path, self.key_path)
        ctx.check_hostname = False                          # имя не используется: проверяется NodeID
        ctx.verify_mode = ssl.CERT_REQUIRED                 # взаимная аутентификация
        ctx.load_verify_locations(cadata=peer_cert_der)     # доверие только закреплённому ключу
        return ctx

    @staticmethod
    def session_id(nonce_c: bytes, nonce_s: bytes) -> bytes:
        return hashlib.sha256(b"overlay-session-v1" + nonce_c + nonce_s).digest()[:16]

    async def secure_client(self, stream: ByteStream, expected_node_id: bytes | None,
                            purpose: str = "link") -> SecureResult:
        nonce_c = os.urandom(16)
        hello = {"node_id": self.identity.node_id, "certificate": self.cert_der, "nonce": nonce_c,
                 "purpose": purpose}
        hello_raw = encode_payload(MsgType.SECURE_HELLO, hello)
        stream.write(encode_frame(Frame(MsgType.SECURE_HELLO, new_request_id(), hello_raw)))
        await stream.drain()
        frame, rest = await _read_one_frame(stream)
        if frame.type == MsgType.ERROR:
            body = decode_payload(MsgType.ERROR, frame.payload)
            raise SecureHandshakeError(f"peer refused: {body['code']}")
        if frame.type != MsgType.SECURE_HELLO_ACK:
            raise SecureHandshakeError("expected SECURE_HELLO_ACK")
        ack = decode_payload(MsgType.SECURE_HELLO_ACK, frame.payload)
        if expected_node_id is not None and ack["node_id"] != expected_node_id:
            raise IdentityMismatch("identity mismatch: peer NodeID differs from expected")
        peer_pk = _check_binding(ack["node_id"], ack["certificate"])
        tls = TlsStream(stream, self._context(False, ack["certificate"]), server_side=False)
        tls.feed_raw(rest)
        transcript = self.transcript(hello_raw, frame.payload)
        return await self._finish(tls, ack["node_id"], peer_pk, ack["certificate"], nonce_c, ack["nonce"], purpose,
                                  transcript)

    async def secure_server(self, stream: ByteStream, purpose: str = "link") -> SecureResult:
        frame, rest = await _read_one_frame(stream)
        if frame.type != MsgType.SECURE_HELLO:
            raise SecureHandshakeError("expected SECURE_HELLO")
        hello = decode_payload(MsgType.SECURE_HELLO, frame.payload)
        if hello["purpose"] != purpose:
            raise SecureHandshakeError("unexpected session purpose")
        peer_pk = _check_binding(hello["node_id"], hello["certificate"])
        if hello["node_id"] == self.identity.node_id:
            raise SecureHandshakeError("peer claims our own NodeID")
        nonce_s = os.urandom(16)
        ack = {"node_id": self.identity.node_id, "certificate": self.cert_der, "nonce": nonce_s}
        ack_raw = encode_payload(MsgType.SECURE_HELLO_ACK, ack)
        stream.write(encode_frame(Frame(MsgType.SECURE_HELLO_ACK, frame.request_id, ack_raw)))
        await stream.drain()
        tls = TlsStream(stream, self._context(True, hello["certificate"]), server_side=True)
        tls.feed_raw(rest)
        return await self._finish(tls, hello["node_id"], peer_pk, hello["certificate"], hello["nonce"], nonce_s,
                                  purpose, self.transcript(frame.payload, ack_raw))

    @staticmethod
    def transcript(hello_payload: bytes, ack_payload: bytes) -> bytes:
        return hashlib.sha256(b"overlay-hello-transcript-v1" + hello_payload + ack_payload).digest()

    async def _finish(self, tls: TlsStream, peer_id: bytes, peer_pk: bytes, pinned: bytes, nonce_c: bytes,
                      nonce_s: bytes, purpose: str, transcript: bytes) -> SecureResult:
        try:
            await tls.handshake()
        except ssl.SSLError as exc:
            raise SecureHandshakeError(f"TLS handshake failed: {exc.reason or exc}") from None
        if tls.version != "TLSv1.3":
            raise SecureHandshakeError(f"unexpected TLS version {tls.version}")
        if tls.obj.getpeercert(binary_form=True) != pinned:
            raise IdentityMismatch("identity mismatch: TLS certificate differs from announced")
        tls.write(transcript)
        await tls.drain()
        got = b""
        while len(got) < len(transcript):
            try:
                chunk = await tls.recv()
            except ssl.SSLError as exc:
                raise SecureHandshakeError(f"TLS error: {exc.reason or exc}") from None
            if not chunk:
                raise SecureHandshakeError("closed before transcript confirmation")
            got += chunk
        tls.unread(got[len(transcript):])
        if not hmac.compare_digest(got[:len(transcript)], transcript):
            raise SecureHandshakeError("hello transcript mismatch (HELLO tampered)")
        sid = self.session_id(nonce_c, nonce_s)
        self.log.emit("secure.established", purpose=purpose, peer=short(peer_id), session=short(sid),
                      version=tls.version, cipher=tls.cipher)
        return SecureResult(tls, peer_id, peer_pk, sid, tls.version, tls.cipher)


async def with_timeout(coro, seconds: float):
    return await asyncio.wait_for(coro, seconds)
