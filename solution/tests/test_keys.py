import base64, os
import pytest
from solution.crypto import Vault, load_or_create_keys
from solution.db import connect

def test_partial_env_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("AU_ENC_KEY", raising=False); monkeypatch.delenv("AU_BIDX_KEY", raising=False)
    env = tmp_path / ".env"; env.write_text("AU_ENC_KEY=" + base64.b64encode(os.urandom(32)).decode() + "\n")
    with pytest.raises(SystemExit, match="Only one"):
        load_or_create_keys(env)

def test_first_run_creates_then_reuses(tmp_path, monkeypatch):
    monkeypatch.delenv("AU_ENC_KEY", raising=False); monkeypatch.delenv("AU_BIDX_KEY", raising=False)
    env = tmp_path / ".env"
    assert load_or_create_keys(env) == load_or_create_keys(env)

def test_bad_length_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("AU_ENC_KEY", base64.b64encode(b"short").decode())
    monkeypatch.setenv("AU_BIDX_KEY", base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(SystemExit, match="32 bytes"):
        load_or_create_keys(tmp_path / ".env")

def test_changed_encryption_key_alone_is_refused(tmp_path):
    db = str(tmp_path / "t.db"); bidx = os.urandom(32)
    connect(db, Vault(os.urandom(32), bidx)).close()
    with pytest.raises(SystemExit, match="do not match"):
        connect(db, Vault(os.urandom(32), bidx))       # same HMAC key, different AES key