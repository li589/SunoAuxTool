"""relay-music 源契约测试（2.5 通道 A）：mock HTTP + 任务式轮询注入。

只 mock HTTP 层（urlopen 脚本化注入）与轮询时钟（sleeper 注入），
状态判定/错误码/文件落盘全部走真实实现。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sunoauxtool.download.sources.base import discover_sources
from sunoauxtool.download.sources.relay_music import (
    DEFAULT_BASE_URL,
    DEFAULT_MUSIC_MODEL,
    MediaTaskError,
    RelayMusicSource,
)
from sunoauxtool.exceptions import SourceCredentialError, SourceRequestError

TASK_ID = "task-1793261066130-37700"


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
    """按调用序消费响应；元素为 (status_code, body) 或 Exception。"""
    calls: list = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(item)

    return fake_urlopen, calls


def _submit_body(task_id: str = TASK_ID) -> bytes:
    return json.dumps({"code": 200, "data": {"status": "pending", "task_id": task_id}}).encode()


def _final_body(*urls: str) -> bytes:
    return json.dumps({
        "id": TASK_ID, "is_final": True, "status": "succeeded",
        "data": [{"type": "audio", "url": u} for u in urls],
    }).encode()


def _pending_body() -> bytes:
    return json.dumps({"id": TASK_ID, "is_final": False, "status": "pending"}).encode()


@pytest.fixture()
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SUNOAUX_RELAY_API_KEY", "SUNOAUX_AI_API_KEY",
                 "SUNOAUX_RELAY_BASE_URL", "SUNOAUX_RELAY_MUSIC_MODEL"):
        monkeypatch.delenv(name, raising=False)


class TestCredential:
    def test_missing_key_is_25(self, _no_env, monkeypatch: pytest.MonkeyPatch,
                               tmp_path: Path) -> None:
        # 配置文件可能带 key：临时改走一个必然为空的构造参数
        source = RelayMusicSource(api_key="", base_url=DEFAULT_BASE_URL)
        monkeypatch.setattr(type(source), "_resolve_key",
                            lambda self: "", raising=False)
        with pytest.raises(SourceCredentialError) as excinfo:
            source.check("一段描述")
        assert excinfo.value.code == 25

    def test_key_falls_back_to_ai_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUNOAUX_AI_API_KEY", "sk-shared")
        source = RelayMusicSource(api_key="", base_url=DEFAULT_BASE_URL)
        assert source._resolve_key() == "sk-shared"

    def test_check_masks_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUNOAUX_RELAY_API_KEY", "sk-omni-1234567890abcd")
        source = RelayMusicSource(base_url=DEFAULT_BASE_URL, model=DEFAULT_MUSIC_MODEL)
        text = source.check("城市漫步")
        assert "sk-o***cd" in text
        assert "sk-omni-1234567890abcd" not in text
        assert "music" in text


class TestSubmitAndPoll:
    def test_submit_returns_task_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_urlopen, calls = _scripted([_submit_body()])
        source = RelayMusicSource(api_key="sk-test", base_url="https://relay.example/v1")
        source.urlopen = fake_urlopen
        assert source.submit("城市漫步") == TASK_ID
        req = calls[0]
        assert req.full_url == "https://relay.example/v1/media/generate"
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["model"] == "music"
        assert payload["prompt"] == "城市漫步"
        assert req.get_header("Authorization") == "Bearer sk-test"

    def test_submit_without_task_id_is_26(self, monkeypatch) -> None:
        fake_urlopen, _ = _scripted([json.dumps({"code": 200}).encode()])
        source = RelayMusicSource(api_key="sk-test", base_url=DEFAULT_BASE_URL)
        source.urlopen = fake_urlopen
        with pytest.raises(SourceRequestError) as excinfo:
            source.submit("x")
        assert excinfo.value.code == 26

    def test_wait_polls_until_final(self, monkeypatch) -> None:
        url = "https://cdn.example/a.mp3"
        fake_urlopen, _ = _scripted([_pending_body(), _pending_body(), _final_body(url)])
        source = RelayMusicSource(api_key="sk-test", base_url=DEFAULT_BASE_URL)
        source.urlopen = fake_urlopen
        polls: list[int] = []
        urls = source.wait_task(TASK_ID, interval_s=0.0, on_poll=lambda n, _d: polls.append(n))
        assert urls == [url]
        assert polls == [1, 2, 3]

    def test_wait_timeout_is_47(self, monkeypatch) -> None:
        fake_urlopen, calls = _scripted([])
        source = RelayMusicSource(api_key="sk-test", base_url=DEFAULT_BASE_URL)
        source.urlopen = fake_urlopen
        source.sleeper = lambda _s: None
        with pytest.raises(MediaTaskError) as excinfo:
            source.wait_task(TASK_ID, timeout_s=0.0, interval_s=0.01)
        assert excinfo.value.code == 47
        assert calls == []  # 零超时预算：一次请求都不发

    def test_failed_status_is_47(self, monkeypatch) -> None:
        body = json.dumps({"id": TASK_ID, "is_final": False, "status": "failed"}).encode()
        fake_urlopen, _ = _scripted([body])
        source = RelayMusicSource(api_key="sk-test", base_url=DEFAULT_BASE_URL)
        source.urlopen = fake_urlopen
        with pytest.raises(MediaTaskError) as excinfo:
            source.wait_task(TASK_ID, interval_s=0.0)
        assert excinfo.value.code == 47

    def test_final_without_audio_is_47(self, monkeypatch) -> None:
        body = json.dumps({"id": TASK_ID, "is_final": True, "status": "ok", "data": []}).encode()
        fake_urlopen, _ = _scripted([body])
        source = RelayMusicSource(api_key="sk-test", base_url=DEFAULT_BASE_URL)
        source.urlopen = fake_urlopen
        with pytest.raises(MediaTaskError) as excinfo:
            source.wait_task(TASK_ID, interval_s=0.0)
        assert excinfo.value.code == 47


class TestFetch:
    def test_fetch_downloads_all_versions(self, tmp_path: Path, monkeypatch) -> None:
        u1, u2 = "https://cdn.example/a.mp3", "https://cdn.example/b.mp3"
        fake_urlopen, calls = _scripted([
            _submit_body(), _final_body(u1, u2), b"MP3DATA1", b"MP3DATA2",
        ])
        source = RelayMusicSource(api_key="sk-test", base_url=DEFAULT_BASE_URL)
        source.urlopen = fake_urlopen
        source.sleeper = lambda _s: None
        files = source.fetch("城市漫步", tmp_path)
        assert [f.path.name for f in files] == [f"{TASK_ID}-v1.mp3", f"{TASK_ID}-v2.mp3"]
        assert files[0].path.read_bytes() == b"MP3DATA1"
        assert files[0].meta["task_id"] == TASK_ID
        assert len(calls) == 4

    def test_empty_prompt_is_26(self, tmp_path: Path) -> None:
        source = RelayMusicSource(api_key="sk-test", base_url=DEFAULT_BASE_URL)
        with pytest.raises(SourceRequestError):
            source.fetch("  ", tmp_path)

    def test_registered_in_discover(self) -> None:
        assert "relay-music" in discover_sources()


class TestCli:
    """聚合入口级：post relay-music / post fetch -s relay-music。"""

    def test_dry_run_masks_and_reports(self, monkeypatch) -> None:
        import re
        from sunoauxtool.aggregate import app

        monkeypatch.setenv("SUNOAUX_RELAY_API_KEY", "sk-omni-1234567890abcd")
        result = CliRunner().invoke(
            app, ["post", "relay-music", "--dry-run", "城市漫步", "--model", "music"]
        )
        assert result.exit_code == 0, result.output
        plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
        assert "sk-o***cd" in plain
        assert "sk-omni-1234567890abcd" not in plain

    def test_fetch_dry_run_unknown_prompt_still_checks_credential(
        self, monkeypatch
    ) -> None:
        from sunoauxtool.aggregate import app

        monkeypatch.delenv("SUNOAUX_RELAY_API_KEY", raising=False)
        monkeypatch.delenv("SUNOAUX_AI_API_KEY", raising=False)
        result = CliRunner().invoke(
            app, ["post", "fetch", "--dry-run", "-s", "relay-music", "x"]
        )
        assert result.exit_code == 25

    def test_bad_params_json_is_1(self, monkeypatch) -> None:
        from sunoauxtool.aggregate import app

        monkeypatch.setenv("SUNOAUX_RELAY_API_KEY", "sk-test")
        result = CliRunner().invoke(
            app, ["post", "relay-music", "x", "--params", "{bad"]
        )
        assert result.exit_code == 1
