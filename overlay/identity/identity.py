"""Долговременная криптографическая идентичность узла (Ed25519) и NodeID.

    canonical_encode(pk) = ASCII("ed25519") || 0x00 || pk_raw
    NodeID              = SHA-256(canonical_encode(pk))

pk_raw — 32-байтовое кодирование открытого ключа Ed25519 по RFC 8032 §5.1.5
(как его возвращает Encoding.Raw). NodeID сравнивается как 32 байта.
Закрытый ключ хранится только в NODE_STATE_DIR/identity_ed25519.pem (PKCS#8),
по сети не передаётся и в журнал не пишется.
"""
from __future__ import annotations

import datetime
import hashlib
import os

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

ALGORITHM = "ed25519"
KEY_FILE = "identity_ed25519.pem"
CERT_FILE = "tls_cert.pem"


def canonical_encode(public_key: bytes, algorithm: str = ALGORITHM) -> bytes:
    if algorithm != ALGORITHM:
        raise ValueError(f"unsupported identity algorithm {algorithm!r}")
    if len(public_key) != 32:
        raise ValueError("ed25519 public key must be 32 bytes")
    return algorithm.encode("ascii") + b"\x00" + public_key


def node_id_from_public_key(public_key: bytes, algorithm: str = ALGORITHM) -> bytes:
    return hashlib.sha256(canonical_encode(public_key, algorithm)).digest()


def verify_signature(public_key: bytes, signature: bytes, data: bytes) -> bool:
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public_key).verify(signature, data)
        return True
    except (InvalidSignature, ValueError):
        return False


def is_valid_ed25519_public_key(public_key: bytes) -> bool:
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        return False
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public_key)
        return True
    except ValueError:
        return False


class Identity:
    def __init__(self, private_key: ed25519.Ed25519PrivateKey, state_dir: str | None, created: bool):
        self._sk = private_key
        self.public_key = private_key.public_key().public_bytes(serialization.Encoding.Raw,
                                                                 serialization.PublicFormat.Raw)
        self.algorithm = ALGORITHM
        self.node_id = node_id_from_public_key(self.public_key)
        self.state_dir = state_dir
        self.created = created
        self._cert_der: bytes | None = None

    # ------------------------------------------------------------------ загрузка
    @classmethod
    def load_or_create(cls, state_dir: str) -> "Identity":
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, KEY_FILE)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                sk = serialization.load_pem_private_key(fh.read(), password=None)
            if not isinstance(sk, ed25519.Ed25519PrivateKey):
                raise ValueError("identity key is not Ed25519")
            return cls(sk, state_dir, created=False)
        sk = ed25519.Ed25519PrivateKey.generate()          # CSPRNG (OpenSSL)
        pem = sk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
        return cls(sk, state_dir, created=True)

    @classmethod
    def ephemeral(cls) -> "Identity":
        """Идентичность без каталога состояния (для unit-тестов)."""
        return cls(ed25519.Ed25519PrivateKey.generate(), None, created=True)

    # ------------------------------------------------------------------ подпись
    def sign(self, data: bytes) -> bytes:
        return self._sk.sign(data)

    # ------------------------------------------------------------------ TLS
    def certificate_der(self) -> bytes:
        """Самоподписанный X.509-сертификат на identity-ключе. Ему доверяют не потому,
        что он самоподписан, а потому что SHA-256(canonical_encode(pk)) == NodeID."""
        if self._cert_der is None:
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.node_id.hex())])
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(self._sk.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - datetime.timedelta(hours=1))
                    .not_valid_after(now + datetime.timedelta(days=30))
                    .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                    .sign(self._sk, None))
            self._cert_der = cert.public_bytes(serialization.Encoding.DER)
        return self._cert_der

    def tls_files(self) -> tuple[str, str]:
        """Пути к сертификату и ключу для ssl.SSLContext.load_cert_chain (только в NODE_STATE_DIR)."""
        if self.state_dir is None:
            raise RuntimeError("TLS requires a state directory")
        cert_path = os.path.join(self.state_dir, CERT_FILE)
        der = self.certificate_der()
        pem = x509.load_der_x509_certificate(der).public_bytes(serialization.Encoding.PEM)
        if not os.path.exists(cert_path) or open(cert_path, "rb").read() != pem:
            with open(cert_path, "wb") as fh:
                fh.write(pem)
        return cert_path, os.path.join(self.state_dir, KEY_FILE)

    def write_tmp_key(self, directory: str) -> str:
        """Для эфемерных идентичностей в тестах: ключ во временном каталоге теста."""
        path = os.path.join(directory, KEY_FILE)
        with open(path, "wb") as fh:
            fh.write(self._sk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
        self.state_dir = directory
        return path

    def __repr__(self) -> str:                      # закрытый ключ не печатается
        return f"Identity(node_id={self.node_id.hex()[:16]}...)"


def public_key_from_certificate(der: bytes) -> bytes:
    cert = x509.load_der_x509_certificate(der)
    pk = cert.public_key()
    if not isinstance(pk, ed25519.Ed25519PublicKey):
        raise ValueError("certificate key is not Ed25519")
    return pk.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
