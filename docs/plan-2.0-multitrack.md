# 2.0 多轨编曲与音源体系 · 计划书

> 状态：草案 v1 · 基线版本：1.9.1 · 目标版本：2.0.0 起 · 日期：2026-10-24
> 配套文档：[refactor-2.0-multitrack.md](refactor-2.0-multitrack.md)（技术重构方案）

---

## 0. 一页摘要

**一句话目标**：把 SunoAuxTool 从"固定 3–4 轨的单曲生成器"升级为**代码驱动、可复现、可交付总谱的多轨数字音乐制作内核**，并为后续接入广泛音源库（SF2/SFZ/VST 等）留出稳定抽象。

**三个交付物**（2.0.0 必须全部落地）：

1. **Song 编曲模型**：段落（Section）× 轨道（Track）× 片段（Clip）的完整编曲数据模型，支持速度/拍号/调性变化表、每轨音色/通道/声像/音量。
2. **Arranger 编曲引擎**：给定 `song.toml` 清单 + 种子，生成 8–16 轨、带前奏/主歌/副歌/间奏/尾奏、每轨音色与角色明确的完整 MIDI。
3. **渲染/混音/总谱三件套**：多音源库渲染、分轨（stems）导出 + 软件混音、多声部总谱（SVG/PNG/MusicXML）导出。

**里程碑**：

| 里程碑 | 版本 | 主题 | 一句话验收 |
|---|---|---|---|
| M1 | 2.0.0 | 多轨内核 | 一个 TOML → 8 轨 2 分钟成品（MIDI+混音+分轨+总谱），同种子字节级可复现 |
| M2 | 2.1.0 | 音源库 | 注册表管理 ≥2 套音源，每轨可独立选库/选音色，响度自动对齐 |
| M3 | 2.2.0 | 表现力 | 演奏法/力度/人性化/摇摆进 MIDI，总谱可见记号 |
| M4 | 2.3.0 | AI 编曲 | LLM 产出清单 → 校验 → 渲染闭环；音频反推编曲草案 |
| M4.5 | 2.5.0 | 出版级总谱 + Suno 接入 | 大谱表（钢琴两行）+ 分页；`post relay-music` 中转站生成、`post suno-web` Web 凭证只读（P1） |
| M5 | 3.0.0 | 平台化 | 工程项目制、专辑批量、插件式音源后端、MV 流水线整合 |

---

## 1. 背景：能力缺口与愿景

### 1.1 现状（1.9.1 实测）

| 维度 | 现状 | 证据 |
|---|---|---|
| 轨道 | `generate midi` 固定 chords/melody/bass[+drums] 四轨，风格预设写死 3 个 GM 音色 | `generators/procedural.py:29-52,61-107` |
| 时间 | 全曲单一 `bpm: int`，无速度/拍号变化 | `models/notes.py:66-71`、`models/midi.py:40-42` |
| 音色 | 全曲单一 SoundFont，无每轨选库/选音色 | `render/fluidsynth.py:30-37`、`config/default.toml [paths] soundfont` |
| MIDI 保真 | 读回丢速度表（取首个 tempo）、丢通道（非鼓一律 0）、不写 CC/音色库选择 | `models/midi.py:107-144`、`68-90` |
| 总谱 | 已支持多轨多系统与 MusicXML part-list，但无分谱组、无移调乐器、无钢琴大谱表、SVG 无打击谱号 | `score/layout.py:225-250`、`score/musicxml.py:231-245` |
| 混音 | 无分轨、无音量/声像/发送，渲染即成品 | `render/fluidsynth.py:65-133` |
| 工程 | `generate midi` 20+ 个 CLI 开关；输出为扁平 project-date 目录 | `cli.py:119-168`、`output_manager.py` |

结论：**"多轨"在数据层只有半成品，在编曲层是硬编码，在渲染层完全没有。** 这不是加一个开关能解决的，需要一次内核级重构。

### 1.2 用户诉求拆解

> "支持多通道的器乐通过代码方式合成总谱和完整一首由代码驱动的多乐器 mid 的音乐；后续可能需要引入广泛的音源库。"

拆成六个可验收能力：

- **G1 多通道**：≥8 条独立乐器轨，通道/音色/声像/音量可控，鼓轨独立。
- **G2 代码驱动**：一份声明式清单（或 Python DSL）完整描述一首曲子，零交互可重建。
- **G3 总谱**：多声部、可分谱组、可移调、可交付（SVG/PNG/MusicXML）。
- **G4 成品音频**：多音源渲染 + 分轨 + 混音，达到"可听 demo"质量。
- **G5 音源体系**：音源可注册、可校验、可替换，不与代码耦合。
- **G6 AI 友好**：清单是结构化契约，AI 可生成、可校验、可修复。

### 1.3 产品愿景：数字轴 × 模拟轴

```
数字轴（可控、可复现、可交付）
  代码 / AI ──► SongSpec ──► Song ──► MIDI ──► 音源渲染 ──► 混音 ──► 母带 ──► MV
                    ▲                                                        │
                    │                                                        │
              compose import（分析/转谱回流）                        export suno（片段投喂）
                    │                                                        │
模拟轴（生成式、不可控、需解析）                                            ▼
  Suno / 海绵 / 天音 ──► 下载/解密 ──► 转码 ──► 分析（调性/速度/和弦/结构/转谱）──► 采样/MIDI
```

SunoAuxTool 的差异化不在"再做一个 DAW"，而在**把两条轴接起来**：模拟轴产出素材，数字轴把它变成可编辑、可复现、可交付的作品；反之数字轴的成品可以切段投喂 Suno 做质感增强。MV 子系统（`sunoauxtool.video`）天然消费数字轴的混音与谱面。

---

## 2. 目标与非目标

### 2.1 目标

- **G1–G6**（见 1.2）。
- **G7 兼容**：1.x 全部 CLI 与公开 Python API 在 2.0 保持可用；`generate midi` / `pipeline` 行为不变。
- **G8 确定性**：同一清单 + 同一种子 → MIDI 字节级一致；渲染音频在固定引擎版本下一致。
- **G9 轻依赖**：2.0 不引入重型依赖（仅显式声明已传递依赖的 `mido`）；音源、AI、VST 全部可选。

### 2.2 非目标（明确不做）

| 编号 | 非目标 | 理由 |
|---|---|---|
| N1 | 实时 DAW / 钢琴卷帘 GUI | 定位是无头流水线，不是编辑器 |
| N2 | 记谱交互编辑 | 总谱是交付物，不是编辑对象 |
| N3 | 进程内 VST3 托管 | 宿主协议复杂、跨平台差；2.x 用外部渲染后端 |
| N4 | 内置/分发商业音源 | 许可风险；只做注册与校验 |
| N5 | 云端渲染/协作 | 超出单机工具定位 |
| N6 | 自动混音"专业母带"承诺 | 提供可用的增益/声像/发送/限幅，不承诺发行级母带 |

---

## 3. 用户场景

| 场景 | 输入 | 输出 | 关键能力 |
|---|---|---|---|
| S1 代码编曲 | `song.toml`（调性/速度/段落/和弦/轨道） | `song.mid` + `mix.wav` + `stems/` | G1/G2/G4 |
| S2 总谱交付 | 同一清单或现成 MIDI | `score.svg` / `.musicxml`（分谱组、移调） | G3 |
| S3 音源切换 | 同一 MIDI，换 bank | 不同音色成品，响度不跳变 | G5 |
| S4 AI 编曲 | 自然语言 → LLM → 清单 | 校验通过的清单 + 成品 | G6 |
| S5 模拟回流 | Suno 成品音频 | 调性/速度/和弦/结构报告 → MIDI 草案 | 分析链复用 |
| S6 专辑/MV | 多清单批量 | 批量成品 + 视频 | 批处理 + video |

---

## 4. 能力矩阵

| 能力 | 1.9.1 | 2.0.0 | 2.1.0 | 2.2.0 | 2.3.0 |
|---|---|---|---|---|---|
| 轨道数 | 3–4（固定角色） | 8–16（任意角色） | 同左 | 同左 | 同左 |
| 段落结构 | 无 | 前奏/主歌/副歌/间奏/尾奏 + 能量曲线 | 同左 | 同左 | 同左 |
| 速度/拍号变化 | 单一 | 速度表 + 拍号表 | 同左 | 渐变/弹性速度 | 同左 |
| 每轨音色 | 预设写死 | 指定 bank+program | 逻辑乐器名 + 预设映射 | 演奏法映射 | 同左 |
| 分轨/混音 | 无 | stems + 增益/声像/发送/限幅 | 响度匹配 | 插入效果 | 同左 |
| 总谱 | 多轨单系统 | 分谱组/移调/大谱表/分页 | 同左 | 演奏法/力度记号 | 同左 |
| 音源库 | 单 SF2 | 多 SF2 按轨选择 | 注册表 + 校验 + 许可 | SFZ（外部采样器） | 插件后端 |
| 演奏法/人性化 | 无 | 基础力度/微时序 | 同左 | 完整演奏法 + 摇摆 | 同左 |
| AI 驱动 | 无 | 清单契约 | 同左 | 同左 | LLM 编曲 + 修复 |
| 确定性 | 部分（种子） | MIDI 字节级 | 同左 | 同左 | 同左 |

---

## 5. 产品形态

### 5.1 CLI 面

```bash
# 新建/校验/构建/渲染
sunoaux compose init night            # 生成 night/song.toml 模板
sunoaux compose validate night/song.toml
sunoaux compose build  night/song.toml -o out/night --score svg,musicxml
sunoaux compose render night/song.toml -o out/night --stems --format wav

# 反向：音频/MIDI → 清单草案
sunoaux compose import song.wav -o draft.toml --analyze

# 音源库
sunoaux soundbank list
sunoaux soundbank info gm-generaluser
sunoaux soundbank verify --all
```

`compose build` 只产 MIDI/总谱；`compose render` 产音频（与现有 `render` 语义一致）。`--dry-run` 保持全链路可用（只打印计划，不写产物）。

### 5.2 清单示例（`song.toml`）

```toml
schema = 1

[song]
title = "夜航"
key = "D minor"
tempo = 96
meter = "4/4"
seed = 20261024
bars = 32

# 速度表（beat 为全曲累计拍，0 必须存在）
[[tempo]]
beat = 0
bpm = 96
[[tempo]]
beat = 64
bpm = 104

# 段落：能量 0..1 驱动配器密度
[[section]]
id = "intro"
name = "前奏"
bars = 4
energy = 0.20
progression = ["Dm", "Bb", "F", "C"]

[[section]]
id = "verse"
name = "主歌"
bars = 8
energy = 0.45
progression = ["Dm", "Bb", "F", "C"]

[[section]]
id = "chorus"
name = "副歌"
bars = 8
energy = 0.85
progression = ["Bb", "C", "Dm", "Dm"]

[[section]]
id = "outro"
name = "尾奏"
bars = 4
energy = 0.25
progression = ["Bb", "C", "Dm", "Dm"]

# 轨道：角色决定生成策略，instrument 决定音色
[[track]]
id = "piano"
name = "钢琴"
role = "harmony"
instrument = "piano"
bank = "gm-generaluser"
program = 0
sections = ["intro", "verse", "chorus", "outro"]
density = 0.6
mix = { gain_db = -6.0, pan = -0.15, reverb_send = 0.18 }

[[track]]
id = "cello"
name = "大提琴"
role = "bass"
instrument = "cello"
bank = "gm-generaluser"
program = 42
sections = ["verse", "chorus", "outro"]
octave = 2

[[track]]
id = "strings"
name = "弦乐铺底"
role = "pad"
instrument = "strings"
program = 48
sections = ["chorus", "outro"]
mix = { gain_db = -10.0, pan = 0.2, reverb_send = 0.35 }

[[track]]
id = "drums"
name = "鼓组"
role = "drums"
instrument = "standard_kit"
channel = 9                 # 鼓轨固定通道 9
sections = ["verse", "chorus"]
groove = "pop-half-time"
```

### 5.3 Python DSL（同一内核的薄封装）

```python
from sunoauxtool.compose import Song

song = (
    Song(title="夜航", key="D minor", tempo=96, seed=20261024)
    .section("intro", bars=4, energy=0.2, progression="Dm-Bb-F-C")
    .section("verse", bars=8, energy=0.45, progression="Dm-Bb-F-C")
    .section("chorus", bars=8, energy=0.85, progression="Bb-C-Dm-Dm")
    .track("piano", role="harmony", program=0, sections=["intro", "verse", "chorus"])
    .track("cello", role="bass", program=42, octave=2)
    .track("drums", role="drums", channel=9, groove="pop-half-time")
)

song.render("out/night", stems=True, score="svg,musicxml")
```

DSL 与清单**同构**：DSL 编译为 `SongSpec`，清单解析为 `SongSpec`，之后走同一条构建管线。

### 5.4 项目目录布局

```
out/night/
├── song.toml              # 输入清单（可版本管理）
├── arrangement.json       # 展开后的 Song 快照（审计/复现）
├── night.mid              # 成品 MIDI（format 1，含速度表/CC）
├── night.wav              # 混音成品
├── stems/                 # 分轨（piano.wav / cello.wav / ...）
├── score/
│   ├── night.svg          # 总谱（分页/长卷）
│   ├── night.png
│   └── night.musicxml     # 交付用
└── meta.json              # 版本/种子/音源/参数（现有 metadata 体系扩展）
```

### 5.5 AI 契约

- 提供 `schemas/song.schema.json`（JSON Schema 2020-12）+ 语义校验（和弦可解析、段落小节对齐、音域合理、通道不冲突）。
- `compose validate` 输出**带 JSON Pointer 的错误列表**，供 LLM 自修复循环使用。
- 提示词契约：系统提示 = 精简 schema + 2 个示例 + 规则（每轨必须给 role/sections；鼓轨 channel=9；progression 长度 = 每小节一个和弦或显式展开）。

---

## 6. 架构总览

```
L6  入口层    cli / aggregate / compose CLI / Python DSL
      │  只做参数解析与编排，不含乐理
L5  服务层    compose.arranger（编曲）· render.mixer（混音）· score_export（总谱）
      │
L4  引擎层    harmony / melody / rhythm / orchestration / humanize
      │
L3  领域层    compose.song（Song/Section/Track/Clip/NoteEvent/TempoMap）
      │
L2  交换层    midi.io（mido，SMF type 1）· score.model（Part/Staff）· arrangement.json
      │
L1  基础层    models.notes · music_theory · dsp · soundbank registry
      │
L0  外部      FluidSynth + SoundFont · ffmpeg · （未来）SFZ/VST 后端
```

**关键决策（ADR）**：

| 编号 | 决策 | 理由 | 代价 |
|---|---|---|---|
| ADR-1 | `Song` 是唯一真源，`NoteSequence` 降级为生成器输出适配层 | 现在四套模型（Note/C/ScoreNote/MidiTrack）互相转换、处处有损 | 需要适配器与一次性迁移 |
| ADR-2 | MIDI 写入改用 `mido`（显式声明依赖），`pretty_midi` 仅保留兼容门面 | `pretty_midi` 不写速度表/CC/通道，无法满足多轨 | 重写 IO 与 golden 测试 |
| ADR-3 | 分轨渲染 + 软件混音，而不是依赖 FluidSynth 内建混音 | FluidSynth 无每轨增益/声像/发送，无法交付 stems | 渲染耗时 ×N，需要缓存 |
| ADR-4 | 音源用 `SoundBank` 注册表 + 逻辑乐器名解析 | 换库不改谱，换音色不改代码 | 需要维护映射表 |
| ADR-5 | 种子按 `(song, section, track, purpose)` 派生 | 增删一轨不改变其他轨（可增量编辑） | 初期设计成本 |
| ADR-6 | 总谱用 Part（分谱）→ Staff（谱表）两级 | 钢琴大谱表、分谱组、移调都需要 Part 概念 | 布局与 MusicXML 需扩展 |

---

## 7. 里程碑与验收

### M1 · 2.0.0 多轨内核

**范围**：Song 模型、MIDI IO v2、Arranger v1、渲染分轨/混音、总谱 v2、`compose` CLI。

**验收（全部满足才发版）**：

- [ ] `sunoaux compose build night/song.toml -o out/night` 产出：≥8 轨 MIDI、包含 ≥2 个速度点、鼓轨在通道 9、每轨有音色库选择事件。
- [ ] `compose render --stems` 产出 8 个 stem + 1 个 mix；stem 求和与 mix 在母带前空测 ≤ −60 dBFS。
- [ ] 总谱 SVG/MusicXML：8 个 part、分谱组括号、至少 1 个移调乐器、钢琴大谱表；MusicXML 通过现有离线结构校验。
- [ ] 同清单同种子重复构建 3 次，MIDI 字节级一致；改动任一轨的 density 不影响其他轨音符。
- [ ] 旧命令 `generate midi` / `pipeline` / `render` 全部回归通过，输出与 1.9.1 一致（golden 对比）。
- [ ] 新增模块单测覆盖率 ≥90%，根套件覆盖率不低于 87% 门槛；CI 绿。

### M2 · 2.1.0 音源库

- [ ] `soundbank` 注册表（TOML）+ `list/info/verify` 子命令；校验文件头、sha256、许可字段。
- [ ] ≥2 套音源（GM 通用 + 一套管弦/钢琴）可按轨切换；同一 MIDI 换库不改变音符。
- [ ] 响度匹配：换库后整曲 LUFS 偏差 ≤1 LU（用现有 `dsp/loudness.py` 度量）。
- [ ] `doctor` 报告默认音源与可用库清单。

### M3 · 2.2.0 表现力

- [ ] 演奏法（staccato/tenuto/accent/legato/pizz/arco）写入 MIDI（时值/力度/键切换）并在总谱显示。
- [ ] 力度记号（p/mf/f）与渐强渐弱映射到 CC11/力度。
- [ ] 人性化：每轨微时序 ±10ms、力度 ±8（可关）；摇摆比例可配。
- [ ] 弹性速度：段落级 rit./accel. 曲线，MIDI 速度表可还原。

### M4 · 2.3.0 AI 编曲

- [ ] LLM 依据 schema 产出清单，`compose validate` 一次通过率 ≥80%（人工抽检 20 例）。
- [ ] 校验失败时错误可定位到 JSON Pointer，修复循环 ≤2 轮成功率 ≥90%。
- [ ] `compose import`：音频 → 调性/速度/和弦/结构 → 清单草案（复用 analysis 链），人工可在 10 分钟内改到可渲染。

### M4.5 · 2.5.0 出版级总谱 + Suno 接入（已排期 2026-10-29）

- [x] **F+ 大谱表**：钢琴/键盘类两行谱（treble+bass、右手/左手按 MIDI 60 分界）、
  SVG/PNG/MusicXML 三路同几何（2.6.0 交付；跨行连谱号/符干留后续）。
- [x] **分页**：`--systems-per-page N` 多页文件、首页标题、页脚页码（2.6.0 交付）。
- [x] **Suno 通道 A**：`post relay-music` 中转站生成（2.5.0 已交付，实测 60s 出片/双版本/≈0.48 元）。
- [x] **Suno 通道 B（P1）**：`post suno-web` Web 凭证只读（2.5.0 交付；端点待真实 Cookie 校准）。
- [ ] Suno 通道 B（P2，默认关闭）：Web 凭证生成；`compose import` 音频反推清单草案。

### M5 · 3.0.0 平台化

- [ ] 工程项目制（多曲目、共享音源/模板）、专辑批量渲染。
- [ ] 插件式音源后端（至少 SFZ 或外部 VST 渲染器其一）。
- [ ] 与 `sunoauxtool.video` 打通：一条命令从清单到 MV。
- [ ] 发布级文档与示例库（≥6 种风格完整清单）。

---

## 8. 发布与兼容策略

- **版本**：2.0.0 打 tag + GitHub Release（遵循 x.0 惯例）；2.1/2.2/2.3 不打 tag。
- **兼容承诺**：1.x 的 CLI 命令、参数、退出码、`NoteSequence`/`MidiDocument` 公开方法在 2.x 保持；`models/midi.py` 变为转发门面。
- **错误码**：新增编曲段 **40–49**（40 编曲清单非法 / 41 音源缺失或校验失败 / 42 渲染后端不可用 / 43 分轨或混音失败 / 44 清单 schema 校验失败 / 45 通道资源不足），现有 0–9、10–16、20–36 不动。
- **废弃策略**：`generate midi` 保留至少两个 minor 版本；文档标注"新项目请用 `compose`"。
- **迁移**：老项目无需迁移；`pipeline` 内部改用新引擎时保持输出等价（golden 对比）。

---

## 9. 质量指标

| 指标 | 目标 | 度量方式 |
|---|---|---|
| 确定性 | MIDI 字节级一致 | 同 spec+seed 构建 3 次，sha256 比对 |
| 分轨一致性 | 母带前空测 ≤ −60 dBFS | `mix - Σstems` 的峰值/RMS |
| 总谱合法性 | MusicXML 100% 通过 | 现有 `validate_musicxml` |
| 渲染性能 | 8 轨 × 3 分钟 ≤ 5 分钟（含 stems） | CI 基准（短 fixture 外推） |
| 覆盖率 | 新模块 ≥90%，根 ≥87% | pytest-cov |
| AI 清单通过率 | 首轮 ≥80% | 20 例抽样 |

---

## 10. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| FluidSynth 多库/音色库选择行为不一致 | 换库音色错乱 | 分轨渲染时每轨单库；集成测试用真实 SF2 覆盖 |
| 音源许可不清 | 法务风险 | 注册表强制 `license` 字段；不随包分发 |
| >16 通道 | 无法同时发声 | 明确上限：15 旋律 + 1 鼓；超限报 45 并建议分层/拆分 |
| 分轨渲染耗时 | 体验差 | 缓存（MIDI+库+参数哈希）、并行、`--stems` 可选 |
| 总谱复杂度失控 | 交付延期 | M1 只做分谱组/移调/大谱表/分页；演奏法留 M3 |
| AI 清单幻觉 | 渲染失败 | JSON Schema + 语义校验 + JSON Pointer 修复循环 |
| `mido` 与 `pretty_midi` 共存混乱 | 维护成本 | 新代码只走 `midi/`；旧 API 门面单向转发 |
| Windows 路径/编码 | 渲染失败 | 复用 `platform_paths` 与现有路径解析测试 |

---

## 11. 依赖与资产

- **新增依赖**：`mido`（SMF 读写；当前经 `pretty_midi` 传递安装，2.0 起在 `pyproject.toml` 显式声明并锁版本）。
- **不新增**：2.0 不引入 numpy 之外的重型库；SFZ/VST 后端在 2.2+ 以外部可执行文件方式接入（可选依赖，缺失时降级并提示）。
- **资产**：测试用短 SF2 fixture 走 Git LFS（注意 CI `lfs pull`）；示例清单入库，音源不入库。
- **许可**：注册表记录 `license`/`source`/`sha256`；文档声明"音源由用户自备"。

---

## 12. 开放问题（实施前需拍板）

1. **通道上限**：接受 15+1 的硬上限，还是支持"多文件/多端口"方案？（建议先硬上限，2.3 再评估）
2. **鼓谱呈现**：总谱里鼓轨用五线谱打击谱号，还是单独的打击垫网格图？（建议 M1 用打击谱号，网格图后置）
3. **音源分发**：只做注册表，还是提供"下载指引 + 校验"？（建议只做注册表 + 文档链接，规避分发）
4. **AI 编排署名**：AI 生成的清单是否写入 `meta.json` 的 `generator` 字段？（建议写入，便于审计）
5. **MusicXML 导入**：2.x 是否支持 MusicXML → Song？（建议 2.3 评估，先做 MIDI 导入）

---

## 附录 A · 术语表

| 术语 | 含义 |
|---|---|
| Song | 一首曲子的完整编曲对象（段落+轨道+时间表+总线） |
| Section | 段落（前奏/主歌/副歌…），带小节数与能量值 |
| Track | 一条乐器轨（角色+音色+通道+混音条） |
| Clip | 轨道上的一段音符（通常对应一个段落） |
| NoteEvent | 带演奏法/释放力度的音符事件（`Note` 的超集） |
| TempoMap / MeterMap / KeyMap | 全曲速度/拍号/调性变化表 |
| SoundBank | 音源库（SF2/SF3/SFZ…）+ 预设映射 + 许可元数据 |
| Stem | 单轨渲染出的音频分轨 |
| Mixdown | 分轨经增益/声像/发送/限幅后的立体声成品 |
| Part / Staff | 总谱的分谱（可含 1–2 个谱表）与谱表（大谱表=2 staff） |
