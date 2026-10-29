"""suno-web 源契约测试（2.5 通道 B，P1 只读）：mock HTTP + 凭证分派。

只 mock HTTP 层；凭证查找顺序 / 401 映射 / 载荷归一化 / 文件落盘走真实实现。
端点路径本身属"待真实 Cookie 校准"项，测试用注入的 base_url，不依赖真实网络。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sunoauxtool.download.sources.base import discover_sources
from sunoauxtool.download.sources.suno_web import (
    CLIP_PATH,
    FEED_PATH,
    SunoWebSource,
)
from sunoauxtool.exceptions import SourceCredentialError, SourceRequestError

CLIP_ID = "90c5e692-29ab-4c1e-9f2a-1d2b3c4d5e6f"
AUDIO = f"https://cdn1.suno.ai/{CLIP_ID}.mp3"


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _feed_item(clip_id: str, title: str = "夜航") -> dict:
    return {
        "id": clip_id,
        "title": title,
        "audio_url": f"https://cdn1.suno.ai/{clip_id}.mp3",
    }


def _source(monkeypatch: pytest.MonkeyPatch, responses: list):
    calls: list = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(item)

    source = SunoWebSource(cookie="cookie-alpha-12345678", web_base="https://suno.example")
    source.urlopen = fake_urlopen
    return source, calls


@pytest.fixture()
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUNOAUX_SUNO_COOKIE", raising=False)
    monkeypatch.delenv("SUNOAUX_SUNO_WEB_BASE", raising=False)


class TestCredential:
    def test_missing_cookie_is_25(self, _no_env, monkeypatch: pytest.MonkeyPatch,
                                  tmp_path: Path) -> None:
        monkeypatch.setattr(type(SunoWebSource()), "_resolve_cookie",
                            lambda self: "", raising=False)
        source = SunoWebSource(cookie="", web_base="https://suno.example")
        with pytest.raises(SourceCredentialError) as excinfo:
            source.check(CLIP_ID)
        assert excinfo.value.code == 25
        assert "Cookie" in str(excinfo.value)

    def test_check_masks_cookie(self) -> None:
        source = SunoWebSource(cookie="cookie-alpha-12345678",
                               web_base="https://suno.example")
        text = source.check(CLIP_ID)
        assert "cook***78" in text
        assert "cookie-alpha-12345678" not in text

    def test_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUNOAUX_SUNO_COOKIE", "from-env")
        assert SunoWebSource(web_base="https://suno.example")._resolve_cookie() == "from-env"


class TestReadOnly:
    def test_list_clips_normalizes_payload(self, monkeypatch) -> None:
        body = json.dumps({"clips": [_feed_item(CLIP_ID)]}).encode()
        source, calls = _source(monkeypatch, [body])
        clips = source.list_clips(limit=5)
        assert clips == [{"id": CLIP_ID, "title": "夜航",
                          "urls": [f"https://cdn1.suno.ai/{CLIP_ID}.mp3"]}]
        req = calls[0]
        assert req.full_url == f"https://suno.example{FEED_PATH}?page=0"
        assert "cookie-alpha-12345678" in req.get_header("Cookie")

    def test_list_accepts_bare_list_payload(self, monkeypatch) -> None:
        body = json.dumps([_feed_item(CLIP_ID)]).encode()
        source, _ = _source(monkeypatch, [body])
        assert source.list_clips()[0]["id"] == CLIP_ID

    def test_get_clip_uses_id_path(self, monkeypatch) -> None:
        body = json.dumps({"clip": _feed_item(CLIP_ID)}).encode()
        source, calls = _source(monkeypatch, [body])
        clip = source.get_clip(f"https://suno.com/song/{CLIP_ID}/")
        assert clip["id"] == CLIP_ID
        assert calls[0].full_url == f"https://suno.example{CLIP_PATH.format(clip_id=CLIP_ID)}"

    def test_401_maps_to_25_with_expiry_message(self, monkeypatch) -> None:
        import urllib.error
        source, _ = _source(monkeypatch, [
            urllib.error.HTTPError("https://suno.example/x", 401, "Unauthorized", {}, None)
        ])
        with pytest.raises(SourceCredentialError) as excinfo:
            source.list_clips()
        assert excinfo.value.code == 25
        assert "过期" in str(excinfo.value)

    def test_500_maps_to_26(self, monkeypatch) -> None:
        import urllib.error
        source, _ = _source(monkeypatch, [
            urllib.error.HTTPError("https://suno.example/x", 500, "Oops", {}, None)
        ])
        with pytest.raises(SourceRequestError) as excinfo:
            source.list_clips()
        assert excinfo.value.code == 26

    def test_bad_clip_id_is_26(self) -> None:
        source = SunoWebSource(cookie="c", web_base="https://suno.example")
        with pytest.raises(SourceRequestError):
            source.get_clip("不是 UUID")


class TestFetch:
    def test_fetch_downloads_all_versions(self, tmp_path: Path, monkeypatch) -> None:
        clip_body = json.dumps({"clip": _feed_item(CLIP_ID)}).encode()
        source, calls = _source(monkeypatch, [clip_body, b"MP3A", b"MP3B"])
        files = source.fetch(f"https://suno.com/song/{CLIP_ID}/", tmp_path)
        assert [f.path.name for f in files] == [f"{CLIP_ID}-v1.mp3"]
        assert files[0].path.read_bytes() == b"MP3A"
        assert files[0].meta["title"] == "夜航"
        assert len(calls) == 2  # 元数据 + 下载

    def test_clip_without_urls_is_26(self, tmp_path: Path, monkeypatch) -> None:
        item = _feed_item(CLIP_ID)
        item.pop("audio_url")
        source, _ = _source(monkeypatch, [json.dumps({"clip": item}).encode()])
        with pytest.raises(SourceRequestError) as excinfo:
            source.fetch(CLIP_ID, tmp_path)
        assert "改版" in str(excinfo.value)

    def test_registered_in_discover(self) -> None:
        assert "suno-web" in discover_sources()
