# 2.0 多轨内核 · 重构文档

> 基线：1.9.1 · 对应计划书 M1（2.0.0）· 日期：2026-10-24
> 配套：[plan-2.0-multitrack.md](plan-2.0-multitrack.md)

**总原则：绞杀者模式（Strangler）**——新内核（`compose/` + `midi/` + 新渲染层）并行生长，旧模型（`models/notes.py`、`models/midi.py`、`generators/procedural.py`、`render/fluidsynth.py`）**冻结不改造**，只加适配器。旧 CLI 输出保持不变，新能力走新入口。这样每个 PR 都可独立合并、独立回滚。

---

## 1. 现状审计

### 1.1 数据模型

| 位置 | 现状 | 问题 | 影响 |
|---|---|---|---|
| `models/notes.py:25-43` | `Note(pitch, start, duration, velocity)` | 无演奏法、释放力度、微时序 | 表现力上限锁死 |
| `models/notes.py:56-108` | `NoteSequence(bpm:int, key, time_signature, bars, style)` | 单一速度/拍号；`bars` 是必需项 | 无法表达变速、变拍、前奏弱起 |
| `models/notes.py:46-53` | `_Track(name, program, channel, notes)` | 无音量/声像/静音/独奏/音源库/音域 | 混音与音源选择无处存放 |
| `models/midi.py:37-44` | `MidiDocument(tracks, bpm:int, ticks_per_beat=480)` | `ticks_per_beat` **声明未使用**（pretty_midi 固定 resolution=220） | 名义精度与实际不符 |
| `models/midi.py:68-90` | `to_pretty_midi` 用全局 `beat_duration` 换算 | 只写一个初始 tempo；不写拍号/调号/CC/音色库选择；通道由 pretty_midi 顺序分配 | 多轨成品"听起来对、信息全丢" |
| `models/midi.py:107-144` | `load` 取 `_tempos[0]`，非鼓通道一律置 0 | 读回丢失速度表与真实通道 | MIDI 往返有损，二次编辑失真 |
| `score/model.py:103-121` | `ScoreTrack(name, program, channel, is_drum, clef, voices, notes)` | 一条轨=一个谱表；只有 treble/bass | 钢琴大谱表、分谱组无从表达 |
| `score/model.py:123-145` | `Score` 全局单一 `bpm/key/time_signature` | 无段落性变速/转调 | 总谱无法跟随编曲 |
| `score/model.py:193-233` | `from_midi` 走 `MidiDocument.load` | 继承上述有损 | 总谱从 MIDI 反推时信息缺失 |

### 1.2 编曲与渲染

| 位置 | 现状 | 问题 |
|---|---|---|
| `generators/procedural.py:29-52` | `_StylePreset` 写死 3 个 GM program + density | 风格=3 个数字，换音源/换风格必须改代码 |
| `generators/procedural.py:61-107` | `generate` 固定 chords/melody/bass[/drums] | 无段落、无前奏/副歌、无配器、无动机发展 |
| `pipeline.py:25,139` | 硬编码 `ProceduralGenerator` | 管线与单一生成器耦合 |
| `render/fluidsynth.py:30-37` | `Renderer.render(midi, soundfont, out)` | 接口只够"一个库渲染一首" |
| `render/fluidsynth.py:65-133` | 单 SF2、单进程、无分轨 | 无 stems、无混音、无每轨音色 |
| `config/default.toml [paths]` | `soundfont` / `soundfont_backup` 两个固定路径 | 无音源注册表、无逻辑乐器名 |

### 1.3 被低估的存量资产（重构是"接线"，不是重写）

- **谱面已支持多轨多系统**：`layout.py:225-250` 的 `System` 有 `staff_count`，MusicXML 已按轨写 `part-list`（`musicxml.py:231-245`），打击轨已用 percussion 谱号（`musicxml.py:313-315`）。
- **乐理工具齐全**：`music_theory/{voice_leading,inversion,counterpoint,rhythm_patterns}.py` 可直接供编曲引擎调用。
- **DSP 齐全**：`dsp/{loudness,reverb,filters,ops}.py` 已能承担混音总线的响度、混响与滤波。
- **MIDI 读写底座可用**：`mido` 已随 `pretty_midi` 安装（实测可 import），无需新增下载。

结论：M1 的工作量集中在**模型与 IO**，编曲引擎可复用现有乐理，谱面只需扩展 Part/移调/布局。

---

## 2. 目标架构

```
src/sunoauxtool/
├── compose/                    # 新增：编曲域（纯领域，不依赖 CLI/render）
│   ├── song.py                 # Song/Section/Track/Clip/MixStrip（NoteEvent/时间表复用 midi.document）
│   ├── spec.py                 # SongSpec：TOML 解析 + 语义校验（错误定位到 JSON Pointer）
│   ├── arranger.py             # Spec + seed → Song（编排主流程）
│   ├── harmony.py              # 和弦进行/声部配置（复用 music_theory）
│   ├── melody.py               # 动机生成与发展
│   ├── rhythm.py               # 鼓组/律动/摇摆
│   ├── orchestration.py        # 角色→乐器、音域/双奏/避让规则
│   ├── humanize.py             # 微时序/力度/发音
│   └── api.py                  # Python DSL（SongBuilder，编译为 SongSpec）
├── midi/                       # 新增：全保真 MIDI IO（mido）
│   ├── document.py             # MidiDocumentV2 / MidiTrackV2 / CCEvent / TempoMap...
│   ├── io.py                   # SMF type 1 读写（PPQ 960）
│   └── channel.py              # 通道分配策略
├── render/
│   ├── backend.py              # RenderBackend 协议 + RenderCapabilities
│   ├── fluidsynth.py           # 保留旧类；新增 FluidSynthBackend
│   ├── mixer.py                # 分轨混音（增益/声像/发送/限幅，分块处理）
│   └── registry.py             # SoundBank 注册表
├── score/                      # 扩展：Part/Staff/移调/分谱组
│   ├── model.py                # +ScorePart/ScoreStaff/transposition/tempo_map
│   ├── layout.py               # 系统分谱组/括号/连谱号/分页/多小节休止
│   ├── musicxml.py             # 多 part、<staves>、<transpose>
│   └── articulation.py         # 记号与 MIDI 映射（M3）
├── soundbank/                  # 新增：音源库（2.1，M1 先内置最小注册表）
│   ├── model.py                # SoundBank/Preset/校验
│   └── registry.py             # 搜索路径/加载/解析
└── models/ · generators/ · pipeline.py   # 冻结（仅加适配器，不改行为）
```

**依赖规则**（CI 加 import 守卫）：

```
cli/aggregate ──► compose.api / cli 服务 ──► arranger ──► song
                     │                          │
                     ├──► midi.io ◄─────────────┘
                     ├──► render.backend ──► registry
                     └──► score（只读适配）
compose 不得 import cli/render；render 不得 import compose；score 不得 import cli
```

---

## 3. 数据模型规格

### 3.1 时间与坐标

- 领域层一律用**拍（beat, float）**，与现有 `Note.start/duration` 约定一致。
- MIDI 层 `PPQ = 960`（现有 480 的 2 倍，兼容三连音/32 分音符）：`tick = round(beat * 960)`。
- **禁止增量累加取整**：每个事件独立 `round(start*PPQ)`，避免长曲漂移。
- 秒换算只能通过速度表积分（见 3.2），不得再用全局 `60/bpm`。

### 3.2 时间表

```python
@dataclass(frozen=True)
class TempoPoint:
    beat: float
    bpm: float                  # 允许小数（96.5）
    curve: str = "step"         # step | linear（linear 仅用于渐变，M3）

@dataclass(frozen=True)
class MeterPoint:
    beat: float
    numerator: int
    denominator: int

@dataclass(frozen=True)
class KeyPoint:
    beat: float
    key: str                    # "D minor"
```

要求与算法：

- `tempo_map[0].beat == meter_map[0].beat == key_map[0].beat == 0`；beat 严格递增；`bpm > 0`。
- `beat_to_seconds(beat)`：逐段累加 `Δt = (b - prev_beat) * 60 / prev_bpm`；落在 `curve="linear"` 段时按梯形积分（M3 启用）。
- 小节边界：从 0 开始按 `4 * numerator / denominator` 拍累加；段落 `start` 必须落在小节边界上（校验）。
- 弱起（pickup）：`Song.pickup_beats > 0` 时，第 1 小节不足整小节，后续小节从 `pickup_beats` 起算。

### 3.3 编曲对象

```python
@dataclass
class NoteEvent:
    pitch: int
    start: float                 # 拍（相对全曲）
    duration: float
    velocity: int = 64
    release_velocity: int = 64
    articulation: str | None = None   # staccato/tenuto/accent/legato/...（M3）
    offset_ticks: int = 0             # 人性化微时序（写入 MIDI 时叠加）

@dataclass
class Clip:
    start: float                 # 相对全曲的起始拍
    length: float                # 段落长度（拍）
    notes: list[NoteEvent]
    section_id: str = ""
    label: str = ""

@dataclass
class MixStrip:
    gain_db: float = 0.0
    pan: float = 0.0             # -1.0 左 .. +1.0 右
    mute: bool = False
    solo: bool = False
    reverb_send: float = 0.0     # 0..1
    chorus_send: float = 0.0

@dataclass
class InstrumentRef:
    bank: str                    # 注册表 id
    preset: str | None = None    # 逻辑乐器名（piano/cello/...）
    program: int | None = None   # 显式 GM program（优先于 preset）
    drum_kit: str | None = None

@dataclass
class Track:
    id: str
    name: str
    role: str                    # melody/harmony/bass/pad/counter/drums/percussion
    instrument: InstrumentRef
    channel: int | None = None   # None=自动分配；鼓轨=9
    clips: list[Clip] = field(default_factory=list)
    mix: MixStrip = field(default_factory=MixStrip)
    octave: int = 4
    range: tuple[int, int] | None = None
    muted: bool = False

@dataclass
class Section:
    id: str
    name: str
    start: float                 # 拍
    bars: int
    energy: float = 0.5          # 0..1，驱动配器密度
    progression: list[str] = field(default_factory=list)
    label: str = ""

@dataclass
class Song:
    title: str
    tempo_map: list[TempoPoint]
    meter_map: list[MeterPoint]
    key_map: list[KeyPoint]
    sections: list[Section]
    tracks: list[Track]
    master: MixStrip = field(default_factory=MixStrip)
    seed: int = 0
    schema_version: int = 1
```

**不变量（`Song.validate()` 强制，失败抛错误码 40）**：

1. 时间表首点 beat=0、严格递增；`bpm ∈ (0, 400]`。
2. 段落首尾相接：`sections[0].start == pickup_beats`，`sections[i].start + bars*bar_len == sections[i+1].start`。
3. 段落 `start` 落在小节线；`bars >= 1`。
4. 轨道 id 唯一；非鼓轨道通道互不重复；鼓轨通道必须为 9；全曲最多 15 条旋律轨 + 1 条鼓轨（超限 → 45）。
5. `Clip.start/length` 与所属段落一致；音符 `start >= 0`、`duration > 0`、`pitch ∈ [0,127]`、`velocity ∈ [1,127]`。
6. 每条轨的 `instrument.bank` 能在注册表解析（构建期校验；渲染前再校验一次）。

### 3.4 适配器

| 方向 | 函数 | 说明 |
|---|---|---|
| `NoteSequence` → `Song` | `compose.adapters.from_sequence(seq)` | 旧生成器输出可进新渲染/总谱；单速度转单点 tempo_map |
| `Song` → `MidiDocumentV2` | `midi.document.from_song(song)` | 新写盘路径 |
| `MidiDocument`(旧) → `Song` | `compose.adapters.from_legacy_midi(doc)` | 读旧 MIDI 进新引擎（单速度、通道 0/9） |
| `Song` → `Score` | `score.adapters.from_song(song)` | 总谱导出（M1） |
| `Score` → `Song` | 暂不提供 | MusicXML 导入留 2.3 评估 |

---

## 4. MIDI IO v2（`midi/`）

### 4.1 文件结构

- **SMF format 1**：track 0 = 指挥轨（tempo/meter/key/marker/track name），track 1..N = 乐器轨。
- 每条乐器轨：`track_name` → `bank select (CC0/CC32)` → `program_change` → 初始 `CC7/CC10/CC11` → 音符/CC 事件 → `end_of_track`。
- 鼓轨：channel 9，bank MSB=128（FluidSynth 鼓库约定），program 按鼓组。

### 4.2 事件映射

| 语义 | MIDI 事件 | 备注 |
|---|---|---|
| 速度 | meta `set_tempo` | `µs/qn = round(60_000_000 / bpm)` |
| 拍号 | meta `time_signature` | `dd = log2(denominator)` |
| 调号 | meta `key_signature` | `sf` 升降号数、`mi` 大小调 |
| 段落标记 | meta `marker` | 段落名（总谱排练记号同源） |
| 轨名 | meta `track_name` | `Track.name` |
| 音色库 | CC0 + CC32 | 先 MSB 后 LSB，紧接 program_change |
| 音色 | `program_change` | 0–127 |
| 音量 | CC7 | `gain_db → 0..127`（线性 dB 映射，见 6.4） |
| 声像 | CC10 | `pan → 0..127`，中心 64 |
| 表情 | CC11 | 力度/渐强渐弱（M3） |
| 延音 | CC64 | 演奏法 legato 辅助（M3） |
| 混响发送 | CC91 | `reverb_send` 映射 |
| 合唱发送 | CC93 | `chorus_send` 映射 |
| 弯音 | `pitchwheel` | 预留（M3 揉弦/滑音） |

### 4.3 通道分配

```
显式 channel 优先（校验冲突）
鼓轨 → 9（全曲仅一条鼓轨；多条鼓轨在 M1 合并为一条）
其余按 0..15 顺序取未占用通道（跳过 9）
无可用通道 → CompositionChannelError(45)，提示合并分层或拆分文件
```

### 4.4 读回

- 逐轨解析为 `MidiTrackV2`：`note_on/note_off` 配对（同 pitch 重叠用栈）、CC 归入 `control_changes`、bank/program 归入 `instrument`。
- tempo/meter/key 从指挥轨收集为时间表；缺失时补默认（120 / 4/4 / C major）。
- **未知事件保留**：sysex、未识别 meta 存入 `track.raw_events`，写回时原样重发（尽力无损）。
- 旧 `MidiDocument.load` 行为不变（继续走 pretty_midi）；新读入口 `midi.io.read_smf()`。

### 4.5 依赖

`mido` 在 `pyproject.toml` 显式声明（当前经 `pretty_midi` 传递存在，CI 干净环境同样可用，但按项目铁律必须显式登记）。

### 4.6 Golden 测试

- `tests/midi/golden/` 存放"由新写入器生成、人工审核过"的 `.mid`；测试断言**字节一致**（mido 写入确定性，无时间戳）。
- 另设结构断言：事件序列（tick/type/通道/值）逐条比对，便于定位差异。
- 往返：`Song → write → read → 比较`，覆盖速度表 3 点、拍号变化、CC、鼓轨、15 轨边界。

---

## 5. 编曲引擎（`compose/`）

### 5.1 流程

```
SongSpec ──► normalize（时间线/小节线/段落） ──► plan（Section × Role 矩阵）
        ──► harmony（每段和弦事件）
        ──► generate（按角色生成 Clip：melody/harmony/bass/pad/counter/drums）
        ──► orchestrate（角色→乐器、音域/避让/双奏）
        ──► humanize（微时序/力度）
        ──► Song.validate() ──► arrangement.json
```

### 5.2 段落与能量

| energy | 配器密度 | 典型手法 |
|---|---|---|
| 0.0–0.3 | 1–2 轨 | 单乐器动机、长音铺底、无鼓 |
| 0.3–0.6 | 3–5 轨 | 加入贝斯/轻鼓、和弦块状进行 |
| 0.6–0.85 | 6–8 轨 | 完整节奏组 + 旋律 + 对位 |
| 0.85–1.0 | 8–12 轨 | 加花、双奏、密集鼓、高频装饰 |

段落边界规则：前奏不出现主旋律；副歌首拍允许 crash/镲；尾奏逐轨退出（按 energy 反向）。

### 5.3 和声

- `progression` 每项 = 一小节的和弦（`"Dm"`、`"Bb"`、`"F/A"`）；长度不足时循环，超出时截断并告警。
- 声部配置：三/四声部闭合排列，声部间距上限（SATB 规则），用 `music_theory/voice_leading.py` 做相邻和弦平滑；`inversion.py` 控制低音走向。
- 输出为 `Clip`（每小节一个和弦事件，按 density 拆成块状/分解/琶音）。

### 5.4 旋律

- 动机：2 小节音高/节奏细胞，取自和弦音+音阶音（复用 `generators/base.py` 的 `scale_pitches_in_range`）。
- 发展算子：重复、模进（±2 度）、倒影、逆行、节奏扩/缩；每个 4/8 小节乐句选 1–2 个算子。
- 乐句结构：4 小节问句 + 4 小节答句；终止式落在和弦根音/三音；副歌音区上移 3–5 度。
- 与 `music_theory/counterpoint.py` 联动生成对位轨（role=counter）。

### 5.5 节奏与鼓

- `groove` 名 → 16 分网格图案（kick/snare/hat 命中位 + 力度层）；内置 pop/rock/electronic/half-time/waltz/funk。
- 段落末小节自动 fill（军鼓 16 分或通鼓下行）；段落首拍 crash。
- 摇摆：`swing=0..0.66` 把后半拍延后（M3 写入微时序）。

### 5.6 配器规则

| 规则 | 内容 |
|---|---|
| 音域 | 每个逻辑乐器有 `(low, high)`；超界自动八度折返，仍超界则告警 |
| 避让 | 同频段轨道最小间隔（如 bass 与 cello 相差 ≥12 半音），冲突时低轨下移八度 |
| 双奏 | energy>0.8 时旋律可被弦乐/木管同度加倍（写入独立轨） |
| 角色映射 | `role` 决定生成策略，`instrument` 只决定音色；两者解耦 |

### 5.7 确定性（关键）

```python
def seed_for(song_seed: int, section_id: str, track_id: str, stage: str) -> int:
    material = f"{song_seed}:{section_id}:{track_id}:{stage}".encode()
    return int.from_bytes(hashlib.blake2b(material, digest_size=8).digest(), "big")
```

每个 `(section, track, stage)` 独立 `random.Random(seed)`；**禁止全局 random**。这样：

- 同 spec+seed → 完全一致；
- 修改 A 轨的 density 不影响 B 轨；
- 增删轨道不改变已有轨道内容（可增量编辑）。

### 5.8 与旧生成器关系

`ProceduralGenerator` 保持原样（旧 `generate midi`/`pipeline` 走它）。新引擎只在 `compose` 下使用；M1 不合并两条路径，避免行为漂移。

---

## 6. 渲染与混音（`render/`）

### 6.1 后端协议

```python
class RenderCapabilities(NamedTuple):
    banks: int                  # 可同时加载的库数
    stems: bool
    cc_automation: bool
    sample_rates: tuple[int, ...]

class RenderBackend(Protocol):
    name: str
    def capabilities(self) -> RenderCapabilities: ...
    def render(self, midi_path: Path, banks: list[SoundBank], out_path: Path,
               *, sample_rate: int = 44100, gain: float = 0.6,
               dry_run: bool = False) -> Path: ...
```

`FluidSynthBackend` 实现该协议；旧 `FluidSynthRenderer` 保留并委托（行为不变）。

### 6.2 多音源策略

FluidSynth 的多库/鼓库行为在不同版本间有差异，采用**保守策略**：

- **单库**（所有轨同 bank）：一次渲染整首，速度最快。
- **多库**：自动切换为"分轨渲染 + 混音"——每轨单独 MIDI（指挥轨 + 该轨），用该轨的 bank 渲染，天然规避多库冲突。
- 每轨渲染时关闭内建混响（`-o synth.reverb.active=0`），混响统一在混音阶段做，保证可复现。

### 6.3 分轨流程

```
Song ──► 每轨 MidiDocumentV2 ──► 缓存查找(sha256) ──► fluidsynth 渲染 ──► stem.wav
                                                              │
所有 stems ──► Mixer（分块）──► mix.wav ──► loudnorm 检查 ──► meta.json
```

缓存键：`sha256(midi_bytes + bank_sha256 + sample_rate + gain + backend_version)`；缓存目录 `~/.cache/sunoauxtool/render/`（可用 `SUNO_RENDER_CACHE` 覆盖）。

### 6.4 混音规格

| 环节 | 规则 |
|---|---|
| 增益 | `linear = 10 ** (gain_db / 20)`；CC7 映射用同一曲线（保证 MIDI 与混音一致） |
| 声像 | 等功率：`L = cos((pan+1)·π/4)`，`R = sin((pan+1)·π/4)`；pan ∈ [-1,1] |
| 静音/独奏 | solo 优先：任一 solo 时仅渲染 solo 轨 |
| 发送 | 每轨 `reverb_send` 汇入湿总线，湿总线单独增益后叠加 |
| 总线 | master gain → 软限幅（峰值 ≤ -0.3 dBFS）→ 16bit PCM |
| 处理方式 | **分块**（默认 5 秒块，float32），内存上限约 50MB，避免 8 轨全载 |

**空测验收**：`mix_pre_master - Σ(stem × gain × pan)` 的峰值 ≤ −60 dBFS（母带限幅前）。

### 6.5 性能预算

| 场景 | 预估 | 说明 |
|---|---|---|
| 8 轨 × 180s，单库整曲 | 30–60s | FluidSynth 约 5–10× 实时 |
| 8 轨 × 180s，stems | 4–8 min（串行） | 可 `--jobs 4` 并行到 2–3 min |
| 缓存命中 | < 5s | 仅混音 |

`--stems` 默认关闭；`compose render` 默认只出 mix。

### 6.6 失败语义

- 任一 stem 渲染失败 → 整体失败（错误码 43），不产出"缺轨"的成品（避免静默错误）。
- 可选 `--allow-partial` 时降级：缺失轨写入 `meta.json.warnings`，成品仍产出。

---

## 7. 音源库（`soundbank/`）

### 7.1 模型

```python
@dataclass(frozen=True)
class Preset:
    name: str
    program: int
    bank_msb: int = 0
    bank_lsb: int = 0
    drum: bool = False

@dataclass(frozen=True)
class SoundBank:
    id: str
    path: Path
    format: str                  # sf2 | sf3 | sfz（2.2）
    gm_compatible: bool
    license: str
    sha256: str | None = None
    loudness_offset_lu: float = 0.0
    presets: dict[str, Preset] = field(default_factory=dict)
```

### 7.2 注册表文件（`soundbanks.toml`）

```toml
schema = 1

[bank.gm-generaluser]
path = "module/GeneralUser_GS/GeneralUser-GS/GeneralUser-GS.sf2"
format = "sf2"
gm_compatible = true
license = "GeneralUser GS License (free to use)"
loudness_offset_lu = 0.0

[bank.gm-generaluser.presets]
piano = { program = 0 }
cello = { program = 42 }
strings = { program = 48 }
standard_kit = { program = 0, drum = true }
```

搜索顺序：`--soundbank-file` > 项目根 `soundbanks.toml` > 用户配置目录 > 内置 GM 逻辑名表（仅 program，不含路径）。

### 7.3 解析规则

```
track.instrument.program 显式给出 → 直接使用（bank 取 instrument.bank 或默认库）
否则 instrument.preset → bank.presets[preset] → (bank_msb, bank_lsb, program)
preset 不存在 → CompositionSoundBankError(41)，错误信息列出可用 preset
bank 不存在/文件缺失/校验失败 → 41（与现有 ModuleError(7) 区分：7 是"默认环境缺失"，41 是"清单指定不可用"）
```

### 7.4 校验与许可

- 文件头：`RIFF....sfbk`（SF2/SF3）；大小 > 0；可选 sha256 比对。
- `license` 必填；缺失 → 41（防止来源不明音源进入流水线）。
- `verify --all` 输出表：id/格式/大小/许可/预设数/校验结果。

### 7.5 响度匹配（2.1）

- 换库后整曲 LUFS 偏差 ≤1 LU：用 `dsp/loudness.py` 测混音前后，按 `loudness_offset_lu` 补偿。
- 预设级偏移可选手工标注；未标注时按整库偏移。

---

## 8. 总谱 v2（`score/`）

### 8.1 模型扩展

```python
@dataclass
class ScoreStaff:
    clef: str                    # treble/bass/alto/tenor/percussion
    notes: list[ScoreNote]
    voices: int = 1

@dataclass
class ScorePart:
    id: str
    name: str
    abbreviation: str
    instrument: InstrumentRef
    staves: list[ScoreStaff]     # 钢琴=2，其他=1
    group: str | None = None     # woodwinds/brass/strings/percussion/rhythm
    transposition: int = 0       # 写法音高 = 实音 + transposition

@dataclass
class Score:
    ...                          # 保留现有字段
    parts: list[ScorePart]
    tempo_map: list[TempoPoint]
    meter_map: list[MeterPoint]
    key_map: list[KeyPoint]
    pickup_beats: float = 0.0
```

兼容：`Score.tracks` 保留为只读视图（`parts → staves` 展平），旧导出代码不改。

### 8.2 移调乐器

| 乐器 | transposition（半音） | 说明 |
|---|---|---|
| Bb 单簧管/小号 | +2 | 写法比实音高大二度 |
| Eb 中音萨克斯 | +9 | 写法比实音高小六度 |
| F 圆号 | +7 | 写法比实音高纯五度 |
| 低音提琴 | −12 | 写法比实音高八度（记谱惯例） |

导出：MIDI 用实音；MusicXML 用 `<transpose><chromatic>`；SVG 用写法音高 + 移调后的调号。

### 8.3 布局改造（`layout.py`）

- `System` 从"轨列表"改为"part 列表 + 每 part 的 staff 列表"；`staff_count = Σlen(part.staves)`。
- 新增：分谱组括号（花括号/方括号）、part 名与缩写、系统左侧标签列、多小节休止（空小节 >2 压缩为 `n 小节`）、按页面高度分页。
- 谱号：`_staff_reference` 增加 alto(24)/tenor(20)/percussion（以 treble 几何 + 打击谱号字形）。
- 保持现有几何铁律：行中心对称、宽音域收缩半线距（见 2026-09-20 日志），不得回归。

### 8.4 MusicXML

- `<part-list>` 按 `ScorePart` 写（不是按轨）；`<part>` 内 `<staves>2</staves>` + `<clef number="1|2">`。
- `<transpose><chromatic>` 写移调；`<attributes>` 在小节边界写拍号/调号变化。
- 现有离线校验器扩展：允许 `staves>1` 时 `<note><staff>` 出现。

### 8.5 简谱边界

简谱只支持**单声部旋律轨**；复调/鼓轨跳过并告警（不静默产出错误谱）。总谱交付以 SVG/MusicXML 为准。

---

## 9. CLI / API 变更

### 9.1 命令

| 旧 | 新 | 状态 |
|---|---|---|
| `generate midi` | `compose build` | 并存；旧命令冻结 |
| `render` | `compose render` / `render --stems` | `render` 只加可选参数，默认行为不变 |
| `score` | `compose build --score` / `score --full` | `score` 增加 `--full`（分谱组/移调） |
| — | `compose init/validate/import` | 新增 |
| — | `soundbank list/info/verify` | 新增（2.1） |

### 9.2 清单契约

- 运行时校验用**手写语义校验器**（不引入 `jsonschema` 重依赖）；`schemas/song.schema.json` 作为 AI/编辑器契约随包发布。
- 错误输出格式：

```json
{"path": "/section/2/bars", "code": 40, "message": "bars 必须 >= 1"}
```

- TOML 解析用标准库 `tomllib`；写回用已依赖的 `tomli-w`。

### 9.3 Python API

`compose.api.SongBuilder` 只做"收集 → 编译为 SongSpec"，不复制生成逻辑；`Song.render()` 是 `compose` 服务层的便捷封装。

---

## 10. 迁移计划（PR 序列）

每个 PR 独立可合并、可回滚；新能力只在新入口暴露。

| PR | 内容 | 主要文件 | 测试 | 回滚 |
|---|---|---|---|---|
| A | `midi/` 新包（模型+读写+通道分配） | `midi/{document,io,channel}.py` | `tests/midi/` golden+往返 | 删除新包，无引用 |
| B | `Song` 模型 + 适配器 + 校验 | `compose/{song,adapters}.py` | `tests/compose/test_song.py` | 无引用 |
| C | `compose` CLI MVP（显式音符清单 → MIDI/总谱） | `compose/{spec,api}.py`、`cli.py` | `tests/compose/test_spec_cli.py` | 移除命令注册 |
| D | Arranger v1（段落/角色/确定性） | `compose/{arranger,harmony,melody,rhythm,orchestration,humanize}.py` | `tests/compose/test_arranger*.py` | 命令加 `--engine legacy` |
| E | 渲染 v2（后端协议/多库/stems/mixer） | `render/{backend,mixer}.py`、`render/fluidsynth.py` | `tests/pre/test_render_v2.py` | 旧类不动 |
| F | 总谱 v2（Part/移调/布局） | `score/{model,layout,musicxml}.py` | `tests/score/test_full_score.py` | `--full` 关闭即旧行为 |
| G | SoundBank 注册表 + doctor | `soundbank/*`、`config/default.toml` | `tests/core/test_soundbank.py` | 删除注册表 |
| H | 收口：文档/CHANGELOG/版本 2.0.0 | `docs/*`、`pyproject.toml` 等 4 文件 | 版本对齐守卫 | 回退版本 |

**每个 PR 的完成定义（DoD）**：ruff 干净；新增测试全绿；根套件覆盖率不低于门槛；`--dry-run` 可用；文档同步（至少 docstring + 对应 docs）。

---

## 11. 兼容矩阵

| 对象 | 2.0 状态 | 说明 |
|---|---|---|
| `Note` / `NoteSequence` | 不变 | 冻结；新代码用 `NoteEvent`/`Song` |
| `MidiDocument.load/write` | 不变 | 继续走 pretty_midi；有损行为保留，文档标注 |
| `FluidSynthRenderer.render` | 不变 | 新增 `FluidSynthBackend` 并行 |
| `ProceduralGenerator` | 不变 | `generate midi`/`pipeline` 继续使用 |
| `score` 命令 | 默认不变 | `--full` 才启用新布局 |
| 错误码 | 0–36 不变 | 新增 40–45 |
| 配置文件 | 兼容 | 新增 `[soundbanks]` 段，旧键继续有效 |

---

## 12. 测试策略

| 层 | 手段 | 重点 |
|---|---|---|
| 模型 | 单测 + 不变量 | 时间表、段落对齐、通道唯一、音符边界 |
| MIDI IO | golden 字节 + 事件序列 | 速度表/拍号/CC/鼓轨/15 轨边界 |
| 编曲 | 种子确定性 + 结构断言 | 同种子一致；增删轨不影响他轨；音域不越界 |
| 渲染 | 时长/静音/通道可闻/空测 | stems 求和 = mix（母带前） |
| 总谱 | MusicXML 校验 + 布局不变量 | part 数、staves、transpose、无重叠、系统宽度 |
| CLI | `--dry-run` + 错误码 | 缺库/超通道/非法清单的可读报错 |
| 性能 | 基准测试（pytest marker） | 8 轨 180s 渲染预算 |

CI 注意：音频渲染用 2 小节短 fixture；SF2 走 LFS，checkout 后 `git lfs pull`；Ubuntu fluidsynth 用 `file` 驱动。

---

## 13. 性能与资源

- 混音分块 5s，峰值内存 ≈ 块大小 × 轨数 × 2 声道 × 4B ≈ 2MB；不随曲长增长。
- stems 并行度默认 `min(4, CPU 核数)`；渲染缓存按哈希复用。
- 长曲（>10 分钟）仍按 tick 独立取整，避免累计漂移。
- 总谱布局复杂度 O(小节数 × 谱表数)；30 小节 × 12 谱表在 1s 内可出 SVG（现有实测基线）。

---

## 14. 技术风险

| 风险 | 触发条件 | 对策 |
|---|---|---|
| FluidSynth 鼓库/多库行为差异 | 换库后鼓音色错 | 鼓轨单库渲染；集成测试覆盖真实 SF2 |
| mido 写入非确定性 | 未来版本变更 | 锁版本 + golden 字节测试第一时间发现 |
| 15 轨上限不够 | 大型管弦 | 2.3 评估多文件/多端口；M1 明确报 45 |
| 总谱布局回归 | 大谱表/分谱组改动 | 保留现有几何铁律与像素/结构断言 |
| stems 渲染慢 | 长曲多轨 | 缓存 + 并行 + `--stems` 可选 |
| 音源许可 | 用户自带库 | 注册表强制 license；文档免责 |
| AI 清单非法 | LLM 幻觉 | 语义校验 + JSON Pointer + 修复循环 |

---

## 15. M1 验收清单

> **进度**（按 PR 逐项落地，`[~]` = 部分达成，括号内为剩余工作）
> - PR-A `midi/`（1351f51）：SMF 全保真读写 + 通道分配；68 例。
> - PR-B `compose/song.py`（68ff64f）：`Song` 编曲模型 + 旧模型适配器；71 例。
> - PR-C `compose/{spec,api,cli}.py`（0f9c851）：清单/DSL/`compose init|validate|build`；121 例。
>   顺带修正弱起网格锚点（`_is_bar_boundary` 以 `pickup_beats` 为锚）。
> - PR-D `compose/{arranger,harmony,melody,rhythm,orchestration,humanize,rng}.py`（73c8b4d）：
>   能量→角色矩阵、动机发展与终止式、7 套 groove、blake2b 种子隔离、人性化；243 例。
> - PR-E `render/{backend,mixer,exceptions}.py` + `compose/render.py`（3f49ae6）：
>   后端协议、流式分轨混音（balance 声像）、每轨独立渲染 → stems + mix；45 例。
> - PR-F `score/model.py`（transpose/part_group/from_song/parts）、`score/musicxml.py`
>   （`<transpose>`/`<part-group>`/记谱音高）、`compose/score.py` + `compose score`（5141e51）：
>   移调全链透传、分谱组括号、逐声部分谱；25 + 21 例。
> - PR-G `soundbank/{registry,cli,exceptions}.py` + `doctor` 音源库行（本阶段）：
>   扫描登记、结构校验、`soundbank list/info/verify`、错误码 41；43 例。

- [x] `midi/` 包：SMF type 1 往返测试全绿（含 3 点速度表、拍号变化、CC、鼓轨）。
- [x] `Song.validate()` 覆盖 6 类不变量，错误码 40 可读。
- [x] `compose build`：TOML → 8 轨 MIDI + `arrangement.json`，`--dry-run` 无产物
      （PR-D 起 `compose init` 模板即 8 轨 6 段，Arranger 生成 885 音符 / 90 秒）。
- [x] `compose render --stems`：8 stems + mix；空测 `mix − Σstems` 实测 −155 dBFS（要求 ≤ −60 dBFS）。
- [x] 确定性：同 seed 构建 sha256 一致；改单轨 density 不影响他轨；增删轨道不改变他轨
      （PR-D 起由 `(seed, section, track, role)` 派生独立 RNG）。
- [~] `score --full`：分谱组/移调/分谱已交付（`compose score --parts`，MusicXML 校验通过）；
      大谱表（钢琴两行）与分页留待 F+。
- [x] 旧命令回归：`generate midi`/`pipeline`/`render` 输出与 1.9.1 一致（根套件全绿）。
- [x] 错误码 40–45 全部注册：40/44（清单）、41（音源）、42/43（渲染）、45（通道）。
- [~] 文档：`docs/compose.md` + `docs/structure.md` + README 已更新；CHANGELOG 2.0.0 待 PR-H。
- [x] CI 绿；新增模块覆盖率 ≥90%（compose 99.5% / midi 96% / render 93%），根套件 91.01%。
