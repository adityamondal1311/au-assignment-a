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


def test_env_file_with_bom_loads_both_keys(tmp_path, monkeypatch):
    # .env saved by a Windows editor with a UTF-8 BOM must not hide the first key.
    monkeypatch.delenv("AU_ENC_KEY", raising=False); monkeypatch.delenv("AU_BIDX_KEY", raising=False)
    enc, bidx = os.urandom(32), os.urandom(32)
    env = tmp_path / ".env"
    env.write_text(f"AU_ENC_KEY={base64.b64encode(enc).decode()}\nAU_BIDX_KEY={base64.b64encode(bidx).decode()}\n",
                   encoding="utf-8-sig")
    assert env.read_bytes().startswith(b"\xef\xbb\xbf")
    assert load_or_create_keys(env) == (enc, bidx)


def test_new_keys_appended_on_own_line(tmp_path, monkeypatch):
    # Existing .env without trailing newline: generated keys must not be glued onto its last line.
    monkeypatch.delenv("AU_ENC_KEY", raising=False); monkeypatch.delenv("AU_BIDX_KEY", raising=False)
    env = tmp_path / ".env"; env.write_bytes(b"OTHER=1")
    keys = load_or_create_keys(env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "OTHER=1"
    assert lines[1].startswith("AU_ENC_KEY=") and lines[2].startswith("AU_BIDX_KEY=")
    assert load_or_create_keys(env) == keys


def test_default_env_file_is_repo_root():
    from pathlib import Path
    from solution.crypto import DEFAULT_ENV_FILE
    assert DEFAULT_ENV_FILE == Path(__file__).resolve().parents[2] / ".env"
