"""AI 编曲闭环（2.3 M4）：自然语言 → ``song.toml`` → 校验 → 修复循环。

三条设计铁律：

1. **LLM 只负责"写清单"，不负责"写音乐"**。产出物是 ``song.toml`` 文本，
   正确性由既有的 :class:`~sunoauxtool.compose.spec.SongSpec` 校验
   （错误码 40/44 + JSON Pointer）裁决——模型说什么不算，校验通过才算。
2. **修复闭环是确定性的**。每一轮把上一稿 + 全部结构化错误喂回去
   （2.4 起错误并行收集，不再"修一个、撞下一个"），最多 ``max_rounds`` 轮；
   耗尽仍非法 → 错误码 46（附最后一轮全部错误）。
   这与 Arranger 的"确定性种子"是同一种承诺：同样的对话历史产出同样的清单，
   可审计、可重放（temperature 固定 0）。
3. **零新依赖**。OpenAI 兼容 ``/chat/completions`` 用 stdlib ``urllib``（函数体内
   延迟导入，与 ``ai/`` 包的 P0 零 torch 约定同款）；没有 API key 时可用
   ``provider="fake"``（内置示例清单，离线自检与测试注入两用）。

环境变量（``provider="openai"`` 时）::

    SUNOAUX_AI_API_KEY    必填（缺失 → 46 no_client）
    SUNOAUX_AI_BASE_URL   选填，默认 https://api.openai.com/v1
    SUNOAUX_AI_MODEL      选填，默认 gpt-4o-mini
"""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from typing import Protocol

from sunoauxtool.compose.spec import SCHEMA_ERROR, SongSpec, SpecError, SpecGroupError
from sunoauxtool.exceptions import SmartNoteGenError

#: compose.ai 段（2.3 M4）
AI_ERROR = 46


class AIError(SmartNoteGenError):
    """AI 编曲生成失败（错误码 46）。

    Attributes:
        kind: ``no_client``（未配置）/ ``request``（网络/HTTP）/ ``bad_response``
            （响应里没有 TOML）/ ``rounds_exhausted``（修复轮次耗尽仍非法）。
    """

    code = AI_ERROR

    def __init__(self, message: str, *, kind: str, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class LLMClient(Protocol):
    """LLM 客户端最小协议（测试可注入假实现）。"""

    name: str
    model: str

    def complete(self, system: str, prompt: str) -> str:
        """给定系统提示与用户提示，返回模型输出文本。"""
        ...


class FakeLLMClient:
    """按序返回预置响应的假客户端（离线自检 / 单测注入）。"""

    def __init__(self, responses: list[str], *, name: str = "fake",
                 model: str = "fake") -> None:
        self._responses = list(responses)
        self.name = name
        self.model = model
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        if not self._responses:
            raise AIError("假客户端响应已耗尽", kind="bad_response")
        return self._responses.pop(0)


class OpenAICompatClient:
    """OpenAI 兼容 ``/chat/completions`` 客户端（stdlib urllib，零新依赖）。"""

    def __init__(self, *, base_url: str, api_key: str, model: str,
                 timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.name = "openai-compat"

    def complete(self, system: str, prompt: str) -> str:
        import urllib.error
        import urllib.request

        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # 4xx/5xx：带出服务端消息便于定位
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # pragma: no cover - 服务端无 body 时
                pass
            raise AIError(
                f"LLM 请求失败：HTTP {exc.code}", kind="request", detail=detail
            ) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise AIError(f"LLM 请求失败：{exc}", kind="request") from exc
        try:
            return str(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(
                "LLM 响应格式不符合 /chat/completions 约定",
                kind="bad_response", detail=json.dumps(body)[:300],
            ) from exc


def client_from_env(provider: str | None = None, model: str | None = None) -> LLMClient:
    """按环境变量构造客户端；``fake`` 用内置示例清单（离线可用）。"""
    chosen = (provider or os.environ.get("SUNOAUX_AI_PROVIDER", "")).strip().lower()
    if chosen == "fake":
        return FakeLLMClient([FAKE_TOML])
    api_key = os.environ.get("SUNOAUX_AI_API_KEY", "").strip()
    if not api_key:
        raise AIError(
            "未配置 LLM 凭证：请设置 SUNOAUX_AI_API_KEY（或 --provider fake 离线自检）",
            kind="no_client",
        )
    base = os.environ.get("SUNOAUX_AI_BASE_URL", "https://api.openai.com/v1").strip()
    chosen_model = (model or os.environ.get("SUNOAUX_AI_MODEL", "gpt-4o-mini")).strip()
    return OpenAICompatClient(base_url=base, api_key=api_key, model=chosen_model)


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "你是编曲清单生成器。只输出一份完整的 song.toml 文件内容，"
    "不要解释、不要前后缀、不要 markdown 标题。"
    "清单会被程序严格校验，任何未知字段/越界取值都会被打回。"
)

#: song.toml 契约摘要（给 LLM 的"字段表"；与 docs/compose.md §2 同步维护）
CONTRACT = """\
schema = 1

[song]                     # 必填
title = "曲名"             # 字符串
key = "D minor"            # <音名> <major|minor|aeolian|...>
tempo = 96                 # BPM，20..300
meter = "4/4"              # 分母必须是 2 的幂
seed = 20261024            # 整数，确定性种子
swing = 0.0                # 可选：0.5 平直 .. 0.75 三连音摇摆

[[section]]                # 段落首尾相接，起始拍自动累加；至少 1 段
id = "verse"               # 唯一
name = "主歌"              # 中文名
bars = 8                   # >= 1
energy = 0.45              # 0..1，驱动配器密度（低=铺底，高=全乐队）
progression = ["Dm", "Bb"] # 每小节一个和弦，数量须等于 bars
dynamic = "mf"             # 可选：ppp pp p mp mf f ff fff（段首力度记号）
ramp = "cresc"             # 可选：cresc | dim（段内渐强/渐弱）
tempo = 84                 # 可选：该段渐变到这个 BPM

[[track]]                  # 至少 1 轨；鼓轨 id 建议 "drums"
id = "piano"               # 唯一
name = "钢琴"              # 中文名
role = "harmony"           # melody|counter|harmony|arpeggio|pad|bass|drums|percussion
program = 0                # GM 音色号 0..127（鼓轨不需要）
articulation = "staccato"  # 可选：legato tenuto staccato staccatissimo accent
                           #        marcato spiccato pizz arco
swing = 0.62               # 可选：本轨摇摆（覆盖 song.swing）
sections = ["verse"]       # 参与的段落 id；省略 = 全曲
mix = { gain_db = -6.0, pan = -0.15, reverb_send = 0.18 }   # 可选

规则：
- 和弦写法：根音+性质（"Dm" "F" "G7" "Am" "Bb" "Cmaj7"），降号用 b 不用 ♭。
- 别写 [[track.note]]（手写音符），让 Arranger 按 energy 生成。
- drums 轨不要 program；melody/counter 至少出现在 energy >= 0.6 的段落。
- 配器要完整：流行/电子/摇滚类至少给出 bass 与 drums 轨（节奏组由 role 自动生成织体），
  纯氛围/古典类可以省略；不要用两条同 role 轨凑数。
- 全部中文注释可省略；不要输出 schema 之外的字段。
"""


def build_prompt(brief: str, *, draft: str | None = None,
                 errors: list[str] | None = None) -> str:
    """组装用户提示：契约 + 需求（修复轮追加上一稿与错误列表）。"""
    parts = [f"## song.toml 契约\n\n{CONTRACT}", f"## 编曲需求\n\n{brief.strip()}"]
    if draft and errors:
        listed = "\n".join(f"- {item}" for item in errors)
        parts.append(
            "## 上一稿（校验未通过）\n\n```toml\n" + draft.strip() + "\n```\n\n"
            "## 校验错误（逐条修复，输出完整修正后的 song.toml）\n\n" + listed
        )
    return "\n\n".join(parts)


def extract_toml(text: str) -> str:
    """从模型输出中取 TOML：优先 ```toml 围栏，否则从清单起点截取。"""
    body = str(text).strip()
    if "```" in body:
        for chunk in body.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("toml"):
                chunk = chunk[4:].strip()
            if "schema" in chunk or "[song]" in chunk:
                return chunk
    for marker in ("schema =", "[song]"):
        index = body.find(marker)
        if index >= 0:
            return body[index:]
    raise AIError("模型响应中没有 song.toml 内容", kind="bad_response",
                  detail=body[:300])


def parse_spec(text: str) -> SongSpec:
    """TOML 文本 → 已校验的 :class:`SongSpec`（40/44 错误原样抛出）。"""
    spec, errors = spec_errors(text)
    if errors:
        raise errors[0]
    assert spec is not None  # errors 为空时必有 spec
    return spec


#: 修复轮提示词里最多列出的错误条数（超出部分汇总成一行，避免 prompt 失控）
MAX_PROMPT_ERRORS = 12


def spec_errors(text: str) -> tuple[SongSpec | None, list[SpecError]]:
    """TOML 文本 → ``(spec, errors)``；``errors`` 非空表示未通过校验。

    2.4 起错误**并行收集**（``collect_all=True``）：一个坏轨道不再遮住后面的
    坏段落，修复循环一轮就能看到全部问题。TOML 语法错误仍只有一条
    （语法坏了就谈不上逐项收集）。
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return None, [SpecError("/", f"TOML 语法错误: {exc}", SCHEMA_ERROR)]
    try:
        spec = SongSpec.from_dict(data, collect_all=True)
    except SpecGroupError as exc:
        return None, list(exc.errors)
    try:
        spec.validate(collect_all=True)
    except SpecGroupError as exc:
        return None, list(exc.errors)
    return spec, []


# ---------------------------------------------------------------------------
# 生成闭环
# ---------------------------------------------------------------------------


@dataclass
class RoundReport:
    """一轮"生成 → 校验"的记录。"""

    index: int                      # 1 起
    prompt: str
    toml_text: str
    errors: list[str] = field(default_factory=list)
    ok: bool = False


@dataclass
class GenerateResult:
    """AI 编曲结果（清单已通过全部校验）。"""

    spec: SongSpec
    toml_text: str
    provider: str
    model: str
    rounds: list[RoundReport] = field(default_factory=list)

    @property
    def attempts(self) -> int:
        """总轮数（含失败的）。"""
        return len(self.rounds)

    @property
    def fixed_errors(self) -> int:
        """修复轮里出现过的错误总数。"""
        return sum(len(r.errors) for r in self.rounds[:-1])

    def describe(self) -> list[str]:
        """人类可读摘要（CLI 直接逐行打印）。"""
        spec = self.spec
        notes = sum(len(t.notes) for t in spec.tracks)
        lines = [
            f"模型 {self.model}（{self.provider}）｜第 {self.attempts} 轮通过｜"
            f"修复错误 {self.fixed_errors} 处",
            f"段落 {len(spec.sections)} 个｜轨道 {len(spec.tracks)} 条｜"
            f"手写音符 {notes} 个（其余由 Arranger 生成）",
        ]
        return lines


def generate_spec(
    brief: str,
    client: LLMClient,
    *,
    max_rounds: int = 3,
) -> GenerateResult:
    """生成 → 校验 → 修复循环，返回已通过校验的清单。

    Args:
        brief: 一句话编曲需求。
        client: LLM 客户端（测试注入 :class:`FakeLLMClient`）。
        max_rounds: 最大轮数（含首轮）；耗尽仍非法 → 46。

    Raises:
        AIError: 响应不可解析（kind=bad_response）或轮次耗尽（kind=rounds_exhausted）。
    """
    if not str(brief).strip():
        raise AIError("编曲需求不能为空", kind="no_client")
    draft: str | None = None
    errors: list[str] = []
    rounds: list[RoundReport] = []
    for index in range(1, max(1, int(max_rounds)) + 1):
        prompt = build_prompt(brief, draft=draft, errors=errors)
        text = client.complete(SYSTEM_PROMPT, prompt)
        toml_text = extract_toml(text)
        spec, found = spec_errors(toml_text)
        if found:
            # 错误并行收集：一轮把全部问题喂回去，不再"修一个、撞下一个"
            errors = [str(item) for item in found[:MAX_PROMPT_ERRORS]]
            if len(found) > MAX_PROMPT_ERRORS:
                errors.append(f"…另有 {len(found) - MAX_PROMPT_ERRORS} 处错误未列出")
            rounds.append(RoundReport(
                index=index, prompt=prompt, toml_text=toml_text,
                errors=[str(item) for item in found],
            ))
            draft = toml_text
            continue
        rounds.append(RoundReport(
            index=index, prompt=prompt, toml_text=toml_text, ok=True,
        ))
        return GenerateResult(
            spec=spec, toml_text=toml_text,
            provider=client.name, model=client.model, rounds=rounds,
        )
    last = rounds[-1].errors if rounds else []
    shown = last[:MAX_PROMPT_ERRORS]
    suffix = f"（另有 {len(last) - MAX_PROMPT_ERRORS} 处未列出）" if len(last) > MAX_PROMPT_ERRORS else ""
    raise AIError(
        f"AI 编曲在 {max_rounds} 轮内未产出合法清单；最后一轮 {len(last)} 处错误："
        + "；".join(shown) + suffix,
        kind="rounds_exhausted", detail="\n".join(last),
    )


#: 内置示例清单（provider=fake 时返回；同时是文档示例）
FAKE_TOML = """\
schema = 1

[song]
title = "城市夜车"
key = "A minor"
tempo = 92
meter = "4/4"
seed = 20261024
swing = 0.62

[[section]]
id = "intro"
name = "前奏"
bars = 4
energy = 0.30
progression = ["Am", "F", "C", "G"]
dynamic = "p"

[[section]]
id = "verse"
name = "主歌"
bars = 8
energy = 0.55
progression = ["Am", "F", "C", "G", "Am", "F", "C", "G"]
dynamic = "mf"
ramp = "cresc"

[[section]]
id = "chorus"
name = "副歌"
bars = 8
energy = 0.90
progression = ["F", "G", "Am", "Am", "F", "G", "Am", "E"]
dynamic = "ff"
ramp = "cresc"

[[track]]
id = "piano"
name = "钢琴"
role = "harmony"
program = 0
articulation = "staccato"
sections = ["verse", "chorus"]
mix = { gain_db = -6.0, pan = -0.15, reverb_send = 0.18 }

[[track]]
id = "bass"
name = "贝斯"
role = "bass"
program = 33
sections = ["verse", "chorus"]
mix = { gain_db = -3.0 }

[[track]]
id = "lead"
name = "主奏旋律"
role = "melody"
articulation = "accent"
swing = 0.66
sections = ["chorus"]

[[track]]
id = "drums"
name = "鼓组"
role = "drums"
groove = "pop"
sections = ["verse", "chorus"]
mix = { gain_db = -4.0 }

[[track]]
id = "strings"
name = "弦乐铺底"
role = "pad"
sections = ["intro", "chorus"]
mix = { gain_db = -10.0, pan = 0.2, reverb_send = 0.35 }
"""
