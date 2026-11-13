"""PII protection.
- encrypt()/decrypt(): AES-256-GCM, random 12-byte nonce per value (same input -> different output).
- blind_index(): HMAC-SHA256 with a SEPARATE key (same input -> same output) so we can match
  phones without storing them in plaintext. A plain sha256(phone) would be useless: India has
  only ~4 billion valid mobile numbers, so anyone could hash them all and reverse it.
Keys come from env vars, or from a git-ignored .env file that is created on first run."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENC_VAR, BIDX_VAR = "AU_ENC_KEY", "AU_BIDX_KEY"
# Repo-root .env, independent of the current working directory.
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        # utf-8-sig: tolerate a BOM (Windows editors), which would otherwise hide the first key.
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def load_or_create_keys(env_file: Path = DEFAULT_ENV_FILE) -> tuple[bytes, bytes]:
    file_values = _read_env_file(env_file)
    enc = os.environ.get(ENC_VAR) or file_values.get(ENC_VAR)
    bidx = os.environ.get(BIDX_VAR) or file_values.get(BIDX_VAR)
    if enc is None and bidx is None:                      # first run: create both
        enc = base64.b64encode(os.urandom(32)).decode()
        bidx = base64.b64encode(os.urandom(32)).decode()
        sep = ""
        if env_file.exists():
            existing = env_file.read_bytes()
            if existing and not existing.endswith(b"\n"):
                sep = "\n"                                # don't glue our key onto the last line
        with env_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{sep}{ENC_VAR}={enc}\n{BIDX_VAR}={bidx}\n")
    elif enc is None or bidx is None:                     # partial config: never silently regenerate
        raise SystemExit(f"Only one of {ENC_VAR}/{BIDX_VAR} is set. Restore the missing key.")
    keys = []
    for name, value in ((ENC_VAR, enc), (BIDX_VAR, bidx)):
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError:
            raise SystemExit(f"{name} is not valid base64.") from None
        if len(raw) != 32:
            raise SystemExit(f"{name} must decode to 32 bytes.")
        keys.append(raw)
    return keys[0], keys[1]


class Vault:
    def __init__(self, enc_key: bytes, bidx_key: bytes):
        if enc_key == bidx_key:
            raise ValueError("encryption key and blind-index key must differ")
        self._aes = AESGCM(enc_key)
        self._bidx_key = bidx_key

    def encrypt(self, plaintext: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, plaintext.encode("utf-8"), None)

    def decrypt(self, blob: bytes) -> str:
        return self._aes.decrypt(blob[:12], blob[12:], None).decode("utf-8")

    def blind_index(self, canonical_phone: str) -> str:
        return hmac.new(self._bidx_key, canonical_phone.encode("utf-8"), hashlib.sha256).hexdigest()

    def key_fingerprint(self) -> str:
        """Covers BOTH keys: HMAC of a constant (bidx key) + a deterministic AES-GCM tag of a
        constant under a fixed nonce (enc key). Stored in the DB on first run; a change to either
        key is refused. Fixed nonce is safe here: one constant plaintext, used only as a check."""
        enc_check = self._aes.encrypt(b"\x00" * 12, b"au-key-check", None).hex()
        return self.blind_index("au-key-fingerprint-v1") + ":" + enc_check