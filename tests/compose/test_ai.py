"""AI 编曲闭环（compose/ai.py + `compose ai`）单测。

验收口径：LLM 只产出清单文本，正确性由 SongSpec 校验裁决；
修复闭环确定性可重放；无新依赖（OpenAI 兼容走 stdlib urllib）。
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sunoauxtool.compose.ai import (
    AI_ERROR,
    AIError,
    FakeLLMClient,
    OpenAICompatClient,
    build_prompt,
    client_from_env,
    extract_toml,
    generate_spec,
    parse_spec,
)
from sunoauxtool.compose.cli import app as compose_app
from sunoauxtool.exceptions import ERROR_CODES

_runner = CliRunner()

GOOD_TOML = """\
schema = 1

[song]
title = "夜航"
key = "D minor"
tempo = 96
seed = 7

[[section]]
id = "verse"
name = "主歌"
bars = 2
energy = 0.5
progression = ["Dm", "Bb"]

[[track]]
id = "piano"
name = "钢琴"
role = "harmony"
program = 0
"""

BAD_TOML = """\
schema = 1

[song]
title = "夜航"
key = "D minor"
tempo = 99999

[[section]]
id = "verse"
bars = 2
energy = 0.5
progression = ["Dm", "Bb", "F"]

[[track]]
id = "piano"
role = "harp"
program = 0
"""


class TestPrompt:
    def test_contains_contract_and_brief(self) -> None:
        prompt = build_prompt("一首 synthwave")
        assert "schema = 1" in prompt
        assert "一首 synthwave" in prompt
        assert "上一稿" not in prompt

    def test_repair_round_includes_draft_and_errors(self) -> None:
        prompt = build_prompt("brief", draft=BAD_TOML, errors=["/song/tempo: 必须在 20..300"])
        assert "## 上一稿" in prompt
        assert "/song/tempo" in prompt
        assert "[[section]]" in prompt  # 上一稿原文在围栏里

    def test_system_prompt_forbids_prose(self) -> None:
        from sunoauxtool.compose.ai import SYSTEM_PROMPT
        assert "只输出" in SYSTEM_PROMPT


class TestExtractToml:
    def test_fenced_block(self) -> None:
        text = "好的，这是清单：\n```toml\n" + GOOD_TOML + "\n```\n希望符合要求。"
        assert extract_toml(text) == GOOD_TOML.strip()

    def test_bare_toml_with_prose(self) -> None:
        text = "当然。\n" + GOOD_TOML + "\n以上。"
        assert extract_toml(text).startswith("schema = 1")

    def test_no_toml_raises(self) -> None:
        with pytest.raises(AIError) as excinfo:
            extract_toml("抱歉，我不太明白你的意思。")
        assert excinfo.value.kind == "bad_response"
        assert excinfo.value.code == AI_ERROR


class TestParseSpec:
    def test_good_toml_passes(self) -> None:
        spec = parse_spec(GOOD_TOML)
        assert spec.title == "夜航"

    def test_bad_toml_raises_spec_error(self) -> None:
        # from_dict 只抛第一条错误（结构先于语义）——修复循环逐轮收敛正是为此
        with pytest.raises(Exception) as excinfo:  # SpecError(40/44)
            parse_spec(BAD_TOML)
        assert "/track/0/role" in str(excinfo.value)


class TestGenerate:
    def test_first_round_success(self) -> None:
        result = generate_spec("一首曲子", FakeLLMClient([GOOD_TOML]))
        assert result.attempts == 1
        assert result.rounds[0].ok
        assert result.fixed_errors == 0
        assert result.spec.title == "夜航"

    def test_repair_loop_fixes_errors(self) -> None:
        client = FakeLLMClient([BAD_TOML, GOOD_TOML])
        result = generate_spec("一首曲子", client, max_rounds=3)
        assert result.attempts == 2
        assert result.fixed_errors >= 1
        assert result.rounds[0].errors
        assert result.rounds[-1].ok

    def test_rounds_exhausted(self) -> None:
        client = FakeLLMClient([BAD_TOML] * 5)
        with pytest.raises(AIError) as excinfo:
            generate_spec("一首曲子", client, max_rounds=2)
        assert excinfo.value.kind == "rounds_exhausted"
        assert excinfo.value.code == AI_ERROR
        assert "/track/0/role" in excinfo.value.detail

    def test_repair_loop_respects_round_budget(self) -> None:
        client = FakeLLMClient([BAD_TOML] * 5)
        with pytest.raises(AIError):
            generate_spec("一首曲子", client, max_rounds=3)
        assert len(client.calls) == 3  # 没有超出预算的额外请求

    def test_empty_brief(self) -> None:
        with pytest.raises(AIError) as excinfo:
            generate_spec("   ", FakeLLMClient([GOOD_TOML]))
        assert excinfo.value.kind == "no_client"


class TestClients:
    def test_env_requires_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUNOAUX_AI_API_KEY", raising=False)
        monkeypatch.delenv("SUNOAUX_AI_PROVIDER", raising=False)
        with pytest.raises(AIError) as excinfo:
            client_from_env()
        assert excinfo.value.kind == "no_client"

    def test_env_fake_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUNOAUX_AI_PROVIDER", "fake")
        client = client_from_env()
        assert isinstance(client, FakeLLMClient)
        result = generate_spec("示例", client)
        assert result.spec.title == "城市夜车"

    def test_openai_request_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}

        class _Resp(io.BytesIO):
            def __enter__(self):  # noqa: D105
                return self

            def __exit__(self, *args):  # noqa: D105
                return False

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            body = json.dumps(
                {"choices": [{"message": {"content": GOOD_TOML}}]}
            ).encode("utf-8")
            return _Resp(body)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        client = OpenAICompatClient(
            base_url="https://api.example.com/v1", api_key="sk-test", model="glm-5"
        )
        assert client.complete("sys", "user") == GOOD_TOML
        assert captured["url"] == "https://api.example.com/v1/chat/completions"
        assert captured["payload"]["model"] == "glm-5"
        assert captured["payload"]["temperature"] == 0
        roles = [m["role"] for m in captured["payload"]["messages"]]
        assert roles == ["system", "user"]
        assert any("sk-test" in value for value in captured["headers"].values())

    def test_openai_http_error_maps_to_46(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"denied")
            )

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        client = OpenAICompatClient(
            base_url="https://api.example.com/v1", api_key="bad", model="m"
        )
        with pytest.raises(AIError) as excinfo:
            client.complete("sys", "user")
        assert excinfo.value.kind == "request"
        assert "401" in str(excinfo.value)

    def test_openai_bad_shape_maps_to_46(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Resp(io.BytesIO):
            def __enter__(self):  # noqa: D105
                return self

            def __exit__(self, *args):  # noqa: D105
                return False

        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda request, timeout=None: _Resp(b'{"error": "oops"}'),
        )
        client = OpenAICompatClient(
            base_url="https://api.example.com/v1", api_key="k", model="m"
        )
        with pytest.raises(AIError) as excinfo:
            client.complete("sys", "user")
        assert excinfo.value.kind == "bad_response"


class TestCli:
    def test_fake_provider_end_to_end(self, tmp_path: Path) -> None:
        out = tmp_path / "song.toml"
        result = _runner.invoke(compose_app, [
            "ai", "一首 synthwave 风格的曲子", "-o", str(out),
            "--provider", "fake", "--json",
        ])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["rounds"] == 1
        assert payload["sections"] >= 3 and payload["tracks"] >= 4
        # 产物能被既有 validate 通过（闭环终点是"校验说了算"）
        check = _runner.invoke(compose_app, ["validate", str(out)])
        assert check.exit_code == 0, check.output

    def test_no_key_is_46(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SUNOAUX_AI_API_KEY", raising=False)
        monkeypatch.delenv("SUNOAUX_AI_PROVIDER", raising=False)
        result = _runner.invoke(
            compose_app, ["ai", "一首曲子", "-o", str(tmp_path / "s.toml")]
        )
        assert result.exit_code == AI_ERROR, result.output
        assert "SUNOAUX_AI_API_KEY" in result.output

    def test_help_mentions_flags(self) -> None:
        import re
        plain = re.sub(chr(27) + r"\[[0-9;]*m", "",
                       _runner.invoke(compose_app, ["ai", "--help"]).output)
        for flag in ("--rounds", "--provider", "--build", "--print-prompt"):
            assert flag in plain, flag


def test_error_code_registered() -> None:
    entry = next(item for item in ERROR_CODES if item[0] == AI_ERROR)
    assert "AI 编曲" in entry[1]
