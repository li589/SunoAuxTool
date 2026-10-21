"""usesuno 下载源契约测试（R9）：真实密码学往返 + mock HTTP。

不 mock 任何密码学原语——密钥协商/解包/解密全部走真实 AES-GCM/AES-CTR，
仅 HTTP 层用脚本化响应注入（与 tests/test_download_suno_dl.py 同风格）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from pathlib import Path

import pytest

from sunoauxtool.download.sources.base import list_sources
from sunoauxtool.download.sources.usesuno import (
    AUDIO_URL_TEMPLATE,
    UseSunoSource,
)
from sunoauxtool.download.unlock.exceptions import DecryptFailedError
from sunoauxtool.exceptions import ParameterError, SourceCredentialError, SourceRequestError

CLIP_ID = "90c5e692-29ab-4c1e-9f2a-1d2b3c4d5e6f"
GLT = "test-guest-login-token"


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _scripted(responses: list):
    """按调用序消费响应；元素为 Exception 时抛出。返回 (fake_urlopen, calls)。"""
    calls: list = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(item)

    return fake_urlopen, calls


def _wrap(user_key: bytes, plaintext: bytes, clip_id: str) -> str:
    """base64(iv12B || AES-GCM(key=user_key, aad=clip_id) 密文+tag)。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    iv12 = b"\x01" * 12
    ct = AESGCM(user_key).encrypt(iv12, plaintext, clip_id.encode("utf-8"))
    return base64.b64encode(iv12 + ct).decode("ascii")


def _ctr_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    enc = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def _fake_mp4() -> bytes:
    """最小 MP4：ftyp + moov（解密结果 sanity 校验只查 ftyp）。"""
    ftyp = struct.pack(">I", 16) + b"ftyp" + b"M4A \x00\x00\x00\x00M4A mp42"
    moov = struct.pack(">I", 8) + b"moov"
    return ftyp + moov


def _make_rights_response(user_key: bytes, clip_id: str = CLIP_ID, glt: str = GLT):
    """生成真实 rights 响应体（JSON bytes）与内容密钥。"""
    import os as _os

    content_key = _os.urandom(32)
    content_iv = _os.urandom(16)
    body = json.dumps(
        {
            "key": _wrap(user_key, content_key, clip_id),
            "iv": _wrap(user_key, content_iv, clip_id),
            "glt": glt,
        }
    ).encode("utf-8")
    return body, content_key, content_iv


@pytest.fixture(autouse=True)
def _isolate_home_config(monkeypatch, tmp_path):
    """隔离 ~/.sunoauxtool/sources.toml 与 CWD 配置，避免读到真实凭证。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("USESUNO_TOKEN", raising=False)


# ---------------------------------------------------------------------------
# 契约：真实密码学往返
# ---------------------------------------------------------------------------


class TestUseSunoContract:
    def test_fetch_decrypts_roundtrip(self, tmp_path):
        user_key = hashlib.sha256(GLT.encode()).digest()
        rights_body, content_key, content_iv = _make_rights_response(user_key)
        plain = _fake_mp4() + b"\x00" * 512
        cipher = _ctr_encrypt(content_key, content_iv, plain)

        urlopen, calls = _scripted([rights_body, cipher])
        src = UseSunoSource(token="fake-bearer-token")
        src.urlopen = urlopen

        files = src.fetch(CLIP_ID, tmp_path / "out")
        assert len(files) == 1
        out = files[0].path
        assert out.name == f"{CLIP_ID}.m4a"
        assert out.read_bytes() == plain
        assert files[0].meta["clip_id"] == CLIP_ID

        # 请求形态：POST rights -> GET cloudfront
        assert calls[0].get_method() == "POST"
        assert calls[0].full_url == "https://usesuno.com/api/rights"
        assert calls[0].get_header("Authorization") == "Bearer fake-bearer-token"
        assert calls[0].get_header("X-usesuno-protocol") == "1"
        assert json.loads(calls[0].data)["content_params"] == {
            "content_id": CLIP_ID,
            "content_type": "clip",
        }
        assert calls[1].full_url == AUDIO_URL_TEMPLATE.format(clip_id=CLIP_ID)

    def test_fetch_accepts_song_url(self, tmp_path):
        user_key = hashlib.sha256(GLT.encode()).digest()
        rights_body, content_key, content_iv = _make_rights_response(user_key)
        cipher = _ctr_encrypt(content_key, content_iv, _fake_mp4())
        urlopen, _calls = _scripted([rights_body, cipher])
        src = UseSunoSource(token="t")
        src.urlopen = urlopen
        files = src.fetch(f"https://suno.com/song/{CLIP_ID}", tmp_path / "out")
        assert files[0].path.name == f"{CLIP_ID}.m4a"

    def test_missing_token_raises_25(self, tmp_path):
        src = UseSunoSource()
        with pytest.raises(SourceCredentialError) as exc:
            src.fetch(CLIP_ID, tmp_path / "out")
        assert exc.value.code == 25
        assert "Turnstile" in exc.value.message

    def test_check_masks_token(self):
        src = UseSunoSource(token="abcdefghijklmnop")
        text = src.check(CLIP_ID)
        assert "abcd***op" in text
        assert "abcdefghijklmnop" not in text
        assert CLIP_ID in text

    def test_check_without_token_raises_25(self):
        with pytest.raises(SourceCredentialError) as exc:
            UseSunoSource().check(CLIP_ID)
        assert exc.value.code == 25

    def test_rights_missing_fields_raises_26(self, tmp_path):
        urlopen, _calls = _scripted([json.dumps({"glt": GLT}).encode()])
        src = UseSunoSource(token="t")
        src.urlopen = urlopen
        with pytest.raises(SourceRequestError) as exc:
            src.fetch(CLIP_ID, tmp_path / "out")
        assert exc.value.code == 26
        assert "key" in exc.value.message and "iv" in exc.value.message

    def test_network_failure_raises_26(self, tmp_path):
        urlopen, _calls = _scripted([OSError("connection reset")])
        src = UseSunoSource(token="t")
        src.urlopen = urlopen
        with pytest.raises(SourceRequestError) as exc:
            src.fetch(CLIP_ID, tmp_path / "out")
        assert exc.value.code == 26

    def test_wrong_token_unwrap_fails_28(self, tmp_path):
        """rights 用另一把 user_key 打包 -> GCM 认证失败 -> 28。"""
        other_user_key = hashlib.sha256(b"another-glt").digest()
        rights_body, _ck, _civ = _make_rights_response(other_user_key)
        urlopen, _calls = _scripted([rights_body, b"\x00" * 64])
        src = UseSunoSource(token="t")
        src.urlopen = urlopen
        with pytest.raises(DecryptFailedError) as exc:
            src.fetch(CLIP_ID, tmp_path / "out")
        assert exc.value.code == 28

    def test_non_mp4_plaintext_raises_28(self, tmp_path):
        """密钥对但明文不是音频容器 -> sanity 校验拦截 -> 28。"""
        user_key = hashlib.sha256(GLT.encode()).digest()
        rights_body, content_key, content_iv = _make_rights_response(user_key)
        cipher = _ctr_encrypt(content_key, content_iv, b"not an mp4 at all!!")
        urlopen, _calls = _scripted([rights_body, cipher])
        src = UseSunoSource(token="t")
        src.urlopen = urlopen
        with pytest.raises(DecryptFailedError) as exc:
            src.fetch(CLIP_ID, tmp_path / "out")
        assert exc.value.code == 28
        assert "不是可识别音频容器" in exc.value.message

    def test_bad_query_raises_1(self, tmp_path):
        src = UseSunoSource(token="t")
        with pytest.raises(ParameterError) as exc:
            src.fetch("not-a-uuid", tmp_path / "out")
        assert exc.value.code == 1

    def test_token_from_env(self, monkeypatch, tmp_path):
        user_key = hashlib.sha256(GLT.encode()).digest()
        rights_body, content_key, content_iv = _make_rights_response(user_key)
        cipher = _ctr_encrypt(content_key, content_iv, _fake_mp4())
        urlopen, calls = _scripted([rights_body, cipher])
        monkeypatch.setenv("USESUNO_TOKEN", "env-token")
        src = UseSunoSource()
        src.urlopen = urlopen
        files = src.fetch(CLIP_ID, tmp_path / "out")
        assert calls[0].get_header("Authorization") == "Bearer env-token"
        assert files[0].path.exists()


# ---------------------------------------------------------------------------
# 注册与 CLI
# ---------------------------------------------------------------------------


class TestUseSunoRegistration:
    def test_registered_in_list_sources(self):
        assert "usesuno" in {s.name for s in list_sources()}

    def test_cli_dry_run_missing_token_exit_25(self):
        from typer.testing import CliRunner

        from sunoauxtool.aggregate import app

        r = CliRunner().invoke(
            app, ["post", "fetch", CLIP_ID, "--source", "usesuno", "--dry-run"]
        )
        assert r.exit_code == 25
        assert "25" in r.output

    def test_cli_dry_run_with_token_exit_0(self):
        from typer.testing import CliRunner

        from sunoauxtool.aggregate import app

        r = CliRunner().invoke(
            app,
            ["post", "fetch", CLIP_ID, "--source", "usesuno", "--dry-run", "--token", "tok-tok-tok"],
        )
        assert r.exit_code == 0, r.output
        assert "tok-***ok" in r.output
        assert "tok-tok-tok" not in r.output

    def test_cli_unknown_source_lists_usesuno(self):
        from typer.testing import CliRunner

        from sunoauxtool.aggregate import app

        r = CliRunner().invoke(app, ["post", "fetch", CLIP_ID, "--source", "bogus"])
        assert r.exit_code == 1
        assert "usesuno" in r.output
