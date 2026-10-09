"""R8 suno-dl 三段式下载测试（mock HTTP，零真实网络）。

口径与 download/suno_dl.py docstring 一致（两端同步修改）：
- 段 1 CDN 直拼 cdn1.suno.ai/{id}.{ext}（匿名）；
- 段 2 载荷 GET {base}/api/clip/{id}（需 Cookie）-> audio_url/video_url；
- 段 3 官方轮询 GET {base}/api/download/clip/{id}?format={fmt}（ready -> download_url）；
  wav 走 gen wav_file / convert_wav 轮询；
- 错误码 30-34；批量部分失败 8 / 全失败 9。
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from typer.testing import CliRunner

from sunoauxtool.aggregate import app
from sunoauxtool.download.suno_dl import (
    CLIP_ID_RE,
    SunoDlAllPathsFailedError,
    SunoDlCredentialError,
    SunoDlPollTimeoutError,
    SunoDownloader,
    extract_clip_id,
    sanitize_filename,
    unique_path,
)
from sunoauxtool.exceptions import ParameterError

runner = CliRunner()
CLIP = "203df63f-bbea-4cc7-9345-423f70ceb153"
BAD = "11111111-2222-4333-8444-555555555555"
AUDIO = b"ID3" + b"\x11" * 4096  # 满足 _MIN_AUDIO_BYTES 的假音频
JSON_H = {"Content-Type": "application/json"}
FAIL_JSON = (200, b'{"error":"gone"}', JSON_H)


# ---------------------------------------------------------------------------
# 纯工具
# ---------------------------------------------------------------------------


def test_sanitize_filename():
    assert sanitize_filename("My Song: A/B?") == "My_Song-_A-B-"
    assert len(sanitize_filename("x" * 300)) == 200
    assert sanitize_filename("   ") == "untitled"


def test_unique_path(tmp_path):
    target = tmp_path / "song.mp3"
    target.write_bytes(b"a")
    assert unique_path(target) == tmp_path / "song-1.mp3"
    (tmp_path / "song-1.mp3").write_bytes(b"b")
    assert unique_path(target) == tmp_path / "song-2.mp3"


def test_extract_clip_id():
    assert extract_clip_id(f"https://suno.com/song/{CLIP}/") == CLIP
    assert extract_clip_id(CLIP) == CLIP
    with pytest.raises(ParameterError):
        extract_clip_id("not-a-clip")
    assert CLIP_ID_RE.fullmatch(CLIP)
    assert not CLIP_ID_RE.fullmatch(CLIP.upper())  # 只认小写 UUID


# ---------------------------------------------------------------------------
# HTTP mock 基建
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status = status
        self.headers = headers or {"Content-Type": "audio/mpeg"}

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _scripted(responses: list, calls: list):
    """按请求顺序返回预设。项: bytes=200音频; (status, bytes, headers)=指定响应。"""

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        item = responses[len(calls) - 1]
        if isinstance(item, tuple):
            return _FakeResponse(item[1], status=item[0], headers=item[2])
        return _FakeResponse(item)

    return fake_urlopen


def _dl(responses, cookie="sess=abc1234567", calls=None) -> SunoDownloader:
    return SunoDownloader(
        cookie=cookie,
        api_base="https://api.test",
        urlopen=_scripted(responses, calls if calls is not None else []),
        sleep=lambda *_: None,
    )


# ---------------------------------------------------------------------------
# 三段式路径
# ---------------------------------------------------------------------------


def test_stage1_cdn_direct(tmp_path):
    calls: list = []
    dl = _dl([AUDIO], cookie=None, calls=calls)  # 匿名也能 CDN 直取
    f = dl.download(CLIP, tmp_path)
    assert f.path.read_bytes() == AUDIO
    assert f.meta["via"] == "cdn"
    assert f.path.name == f"{CLIP}.mp3"
    assert calls[0].full_url == f"https://cdn1.suno.ai/{CLIP}.mp3"


def test_stage1_cdn_soft404_falls_to_payload(tmp_path):
    calls: list = []
    payload = json.dumps(
        {"title": "My Cool Song", "audio_url": "https://cdn.test/x.mp3"}
    ).encode()
    dl = _dl([FAIL_JSON, payload, AUDIO], calls=calls)
    f = dl.download(CLIP, tmp_path)
    assert f.meta["via"] == "payload"
    assert f.path.name == "My_Cool_Song.mp3"  # title 经 sanitize
    # 请求顺序：cdn -> api/clip -> audio_url
    assert "/api/clip/" in calls[1].full_url
    assert calls[1].get_header("Cookie") == "sess=abc1234567"
    assert calls[2].full_url == "https://cdn.test/x.mp3"


def test_stage3_poll_ready(tmp_path):
    calls: list = []
    processing = json.dumps({"status": "processing"}).encode()
    ready = json.dumps(
        {"status": "ready", "download_url": "https://presigned.test/a.mp3"}
    ).encode()
    payload = json.dumps({"title": "Polled Song"}).encode()
    dl = _dl(
        [FAIL_JSON, payload, processing, processing, ready, AUDIO, payload],
        calls=calls,
    )
    f = dl.download(CLIP, tmp_path)
    assert f.meta["via"] == "download-clip"
    assert f.path.name == "Polled_Song.mp3"
    assert "format=mp3" in calls[2].full_url


def test_stage3_poll_timeout_then_34(tmp_path):
    processing = json.dumps({"status": "processing"}).encode()
    dl = _dl(
        [FAIL_JSON, b"{}", processing, processing, processing],
    )
    with pytest.raises(SunoDlAllPathsFailedError) as exc:
        dl.download(CLIP, tmp_path, poll_max=3, poll_interval=0.0)
    assert exc.value.code == 34
    assert "- poll:" in exc.value.message


def test_no_cookie_and_cdn_fail_exit_30(tmp_path):
    dl = _dl([FAIL_JSON], cookie=None)
    with pytest.raises(SunoDlCredentialError) as exc:
        dl.download(CLIP, tmp_path)
    assert exc.value.code == 30


def test_wav_skips_payload_stage(tmp_path):
    calls: list = []
    dl = _dl(
        [(200, b'{}', JSON_H), AUDIO],  # cdn .wav 失败 -> gen wav_file 直取
        calls=calls,
    )
    f = dl.download(CLIP, tmp_path, fmt="wav")
    assert f.meta["via"] == "gen-wav-file"
    assert f.path.name == f"{CLIP}.wav"
    assert calls[0].full_url.endswith(f"/{CLIP}.wav")
    assert calls[1].full_url.endswith(f"/api/gen/{CLIP}/wav_file/")
    # 主路径（cdn -> wav_file）不经过 api/clip 载荷段；其后的 title 获取为 best-effort
    assert all("/api/clip/" not in c.full_url for c in calls[:2])


def test_wav_convert_poll(tmp_path):
    calls: list = []
    dl = _dl(
        [
            (200, b'{}', JSON_H),        # cdn .wav 失败
            (404, b"", {"Content-Type": "text/html"}),  # wav_file 无
            b"",                          # convert_wav POST（结果忽略）
            (404, b"", {"Content-Type": "text/html"}),  # 轮询 1 未就绪
            AUDIO,                        # 轮询 2 就绪
        ],
        calls=calls,
    )
    f = dl.download(CLIP, tmp_path, fmt="wav", wav_poll_max=3, wav_poll_interval=0.0)
    assert f.meta["via"] == "convert-wav"
    assert "convert_wav" in calls[2].full_url


def test_authorize_called_when_flag(tmp_path):
    calls: list = []
    dl = _dl([b'{"ok":true}', AUDIO], calls=calls)
    dl.download(CLIP, tmp_path, authorize=True)
    assert calls[0].full_url.endswith("/api/download/authorize")
    assert calls[0].get_method() == "POST"
    assert json.loads(calls[0].data) == {"item_id": CLIP, "item_type": "clip"}


def test_bad_fmt_rejected(tmp_path):
    dl = _dl([], cookie=None)
    with pytest.raises(ParameterError):
        dl.download(CLIP, tmp_path, fmt="flac")


def test_payload_garbage_json_falls_to_poll(tmp_path):
    processing = json.dumps({"status": "processing"}).encode()
    dl = _dl(
        [FAIL_JSON, b"<html>not json</html>", processing, processing],
    )
    with pytest.raises(SunoDlAllPathsFailedError) as exc:
        dl.download(CLIP, tmp_path, poll_max=2, poll_interval=0.0)
    assert exc.value.code == 34
    # 失败原因同时含 payload 与 poll 两条
    assert "payload" in exc.value.message and "poll" in exc.value.message


# ---------------------------------------------------------------------------
# dry-run 自检（凭证掩码，不回显明文）
# ---------------------------------------------------------------------------


def test_check_masks_cookie():
    out = SunoDownloader(cookie="abcd1234efgh").check(CLIP)
    assert "abcd***gh" in out
    assert "abcd1234efgh" not in out
    assert "非法" in SunoDownloader().check("nope")


# ---------------------------------------------------------------------------
# CLI（批量汇总 / uniquify / dry-run）
# ---------------------------------------------------------------------------


def _patch_cli_dl(monkeypatch, responses):
    """替换 CLI 内部 import 的 SunoDownloader 为注入 mock urlopen 的子类。"""
    import sunoauxtool.download.suno_dl as mod

    calls: list = []

    class _FakeDL(SunoDownloader):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.urlopen = _scripted(responses, calls)
            self.sleep = lambda *_: None

    monkeypatch.setattr(mod, "SunoDownloader", _FakeDL)
    return calls


def test_cli_cdn_download_and_uniquify(tmp_path, monkeypatch):
    _patch_cli_dl(monkeypatch, [AUDIO, AUDIO])
    result = runner.invoke(app, ["post", "suno-dl", CLIP, "-o", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / f"{CLIP}.mp3").is_file()
    # 再跑一次：uniquify 追加 -1
    result2 = runner.invoke(app, ["post", "suno-dl", CLIP, "-o", str(tmp_path)])
    assert result2.exit_code == 0, result2.output
    assert (tmp_path / f"{CLIP}-1.mp3").is_file()


def test_cli_batch_partial_exit_8(tmp_path, monkeypatch):
    import sunoauxtool.download.suno_dl as mod

    calls = _patch_cli_dl(monkeypatch, [AUDIO])

    class _FailingDL(mod.SunoDownloader):
        def download(self, clip_id, out_dir, **kw):
            if clip_id == BAD:
                raise SunoDlPollTimeoutError("超时", code=33)
            return super().download(clip_id, out_dir, **kw)

    monkeypatch.setattr(mod, "SunoDownloader", _FailingDL)
    result = runner.invoke(
        app, ["post", "suno-dl", CLIP, BAD, "-o", str(tmp_path)]
    )
    assert result.exit_code == 8
    assert "成功 1 个、失败 1 个" in result.output


def test_cli_batch_all_fail_exit_9(tmp_path, monkeypatch):
    import sunoauxtool.download.suno_dl as mod

    _patch_cli_dl(monkeypatch, [(200, b'{"error":"gone"}', JSON_H)])
    result = runner.invoke(
        app, ["post", "suno-dl", BAD, CLIP, "-o", str(tmp_path)]
    )
    # 匿名 CDN 失败且无 Cookie -> 30（凭证）汇总为全失败 9
    assert result.exit_code == 9
    assert "全部失败" in result.output


def test_cli_dry_run_masks_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("SUNO_DL_COOKIE", "abcd1234efgh")
    result = runner.invoke(app, ["post", "suno-dl", CLIP, "--dry-run", "-o", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "abcd***gh" in result.output
    assert "abcd1234efgh" not in result.output


def test_cli_bad_fmt(tmp_path):
    result = runner.invoke(app, ["post", "suno-dl", CLIP, "--fmt", "flac", "-o", str(tmp_path)])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# 错误码表登记
# ---------------------------------------------------------------------------


def test_error_codes_registered():
    from sunoauxtool.exceptions import ERROR_CODES

    registered = {c for c, *_ in ERROR_CODES}
    assert {30, 31, 32, 33, 34} <= registered
