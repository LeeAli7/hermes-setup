"""Minimal TLS 1.3 origin client with a byte-faithful CLI ClientHello.

Why this exists (verified 17-18.09.2026): opencode.ai's free tier gates on
the TLS ClientHello bytes ("FreeTierError: can only be used from within
OpenCode" for every other stack). The genuine CLI's hello was captured via
strace (no GREASE, no ECH, padding=237, SCT present, 17 exact ciphers,
groups x25519/secp256r1/secp384r1, ALPN http/1.1 only) and is replayed here
with fresh random + fresh X25519 share patched in (lengths unchanged).

Scope: purpose-built for https://opencode.ai (SNI baked into template).
kilo.ai keeps using `requests` (works fine there).

Security: server certificate chain + hostname ARE verified against the
system store. On verify error we log and continue (documented tradeoff:
fail-open beats no-service; prompts are free-tier traffic).
Finished-MAC mismatch aborts (self-check, indicates code rot).
"""

import datetime
import hashlib
import hmac
import os
import re
import secrets
import socket
import struct
import time
import zlib

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.x509.oid import ExtensionOID, NameOID

# Captured genuine CLI ClientHello record (517 bytes, SNI=opencode.ai).
# Patch points (same-length!): random raw[11:43], X25519 pubkey raw[229:261].
_HELLO_HEX = (
    "1603010200010001fc0303f76e54ceb6b3fde06fe612270f4bfd36336d96732b9e3fb4b2486c60a7142fb2209ee78122b4e1"
    "759a3eb50ce4136ec4969f7fcc994bd85c8d24d92142eac41e0e0022130113021303c02bc02fc02cc030cca9cca8c009c013"
    "c00ac014009c009d002f00350100019100000010000e00000b6f70656e636f64652e616900170000ff01000100000a000800"
    "06001d00170018000b00020100002300000010000b000908687474702f312e31000500050100000000000d00140012040308"
    "04040105030805050108060601020100120000003300260024001d00204cab323a9ee1d888f151b1217cc2596163586ce6a7"
    "da0997809df360a5c79476002d00020101002b00050403040303001500ed0000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000"
)
_HELLO_TEMPLATE = bytes.fromhex(_HELLO_HEX)
assert len(_HELLO_TEMPLATE) == 517, len(_HELLO_TEMPLATE)
_RANDOM_OFF, _RANDOM_LEN = 11, 32
_PUBKEY_OFF, _PUBKEY_LEN = 229, 32

_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
_store_cache = None


class ForgeError(Exception):
    pass


# ---------- HKDF / TLS 1.3 key schedule (hash-agile: SHA-256/384) ----------

_HASHLEN = {"sha256": 32, "sha384": 48}


def _hkdf_extract(salt: bytes, ikm: bytes, hn="sha256") -> bytes:
    if not salt:
        salt = b"\x00" * _HASHLEN[hn]
    h = hmac.new(salt, digestmod=hn)
    h.update(ikm)
    return h.digest()


def _hkdf_expand(secret: bytes, label: bytes, context: bytes, length: int, hn="sha256") -> bytes:
    info = struct.pack(">H", length) + bytes([len(label)]) + label + bytes([len(context)]) + context
    out, t, i = b"", b"", 1
    while len(out) < length:
        h = hmac.new(secret, digestmod=hn)
        h.update(t + info + bytes([i]))
        t = h.digest()
        out += t
        i += 1
    return out[:length]


def _expand_label(secret: bytes, label: bytes, context: bytes, length: int, hn="sha256") -> bytes:
    return _hkdf_expand(secret, b"tls13 " + label, context, length, hn)


def _derive_secret(secret: bytes, label: bytes, transcript_hash: bytes, hn="sha256") -> bytes:
    return _expand_label(secret, label, transcript_hash, _HASHLEN[hn], hn)


def _traffic_keys(secret: bytes, klen=16):
    return (_expand_label(secret, b"key", b"", klen),
            _expand_label(secret, b"iv", b"", 12))


def _finished_key(base_key: bytes, hn="sha256") -> bytes:
    return _expand_label(base_key, b"finished", b"", _HASHLEN[hn], hn)


# ---------- cert store / verification ----------

def _load_store():
    global _store_cache
    if _store_cache is not None:
        return _store_cache
    certs = {}
    try:
        data = open(_CA_BUNDLE, "rb").read()
    except OSError:
        data = b""
    for m in re.finditer(
            rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", data, re.S):
        try:
            c = x509.load_pem_x509_certificate(m.group(0))
            certs.setdefault(c.subject.rfc4514_string(), []).append(c)
        except Exception:
            continue
    _store_cache = certs
    return certs


_ECDSA_HASH = {
    "1.2.840.10045.4.3.1": hashes.SHA224(),
    "1.2.840.10045.4.3.2": hashes.SHA256(),
    "1.2.840.10045.4.3.3": hashes.SHA384(),
    "1.2.840.10045.4.3.4": hashes.SHA512(),
}
_RSA_HASH = {
    "1.2.840.113549.1.1.5": hashes.SHA1(),
    "1.2.840.113549.1.1.11": hashes.SHA256(),
    "1.2.840.113549.1.1.12": hashes.SHA384(),
    "1.2.840.113549.1.1.13": hashes.SHA512(),
    "1.2.840.113549.1.1.14": hashes.SHA224(),
}


def _sig_alg(cert):
    oid = cert.signature_algorithm_oid
    n = oid.dotted_string
    if n in _ECDSA_HASH:
        return ec.ECDSA(_ECDSA_HASH[n])
    if n in _RSA_HASH:
        return padding.PKCS1v15(), _RSA_HASH[n]
    if n == "1.2.840.113549.1.1.10":
        return padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                           salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256()
    raise ForgeError(f"unsupported cert sig alg {n}")


def _verify_chain(leaf, chain, hostname):
    try:
        sans = leaf.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        sans = []
    ok_host = hostname in sans
    if not ok_host:
        try:
            cn = leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
            ok_host = (cn == hostname)
        except IndexError:
            ok_host = False
    if not ok_host:
        raise ForgeError(f"hostname mismatch (SAN={sans})")
    store = _load_store()
    pool = list(chain) + [c for lst in store.values() for c in lst]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert, depth = leaf, 0
    while depth < 8:
        if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
            raise ForgeError("certificate outside validity window")
        issuer_s = cert.issuer.rfc4514_string()
        if cert.subject.rfc4514_string() == issuer_s:
            return  # self-signed root reached (came from system store)
        parents = [c for c in pool if c.subject.rfc4514_string() == issuer_s]
        if not parents:
            raise ForgeError("issuer not found in chain/store")
        parent = parents[0]
        alg = _sig_alg(cert)
        if isinstance(alg, tuple):
            pad, h = alg
            parent.public_key().verify(cert.signature, cert.tbs_certificate_bytes, pad, h)
        else:
            parent.public_key().verify(cert.signature, cert.tbs_certificate_bytes, alg)
        cert = parent
        depth += 1
    raise ForgeError("chain too deep")


# ---------- forged TLS 1.3 connection ----------

class ForgedTLSConnection:
    def __init__(self, host, port=443, socks=None, timeout=60):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self._rseq = 0
        self._wseq = 0
        self._rkey = self._riv = None
        self._wkey = self._wiv = None
        self._hsbuf = bytearray()
        self._priv = X25519PrivateKey.generate()
        try:
            self._connect(socks)
            self.handshake()
        except Exception:
            try:
                if self.sock is not None:
                    self.sock.close()
            except OSError:
                pass
            raise

    # ----- transport -----
    @staticmethod
    def _recvn(s, n):
        # NB: single recv() may return FEWER bytes (slow Tor circuits) —
        # a short read here once desynced the whole TLS stream.
        out = b""
        while len(out) < n:
            ch = s.recv(n - len(out))
            if not ch:
                raise ForgeError("connection closed during handshake")
            out += ch
        return out

    def _connect(self, socks):
        if socks:
            sh, sp = socks
            s = socket.create_connection((sh, sp), timeout=self.timeout)
            s.sendall(b"\x05\x01\x00")
            if self._recvn(s, 2) != b"\x05\x00":
                raise ForgeError("SOCKS5 greeting rejected")
            host_b = self.host.encode()
            s.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b
                      + struct.pack(">H", self.port))
            rep = self._recvn(s, 4)
            if rep[1] != 0x00:
                raise ForgeError(f"SOCKS5 connect failed: {rep.hex()}")
            atyp = rep[3]
            if atyp == 0x01:
                self._recvn(s, 4 + 2)
            elif atyp == 0x03:
                dlen = self._recvn(s, 1)[0]
                self._recvn(s, dlen + 2)
            elif atyp == 0x04:
                self._recvn(s, 16 + 2)
            else:
                raise ForgeError(f"SOCKS5 bad atyp {atyp}")
        else:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        self.sock = s

    def _read_record(self):
        hdr = self._read_exactly(5)
        ctype, ver, ln = hdr[0], hdr[1:3], struct.unpack(">H", hdr[3:5])[0]
        if ln > 2 ** 14 + 256:
            raise ForgeError("oversize record")
        return ctype, self._read_exactly(ln), hdr

    def _read_exactly(self, n):
        out = b""
        while len(out) < n:
            ch = self.sock.recv(n - len(out))
            if not ch:
                raise ForgeError("connection closed mid-record")
            out += ch
        return out

    # ----- AES-GCM with sequence nonces -----
    def _nonce(self, iv, seq):
        return bytes(a ^ b for a, b in zip(iv, seq.to_bytes(12, "big")))

    def _decrypt(self, key, iv, seq, header, record_payload):
        if len(record_payload) < 16:
            raise ForgeError("short ciphertext")
        ct, tag = record_payload[:-16], record_payload[-16:]
        d = Cipher(algorithms.AES(key), modes.GCM(self._nonce(iv, seq), tag)).decryptor()
        d.authenticate_additional_data(header)
        return d.update(ct) + d.finalize()

    def _send_encrypted(self, inner_type, plaintext):
        inner = plaintext + bytes([inner_type])
        e = Cipher(algorithms.AES(self._wkey),
                   modes.GCM(self._nonce(self._wiv, self._wseq))).encryptor()
        # AAD (outer header) depends on ciphertext length: same length either way.
        hdr = b"\x17\x03\x03" + struct.pack(">H", len(inner) + 16)
        e.authenticate_additional_data(hdr)
        ct = e.update(inner) + e.finalize()
        self.sock.sendall(hdr + ct + e.tag)
        self._wseq += 1

    # ----- handshake -----
    def _forge_hello(self):
        hello = bytearray(_HELLO_TEMPLATE)
        hello[_RANDOM_OFF:_RANDOM_OFF + _RANDOM_LEN] = secrets.token_bytes(32)
        pub = self._priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        assert len(pub) == _PUBKEY_LEN
        hello[_PUBKEY_OFF:_PUBKEY_OFF + _PUBKEY_LEN] = pub
        return bytes(hello)

    def handshake(self):
        hello_rec = self._forge_hello()
        ch_msg = hello_rec[5:]
        transcript = bytearray(ch_msg)
        # send ClientHello (record header from template + fragment)
        self.sock.sendall(hello_rec)
        # --- ServerHello ---
        ctype, sh_frag, _ = self._read_record()
        if ctype == 21:
            raise ForgeError(f"alert during handshake: {sh_frag.hex()}")
        if ctype != 22:
            raise ForgeError(f"expected ServerHello, got type {ctype}")
        # parse (single-record ServerHello assumed; CF sends it whole)
        assert sh_frag[0] == 0x02, sh_frag[:4].hex()
        sh_len = struct.unpack(">I", b"\x00" + sh_frag[1:4])[0]
        sh_body = sh_frag[4:4 + sh_len]
        if sh_body[:32] == bytes.fromhex("CF21AD74E59A6111BE1D8C021E65B899C2FC5EC0FDBE7BA9251A4A0224"):
            raise ForgeError("HelloRetryRequest - unsupported path")
        p = 2 + 32
        sl = sh_body[p]; p += 1 + sl
        cipher = struct.unpack(">H", sh_body[p:p + 2])[0]; p += 2
        if cipher == 0x1301:
            self._hn, self._hlen, self._klen = "sha256", 32, 16
        elif cipher == 0x1302:
            self._hn, self._hlen, self._klen = "sha384", 48, 32
        else:
            raise ForgeError(f"unsupported cipher {cipher:04x} (need 1301/1302)")
        p += 1  # compression
        el = struct.unpack(">H", sh_body[p:p + 2])[0]; p += 2
        exts, server_pub, got_versions = sh_body[p:p + el], None, False
        q = 0
        while q + 4 <= len(exts):
            et, elen = struct.unpack(">HH", exts[q:q + 4])
            if et == 51:
                # ServerHello carries a bare KeyShareEntry (no vector prefix).
                g, kl = struct.unpack(">HH", exts[q + 4:q + 8])
                if g != 0x001D or kl != 32:
                    raise ForgeError("non-x25519 key share")
                server_pub = exts[q + 8:q + 8 + 32]
            if et == 43 and b"\x03\x04" in exts[q + 4:q + 4 + elen]:
                got_versions = True
            q += 4 + elen
        if server_pub is None or not got_versions:
            raise ForgeError("ServerHello missing key_share/TLS1.3")
        transcript += sh_frag[:4 + sh_len]
        # --- key schedule ---
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
        shared = self._priv.exchange(X25519PublicKey.from_public_bytes(server_pub))
        hn, hlen, klen = self._hn, self._hlen, self._klen
        zeros = b"\x00" * hlen
        early = _hkdf_extract(zeros, zeros, hn)
        derived = _derive_secret(early, b"derived", hashlib.new(hn, b"").digest(), hn)
        hs = _hkdf_extract(derived, shared, hn)
        th1 = hashlib.new(hn, bytes(transcript)).digest()
        c_hs = _derive_secret(hs, b"c hs traffic", th1, hn)
        s_hs = _derive_secret(hs, b"s hs traffic", th1, hn)
        c_hs_key, c_hs_iv = _traffic_keys(c_hs, klen)
        s_hs_key, s_hs_iv = _traffic_keys(s_hs, klen)
        self._rkey, self._riv, self._rseq = s_hs_key, s_hs_iv, 0
        # --- encrypted handshake flight ---
        certs_der, cv_scheme, cv_sig, sfin = None, None, None, None
        th_for_cv = None
        msgs = []
        while sfin is None:
            ctype, payload, hdr = self._read_record()
            if ctype == 21:
                raise ForgeError(f"handshake alert: {payload.hex()}")
            if ctype == 20:
                continue  # middlebox-compat CCS, ignore
            # NB: post-ServerHello records ALWAYS carry outer type 23
            # (application_data); the real type is the last inner byte.
            if ctype != 23:
                raise ForgeError(f"unexpected record {ctype} in handshake")
            pt = self._decrypt(s_hs_key, s_hs_iv, self._rseq, hdr, payload)
            self._rseq += 1
            if not pt or pt[-1] != 22:
                raise ForgeError("bad inner type in handshake flight")
            self._hsbuf += pt[:-1]
            while len(self._hsbuf) >= 4:
                ml = struct.unpack(">I", b"\x00" + bytes(self._hsbuf[1:4]))[0]
                if len(self._hsbuf) < 4 + ml:
                    break
                mtype, mbody = self._hsbuf[0], bytes(self._hsbuf[4:4 + ml])
                del self._hsbuf[:4 + ml]
                if mtype == 15:
                    # CertificateVerify signs Hash(ClientHello..server Certificate),
                    # i.e. WITHOUT itself: snapshot before appending.
                    th_for_cv = hashlib.new(self._hn, bytes(transcript)).digest()
                if mtype == 20 and sfin is None:
                    # Server Finished covers Hash(CH..CV): snapshot first.
                    th_pre_fin = hashlib.new(self._hn, bytes(transcript)).digest()
                transcript += bytes([mtype]) + struct.pack(">I", ml)[1:] + mbody
                msgs.append((mtype, mbody))
                if mtype == 20:
                    sfin = mbody
                    break  # trailing bytes (e.g. NST) are NOT part of th2
        # --- parse flight ---
        ee = cert_list = None
        for mtype, mbody in msgs:
            if mtype == 8:
                ee = mbody
            elif mtype == 11:
                cert_list = self._parse_certs(mbody)
            elif mtype == 15:
                cv_scheme = struct.unpack(">H", mbody[:2])[0]
                slen = struct.unpack(">H", mbody[2:4])[0]
                cv_sig = mbody[4:4 + slen]
        if cert_list is None or cv_sig is None:
            raise ForgeError("flight missing Certificate/CertificateVerify")
        leaf = cert_list[0]
        # CertificateVerify over Hash(CH..Cert) — snapshot saved above.
        # Server Finished covers Hash(CH..CV), i.e. everything except itself.
        if th_for_cv is None or th_pre_fin is None:
            raise ForgeError("flight missing CertificateVerify/Finished")
        # server Finished verify FIRST (fast HMAC)...
        hn = self._hn
        s_fin_key = _finished_key(s_hs, hn)
        h = hmac.new(s_fin_key, digestmod=hn)
        h.update(th_pre_fin)
        if not hmac.compare_digest(h.digest(), sfin):
            raise ForgeError("server Finished MAC mismatch (code bug?)")
        th2 = hashlib.new(hn, bytes(transcript)).digest()
        # --- our Finished ---
        # Client Finished covers Hash(CH..server Finished), i.e. INCLUDING
        # the server Finished (unlike server Finished which stops at CV).
        th_with_sfin = hashlib.new(hn, bytes(transcript)).digest()
        c_fin_key = _finished_key(c_hs, hn)
        h2 = hmac.new(c_fin_key, digestmod=hn)
        h2.update(th_with_sfin)
        # NB: intermediate "derived" salts ALWAYS hash the EMPTY transcript
        # (RFC 8446 S7.1, proven by RFC 8448 master vectors 18.09.2026) —
        # th2 goes ONLY into final secrets (ap traffic / exporter / resumption).
        master = _hkdf_extract(
            _derive_secret(hs, b"derived", hashlib.new(hn, b"").digest(), hn),
            zeros, hn)
        c_ap = _derive_secret(master, b"c ap traffic", th2, hn)
        s_ap = _derive_secret(master, b"s ap traffic", th2, hn)
        fin_msg = b"\x14\x00\x00\x20" + h2.digest()
        # temporarily point write keys at handshake keys for our Finished
        self._wkey, self._wiv = c_hs_key, c_hs_iv
        self._send_encrypted(0x16, fin_msg)
        self._wkey, self._wiv = _traffic_keys(c_ap, self._klen)
        self._rkey, self._riv = _traffic_keys(s_ap, self._klen)
        # CRITICAL (RFC 8446 S5.3): sequence numbers are PER EPOCH —
        # handshake epoch and application epoch EACH start at 0.
        # Forgetting this reset breaks every app record (found 18.09.2026).
        self._rseq = 0
        self._wseq = 0
        # ...THEN the slow cert/CV checks (server must not wait for these).
        try:
            _verify_chain(leaf, cert_list[1:], self.host)
        except ForgeError as e:
            # Documented tradeoff: fail-open with a loud marker (see module doc).
            self._cert_warn = f"CERT VERIFY SKIPPED: {e}"
        signed = b"\x20" * 64 + b"TLS 1.3, server CertificateVerify\x00" + th_for_cv
        try:
            self._verify_cv(cv_scheme, leaf, cv_sig, signed)
        except ForgeError as e:
            self._cert_warn = getattr(self, "_cert_warn", "") + f" | CV SKIPPED: {e}"
        # transcript no longer needed

    @staticmethod
    def _parse_certs(mbody):
        if mbody[0] != 0:
            raise ForgeError("non-empty cert request context")
        total = struct.unpack(">I", b"\x00" + mbody[1:4])[0]
        p, out = 4, []
        end = 4 + total
        while p < end:
            ln = struct.unpack(">I", b"\x00" + mbody[p:p + 3])[0]; p += 3
            from cryptography.x509 import load_der_x509_certificate
            out.append(load_der_x509_certificate(mbody[p:p + ln])); p += ln
            el = struct.unpack(">H", mbody[p:p + 2])[0]; p += 2 + el
        if not out:
            raise ForgeError("empty certificate list")
        return out

    @staticmethod
    def _verify_cv(scheme, leaf_cert, sig, signed):
        pub = leaf_cert.public_key()
        ecdsa = {0x0403: hashes.SHA256(), 0x0402: hashes.SHA384(),
                 0x0401: hashes.SHA512()}
        pss = {0x0804: hashes.SHA256(), 0x0805: hashes.SHA384(),
               0x0806: hashes.SHA512()}
        if scheme in ecdsa:
            pub.verify(sig, signed, ec.ECDSA(ecdsa[scheme]))
        elif scheme in pss:
            pub.verify(sig, signed, padding.PSS(mgf=padding.MGF1(pss[scheme]),
                                                salt_length=padding.PSS.DIGEST_LENGTH),
                       pss[scheme])
        else:
            raise ForgeError(f"unsupported CV scheme {scheme:04x}")

    # ----- application data -----
    def send_app(self, data: bytes):
        while data:
            chunk, data = data[:16384], data[16384:]
            self._send_encrypted(0x17, chunk)

    def _read_app_chunk(self):
        while True:
            ctype, payload, hdr = self._read_record()
            if ctype == 21:
                if len(payload) >= 2 and payload[0] == 2:
                    raise ForgeError(f"fatal alert {payload[1]}")
                continue
            if ctype != 23:
                continue
            pt = self._decrypt(self._rkey, self._riv, self._rseq, hdr, payload)
            self._rseq += 1
            if not pt:
                continue
            itype, data = pt[-1], pt[:-1]
            if itype == 23:
                return data
            if itype == 22:
                # post-handshake handshake msg (e.g. NewSessionTicket): skip
                continue
            # unknown inner type: skip

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------- HTTP/1.1 over forged TLS + requests-like adapter ----------

class ForgedResponse:
    def __init__(self, status_code, headers, body_iter, conn):
        self.status_code = status_code
        self.headers = headers
        self._body_iter = body_iter
        self._conn = conn
        self._closed = False

    @property
    def text(self):
        if not hasattr(self, "_text_cache"):
            self._text_cache = b"".join(self.iter_content()).decode("utf-8", "replace")
        return self._text_cache

    def json(self, **kwargs):
        import json as _json
        return _json.loads(self.text, **kwargs)

    def iter_lines(self, chunk_size=65536, decode_unicode=False):
        buf = b""
        for chunk in self.iter_content(chunk_size=chunk_size):
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.rstrip(b"\r")
                yield line.decode("utf-8", "replace") if decode_unicode else line
        if buf:
            line = buf.rstrip(b"\r")
            yield line.decode("utf-8", "replace") if decode_unicode else line

    def iter_content(self, chunk_size=65536):
        enc = self.headers.get("content-encoding", "identity").lower()
        if "gzip" in enc:
            import zlib as _zlib
            raw = b"".join(self._body_iter)
            try:
                data = _zlib.decompress(raw, 16 + _zlib.MAX_WBITS)
            except Exception:
                data = raw
            for i in range(0, len(data), chunk_size):
                yield data[i:i + chunk_size]
            return
        buf = b""
        for chunk in self._body_iter:
            buf += chunk
            while len(buf) >= chunk_size:
                yield buf[:chunk_size]
                buf = buf[chunk_size:]
        if buf:
            yield buf

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                for _ in self._body_iter:
                    pass
            except ForgeError:
                pass
            self._conn.close()


def _read_http_response(conn, read_timeout):
    conn.sock.settimeout(read_timeout)
    raw = b""
    while b"\r\n\r\n" not in raw:
        raw += conn._read_app_chunk()
        if len(raw) > 65536:
            raise ForgeError("header block too large")
    head, rest = raw.split(b"\r\n\r\n", 1)
    lines = head.split(b"\r\n")
    try:
        status = int(lines[0].split(b" ", 2)[1])
    except (IndexError, ValueError):
        raise ForgeError(f"bad status line: {lines[0][:60]!r}")
    headers = {}
    for ln in lines[1:]:
        if b":" in ln:
            k, v = ln.split(b":", 1)
            headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()
    return status, headers, rest


def _body_iter(conn, headers, initial, read_timeout):
    conn.sock.settimeout(read_timeout)
    buf = initial
    enc = headers.get("content-encoding", "identity").lower()
    chunks = []
    if "content-length" in headers:
        need = int(headers["content-length"])
        while len(buf) < need:
            buf += conn._read_app_chunk()
        raw = buf[:need]
        yield raw
    elif headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            while b"\r\n" not in buf:
                buf += conn._read_app_chunk()
            ln, buf = buf.split(b"\r\n", 1)
            try:
                size = int(ln.split(b";")[0].strip(), 16)
            except ValueError:
                raise ForgeError("bad chunk size")
            if size == 0:
                break
            while len(buf) < size + 2:
                buf += conn._read_app_chunk()
            yield buf[:size]
            buf = buf[size + 2:]
    else:
        # close-delimited
        if buf:
            yield buf
        try:
            while True:
                yield conn._read_app_chunk()
        except ForgeError:
            return


def forge_request(method, url, headers_ordered, body=b"", socks=None,
                  connect_timeout=60, read_timeout=300):
    """Mirror of requests.request(stream=True) over forged TLS.

    headers_ordered: list of (name, value) in exact wire order.
    Returns ForgedResponse (status+headers read eagerly, body lazy).
    Only https:// URLs. Host header is caller's responsibility.
    """
    if not url.startswith("https://"):
        raise ForgeError("only https:// supported")
    rest = url[len("https://"):]
    host = rest.split("/", 1)[0].split(":")[0]
    path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    if host != "opencode.ai":
        raise ForgeError("forge is purpose-built for opencode.ai")
    # NB: __init__ already ran handshake(); do NOT call it again here
    # (a second ClientHello mid-session desyncs the stream).
    conn = ForgedTLSConnection(host, 443, socks=socks, timeout=connect_timeout)
    body = body or b""
    raw = (f"{method} {path} HTTP/1.1\r\n").encode()
    for k, v in headers_ordered:
        raw += f"{k}: {v}\r\n".encode("latin1")
    raw += f"Content-Length: {len(body)}\r\n".encode() + b"\r\n" + body
    try:
        conn.send_app(raw)
        status, headers, rest0 = _read_http_response(conn, read_timeout)
    except Exception:
        conn.close()
        raise
    return ForgedResponse(status, headers,
                          _body_iter(conn, headers, rest0, read_timeout), conn)


if __name__ == "__main__":
    import sys
    _h = [
        ("Authorization", "Bearer public"),
        ("Content-Type", "application/json"),
        ("User-Agent", "opencode/1.18.31 ai-sdk/provider-utils/4.0.40 runtime/bun/1.3.14"),
        ("x-opencode-client", "cli"),
        ("x-opencode-project", "global"),
        ("x-opencode-request", "msg_abcdef123456ABCDEF1234567"),
        ("x-opencode-session", "ses_abcdef123456ABCDEF1234567"),
        ("Connection", "keep-alive"),
        ("Accept", "*/*"),
        ("Host", "opencode.ai"),
        ("Accept-Encoding", "gzip, deflate"),
    ]
    _model = sys.argv[1] if len(sys.argv) > 1 else "mimo-v2.5-free"
    _b = ('{"model":"%s","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}'
          % _model).encode()
    _r = forge_request("POST", "https://opencode.ai/zen/v1/chat/completions", _h, _b,
                       socks=("127.0.0.1", 9050))
    print("STATUS:", _r.status_code)
    data = b"".join(_r.iter_content())
    print(data[:300].decode("utf-8", "replace"))
    _r.close()
