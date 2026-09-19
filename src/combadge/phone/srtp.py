"""SRTP AES_CM_128_HMAC_SHA1_80 using the system OpenSSL implementation.

RFC 3711 with SDES keys, zero key-derivation rate, and no MKI. One context per
SSRC/direction; authentication and replay checks precede payload decryption.
"""

import ctypes
import hashlib
import hmac
import struct
from functools import lru_cache


@lru_cache(maxsize=1)
def crypto():
    for name in ("libcrypto.so.3", "libcrypto.so"):
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            continue
    else:
        raise RuntimeError("Direct calls need system OpenSSL libcrypto and Python ctypes")
    pointer = ctypes.c_void_p
    lib.EVP_CIPHER_CTX_new.argtypes = []
    lib.EVP_CIPHER_CTX_new.restype = pointer
    lib.EVP_CIPHER_CTX_free.argtypes = [pointer]
    lib.EVP_CIPHER_CTX_free.restype = None
    lib.EVP_aes_128_ctr.argtypes = []
    lib.EVP_aes_128_ctr.restype = pointer
    lib.EVP_EncryptInit_ex.argtypes = [pointer, pointer, pointer, pointer, pointer]
    lib.EVP_EncryptInit_ex.restype = ctypes.c_int
    lib.EVP_EncryptUpdate.argtypes = [
        pointer,
        pointer,
        ctypes.POINTER(ctypes.c_int),
        pointer,
        ctypes.c_int,
    ]
    lib.EVP_EncryptUpdate.restype = ctypes.c_int
    return lib


def aes_ctr(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(key) != 16 or len(iv) != 16:
        raise ValueError("AES-128 requires a 16-byte key and counter")
    lib = crypto()
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise RuntimeError("OpenSSL could not allocate a cipher context")
    try:
        out = ctypes.create_string_buffer(len(data) + 16)
        size = ctypes.c_int()
        if lib.EVP_EncryptInit_ex(ctx, lib.EVP_aes_128_ctr(), None, key, iv) != 1:
            raise RuntimeError("OpenSSL AES initialization failed")
        if lib.EVP_EncryptUpdate(ctx, out, ctypes.byref(size), data, len(data)) != 1:
            raise RuntimeError("OpenSSL AES operation failed")
        return out.raw[: size.value]
    finally:
        lib.EVP_CIPHER_CTX_free(ctx)


def derive(master: bytes, label: int, size: int) -> bytes:
    if len(master) != 30:
        raise ValueError("SDES key must contain 16 key bytes and 14 salt bytes")
    # RFC 3711 section 4.3: label XOR salt, then multiply by 2**16.
    iv = (int.from_bytes(master[16:], "big") ^ (label << 48)) << 16
    return aes_ctr(master[:16], iv.to_bytes(16, "big"), bytes(size))


def header_size(packet: bytes) -> int:
    if len(packet) < 12 or packet[0] >> 6 != 2:
        raise ValueError("Invalid RTP header")
    size = 12 + 4 * (packet[0] & 15)
    if len(packet) < size:
        raise ValueError("Truncated RTP CSRCs")
    if packet[0] & 16:
        if len(packet) < size + 4:
            raise ValueError("Truncated RTP extension")
        size += 4 + 4 * int.from_bytes(packet[size + 2 : size + 4], "big")
    if len(packet) < size:
        raise ValueError("Truncated RTP header")
    return size


class Srtp:
    def __init__(self, master: bytes):
        self.key = derive(master, 0, 16)
        self.auth = derive(master, 1, 20)
        self.salt = int.from_bytes(derive(master, 2, 14), "big") << 16
        self.highest = -1
        self.window = 0
        self.ssrc = None

    def _index(self, sequence):
        if self.highest < 0:
            return sequence
        index = (self.highest & ~65535) | sequence
        if index < self.highest - 32768:
            index += 65536
        elif index > self.highest + 32768:
            index -= 65536
        if not 0 <= index < 1 << 48:
            raise ValueError("RTP index outside session lifetime")
        return index

    def _crypt(self, packet, index):
        size = header_size(packet)
        ssrc = int.from_bytes(packet[8:12], "big")
        iv = self.salt ^ (ssrc << 64) ^ (index << 16)
        return packet[:size] + aes_ctr(self.key, iv.to_bytes(16, "big"), packet[size:])

    def protect(self, packet: bytes) -> bytes:
        header_size(packet)
        ssrc = packet[8:12]
        index = self._index(int.from_bytes(packet[2:4], "big"))
        if index <= self.highest or (self.ssrc is not None and ssrc != self.ssrc):
            raise ValueError("SRTP sender must use a single SSRC and increasing packet indices")
        encrypted = self._crypt(packet, index)
        tag = hmac.digest(self.auth, encrypted + struct.pack("!I", index >> 16), "sha1")[:10]
        self.highest, self.ssrc = index, ssrc
        return encrypted + tag

    def unprotect(self, packet: bytes) -> bytes:
        if len(packet) < 22:
            raise ValueError("Truncated SRTP packet")
        encrypted, tag = packet[:-10], packet[-10:]
        header_size(encrypted)
        ssrc = encrypted[8:12]
        if self.ssrc is not None and ssrc != self.ssrc:
            raise ValueError("Unexpected SRTP SSRC")
        index = self._index(int.from_bytes(encrypted[2:4], "big"))
        age = self.highest - index
        if age >= 64 or (age >= 0 and self.window & (1 << age)):
            raise ValueError("Replayed or expired SRTP packet")
        expected = hmac.new(
            self.auth, encrypted + struct.pack("!I", index >> 16), hashlib.sha1
        ).digest()[:10]
        if not hmac.compare_digest(tag, expected):
            raise ValueError("SRTP authentication failed")
        plain = self._crypt(encrypted, index)
        if index > self.highest:
            self.window = ((self.window << min(index - self.highest, 64)) | 1) & ((1 << 64) - 1)
            self.highest = index
        else:
            self.window |= 1 << age
        self.ssrc = ssrc
        return plain
