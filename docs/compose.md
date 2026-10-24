# compose：代码驱动的多轨编曲（2.0）

> 状态：**2.1.0 已交付**（2.0 全部能力 + 每轨独立选音源库 + 跨库响度对齐）。
> 命令面：`compose init / validate / build / render / score` + `soundbank list / info / verify`。

`compose` 是"数字轴"入口：一份 `song.toml`（或 Python DSL）完整描述一首曲子，
构建出**全保真 MIDI**（format 1，含速度表/拍号/调性/CC/音色库）与**编排快照**，
零交互、字节级可复现。

## 1. 五个命令

```bash
sunoaux compose init night                 # 生成 night/song.toml（8 轨 6 段模板，可直接构建）
sunoaux compose validate night/song.toml   # 校验；--json 给机器读
sunoaux compose build  night/song.toml     # -> out/night/night.mid + arrangement.json
sunoaux compose render night/song.toml     # -> out/night/night.wav + stems/ + midi/
sunoaux compose score  night/song.toml --format svg,musicxml --parts
                                          # -> out/night/score/ + score/parts/
```

| 命令 | 关键选项 | 产物 |
|---|---|---|
| `init` | `--out/-o DIR`、`--title`、`--force` | `DIR/song.toml`（8 轨 6 段 / 36 小节；`init` 会立刻编排一遍自检） |
| `validate` | `--json` | 摘要（曲名/调性/速度/段落/轨道/音符/时长）或结构化错误 |
| `build` | `--out/-o DIR`、`--dry-run`、`--no-arrange`、`--no-humanize` | `<曲名>.mid` + `arrangement.json`；`--dry-run` 只打印通道分配与产物路径 |

`render` 的关键选项：

| 选项 | 说明 |
|---|---|
| `--soundfont` / `--fluidsynth` | 覆盖配置中的音色库与 fluidsynth 路径 |
| `--stems/--no-stems` | 是否导出分轨（默认导出） |
| `--format wav / flac` | 混音成品格式（**分轨恒为 WAV**：fluidsynth 只写 wav） |
| `--sample-rate` | 采样率（默认 44100） |
| `--no-normalize` | 不做峰值归一（默认归一到 −0.5 dBFS） |
| `--bank <id或路径>` | 全曲统一音源库（覆盖清单里每轨的 `bank`） |
| `--no-align-loudness` | 关闭跨音源库响度对齐 |
| `--no-arrange` | 不做自动编排（只用清单里的音符） |
| `--dry-run` | 只打印计划（含后端能力），不写任何文件 |

产物：`out/<曲名>/<曲名>.wav`（混音）、`stems/<轨道>.wav`（分轨）、`midi/<轨道>.mid`（每轨切片 MIDI）。

`score` 的选项见 [第 7 节](#7-总谱与分谱pr-f)。

只写 `[[section]]` + `[[track]]` 的 `role` 就够了：`compose build` 会先跑 **Arranger** 自动生成音符。
手写 `[[track.note]]` 的轨道不受 Arranger 影响（**显式优先**，可混用）。
`--no-arrange` 关闭自动编排，`--no-humanize` 关掉力度/微时序抖动（严格对齐网格）。

**音频渲染**（`compose render --stems`）在 PR-E 落地，总谱 v2（分谱组/移调/大谱页）在 PR-F 落地。

## 2. `song.toml` 契约

`beat` 一律是**全曲绝对拍**（4/4 下 1 小节 = 4 拍，含弱起偏移）。

```toml
schema = 1                    # 必填；当前只支持 1

[song]
title = "夜航"
key = "D minor"               # 项目约定写法：<音名> <major|minor>
tempo = 96                    # 基准速度；有 [[tempo]] 时以速度表为准
meter = "4/4"                 # 名义拍号；分母必须是 2 的幂
seed = 20261024               # PR-D 编曲引擎的确定性种子
pickup_beats = 0.0            # 可选：弱起拍（首段小节网格从弱起起点算起）
bars = 32                     # 可选：与段落 bars 合计交叉校验

[[tempo]]                     # 可选；首点 beat 必须为 0
beat = 0
bpm = 96
curve = "step"                # step | linear（linear 按对数积分求秒）

[[section]]                   # 段落首尾相接，起始拍由小节数自动累加
id = "verse"                  # 唯一
name = "主歌"
bars = 8
energy = 0.45                 # 0..1，驱动配器密度（PR-D 生效）
progression = ["Dm", "Bb", "F", "C"]   # 每小节一个和弦

[[track]]
id = "piano"                  # 唯一
name = "钢琴"
role = "harmony"              # melody/counter/harmony/arpeggio/pad/bass/drums/percussion
program = 0                   # 显式 GM 音色号（0-127）；`instrument` 供 PR-G 注册表解析
bank = "gm"                   # 音源库 id（2.1 起生效）
channel = 0                   # 可选：省略则自动分配（鼓固定 9）
octave = 4
range = [40, 96]              # 可选：音域，越界即报错
sections = ["verse", "chorus"]        # 参与段落；省略=全曲
muted = false
mix = { gain_db = -6.0, pan = -0.15, reverb_send = 0.18 }
density = 0.6                 # 可选（PR-D）
groove = "pop-half-time"      # 可选（PR-D）

[[track.note]]                # 显式音符（PR-C 手写路径）
beat = 0.0                    # 绝对拍；必须落在该轨 sections 内的段落里
pitch = 62
duration = 3.5                # 必须 > 0 且不越过所属段落结尾
velocity = 76                 # 0-127
release_velocity = 64
articulation = "accent"       # 可选，写入 MIDI 时随音符保留
offset_ticks = 0              # 可选，微时序（tick 偏移）
```

**严格性**：未知字段直接报错（拼写错误即刻暴露，AI 生成物友好）；任何多余字段
（如把 `mix` 的 `gain_db` 写成 `gain`）都会被指出路径。

## 3. 错误码与定位

| 码 | 含义 | 例子 |
|---|---|---|
| 40 | 清单语义/编曲非法 | `{"path": "/track/0/program", "code": 40, "message": "program 必须在 0..127，实际为 200"}` |
| 44 | 清单结构/schema 非法 | `{"path": "/song", "code": 44, "message": "未知字段: tmpo"}` |
| 45 | MIDI 通道资源不足 | 旋律轨超过 15 条 |

`--json` 输出即上面的对象（失败时附 `"ok": false`），可直接喂给 AI 做自动修复。

## 4. Python DSL

```python
from sunoauxtool.compose import SongBuilder

song = (
    SongBuilder("夜航", key="D minor", tempo=96, seed=20261024)
    .section("verse", bars=8, energy=0.45, progression="Dm-Bb-F-C")
    .section("chorus", bars=8, energy=0.85, progression="Bb-C-Dm-Dm")
    .track("piano", role="harmony", program=0, sections=["verse", "chorus"],
           mix={"gain_db": -6.0, "pan": -0.15})
    .track("bass", role="bass", program=33, octave=2)
    .note("piano", beat=0, pitch=62, duration=3.5)
    .build()          # -> compose.song.Song（唯一真源）
)
```

DSL 只做"收集"，编译成与 TOML **同构**的 `SongSpec`，因此两种写法能力与报错一致；
`SongBuilder.to_dict()` 可直接 `json.dump` 成清单再交给 AI 或版本管理。

## 5. Arranger：能量驱动的自动编曲

`compose build` 对"一个音符都没有"的轨道按 **段落能量 × 角色** 生成音符：

| 段落 energy | 参与角色 | 听感 |
|---|---|---|
| 0.00–0.30 | `pad` | 长音铺底，动机留白（前奏常用） |
| 0.30–0.60 | `harmony` `bass` `drums` | 和弦块状 + 根音贝斯 + 基础律动 |
| 0.60–0.85 | + `melody` | 加入有动机发展的主旋律 |
| 0.85–1.00 | + `counter` `arpeggio` | 对位声部、琶音、打击式和弦、加花 fill |

角色分工：

| 角色 | 生成策略 | 默认 GM 音色 | 默认音域 |
|---|---|---|---|
| `melody` | 动机 + 发展算子（重复/模进/倒影/逆行）+ 末句终止式 | 73 Flute | 60–84 |
| `counter` | 对主旋律做倒影/移位，避开同度撞音；无旋律时独立生成 | 68 Oboe | 55–79 |
| `harmony` | 每小节一次密集排列（`voice_chord` 平滑声部连接），高能量拆成打击式和弦 | 0 Piano | 48–72 |
| `arpeggio` | 八分音符上行循环和弦音 | 46 Harp | 55–84 |
| `pad` | 每小节一个长音和弦 | 89 Warm Pad | 48–76 |
| `bass` | 每小节根音（含转位低音）；能量 ≥0.55 加半拍推进、≥0.8 加五度 | 33 Finger Bass | 28–52 |
| `drums` | groove 图案 + 首拍 crash + 末小节 fill | 打击乐通道 9 | — |
| `percussion` | 预留（暂无生成器，留空不报错） | 47 Timpani | 40–60 |

鼓组 groove 名（`groove = "..."`）：`pop` / `rock` / `electronic` / `half_time` / `funk` /
`waltz` / `ballad`；不写时按能量选（低→ballad，中→pop，高→rock，3/4 一律 waltz）。

**确定性**（G8）：每条 `(段落, 轨道, 角色)` 用
`blake2b(f"{seed}:{section}:{track}:{role}")` 派生独立 RNG，因此

- 同清单 + 同 `seed` → MIDI 字节级一致（测试用 sha256 断言）；
- 改某轨 `density` 只影响该轨；**增删轨道不改变已有轨道的音符**（可增量编辑）；
- 换 `seed` → 旋律/律动换一版，结构与配器不变。

人性化（`humanize`）：力度 ±6%、微时序 ±1.5% 拍（写在 `offset_ticks`，不破坏节拍网格）。

## 6. 渲染与混音（PR-E）

**每轨单独渲染，再线性叠加**——这是唯一能保证"分轨 = 母带成分"的做法：

```
Song ─按轨切 MIDI→ midi/<id>.mid ─backend.render→ stems/<id>.wav
                                              └─mixer.mixdown→ <曲名>.wav
```

- 混音公式：`mix = Σ (gain_db · balance(pan) · stem)`，实测 `mix − Σstems` 峰值 **−155 dBFS**
  （M1 验收要求 ≤ −60 dBFS，即"母带里没有来路不明的信号"）；
- 声像用**立体声 balance 定律**（中置 1.0/1.0），刻意不用等功率——stem 已是立体声，
  再乘 0.707 会让所有中置轨无故 −3 dB；
- `mute`/`solo` 走通道条语义（有 solo 时只混 solo 轨）；
- 分块流式（默认 5 秒一块，两遍扫描：先求峰值再归一写盘），长工程不吃内存；
- 归一上限 64×（避免把极静的轨道放大成噪声墙）；**不做母带**（N6：不承诺发行级）。

后端解耦：`render/backend.py` 定义 `RenderBackend` 协议 + `RenderCapabilities`，
`FluidSynthBackend` 只是它的一个实现（旧 `FluidSynthRenderer` 一行未改）。
错误码 42（后端不可用）/43（分轨或混音失败）。

### 6.1 每轨独立选音源库（2.1）

```toml
[[track]]
id = "harp"
role = "arpeggio"
bank = "colombogmgs2"   # 音源库 id（soundbank list 第一列）；省略/写 gm/default → 用配置默认
```

- 解析顺序：`--bank` 全局覆盖 > 清单 `bank` > 配置默认音源；
- 未注册或校验失败 → **错误码 41**（报错里列出已注册 id）；
- 每轨单独渲染 → 每个 stem 用自己那套库 → 混音仍是线性叠加，语义不变。

**跨库响度对齐**：不同音源库同一 GM 音色的电平能差 10 dB 以上；不校正的话"换库"会变成
"换音量"。做法是**按库对齐**而不是"每轨拉平"：

1. 按 `bank` 把轨道分组，取每组实测 LUFS 的中位数作为该库电平；
2. 以默认音源为基准，算出每个库需要补的 dB（限幅 ±12 dB）；
3. 同库下所有轨道补同样 dB —— 库内平衡与 `mix.gain_db` 的意图完全保留。

实测（模板 8 轨，竖琴换 Colombo 260MB 库）：`跨库响度对齐：最大修正 +9.8 dB（harp）`。
单库工程恒为 0 修正（没有可比较对象），`--no-align-loudness` 可关闭。

> 环境提示：fluidsynth 与音色库不在 PATH 时，用
> `--fluidsynth module/fluidsynth/bin/fluidsynth.exe --soundfont module/GeneralUser_GS/GeneralUser-GS/GeneralUser-GS.sf2`。

## 7. 总谱与分谱（PR-F）

```bash
sunoaux compose score night/song.toml -o out/night/score         --format svg,png,musicxml --parts --composer "齐见林"
```

| 选项 | 说明 |
|---|---|
| `--format svg,png,musicxml` | 可多选（`xml` 是 `musicxml` 的别名） |
| `--parts` | 额外导出每声部分谱到 `parts/` |
| `--composer` | 署名（写进 SVG 标题区与 MusicXML `<creator>`） |
| `--no-part-groups` | 不输出 `<part-group>` 括号 |
| `--scale` | PNG 超采样倍数（默认 2.0） |

产物：`<曲名>.svg/.png/.musicxml`（总谱）+ `parts/<声部>.*`（分谱）。

**移调与分谱组**（本 PR 新增，属于 `Score` 模型层）：

| 能力 | 实现 |
|---|---|
| 移调乐器 | `[[track]] transpose = 2`（Bb 单簧管记谱比实音高 2 个半音）；`Song → Score → MusicXML` 全链透传 |
| 记谱音高 | MusicXML `<pitch>` 按记谱写出 + `<transpose><chromatic/diatonic>` 标注 |
| 分谱组 | 按角色自动分组（键盘组/弦乐组/低音组/旋律组/节奏组），MusicXML `<part-group type="start|stop">` + 括号 |
| 分谱 | `Score.parts()` 逐声部拆成独立谱（保留标题/调号/拍号/小节数/移调） |

> **已知范围**：SVG/PNG 按**实音**绘制（移调只体现在 MusicXML）；
> 大谱表（钢琴两行谱）与分页尚在 PR-F 之后单开。另：符干长度偏长的视觉问题
> 属 1.7.0 起的既有渲染行为（旧管线同样存在），不在本 PR 改动范围内。

## 8. 音源库（PR-G）

```bash
sunoaux soundbank list                 # 列出 module/ 下发现的音源（含结构校验）
sunoaux soundbank info generaluser-gs  # 详情：路径/格式/体积/许可/校验
sunoaux soundbank verify --all         # 批量结构校验；有失败则退出码 41
```

- **只登记，不分发**：扫描 `module/` 下的 `.sf2`（未来 `.sf3`/`.sfz`），只描述你机器上已有的文件；
  `license` 字段对常见免费库给出提示，**不代替原许可证**（请自行确认商业使用条件）。
- **结构校验**（不启动 fluidsynth）：存在性 + 体积下限（≥64KB）+ `RIFF/sfbk` 魔数；
  `.sfz` 检查是否有 `<region>/<group>`。真正的"能不能发声"由渲染阶段暴露（错误码 4/7）。
- **id 规则**：文件名 slug 化（`GeneralUser-GS` → `generaluser-gs`）；
  同名不同文件时自动追加父目录名（`gm-x` / `gm-y`），保证扫描结果确定。
- **扫描根**：`SUNOAUX_MODULE_DIR`（显式指定时**只**认它）> 当前目录向上的项目根 >
  安装包所在仓库根。只认 cwd 会让"在别的目录跑 `sunoaux compose render`"找不到音源库。
- `doctor` 新增"音源库"一行：`✅ 2/2 套可用（colombogmgs2, generaluser-gs）`。
- 错误码 **41**：未注册 id、文件缺失、体积异常、非 RIFF/sfbk 容器。

## 9. 与旧命令的关系

| 场景 | 用哪个 |
|---|---|
| 快速出 3–4 轨 demo | `sunoaux pre midi`（旧 `generate midi`，2.0 冻结不动） |
| 多轨、可复现、要清单/审计 | `sunoaux compose build` |
| 只要有声 | `sunoaux pre midi` + `sunoaux pre render` |

旧命令在 2.0 全周期保留；`compose` 不改变它们的行为。

## 10. 后续（按里程碑推进）

| PR | 内容 | 用户可见变化 |
|---|---|---|
| ~~D~~ | ~~Arranger v1~~ | 已交付：清单只写 `role`/`energy` 就能生成音符 |
| ~~E~~ | ~~渲染 v2（后端协议 / 分轨 / 混音）~~ | 已交付：`compose render` → mix + stems/ + midi/ |
| ~~F~~ | ~~总谱 v2（移调 / 分谱组 / 分谱）~~ | 已交付：`compose score` → 总谱 + 分谱 |
| F+ | 大谱表（钢琴两行）、分页、演奏记号 | 更接近出版级总谱 |
| 2.2 | M3 表现力：演奏法/力度记号/摇摆进 MIDI 与总谱 | 更像"演奏"而不是"打谱" |
| 2.3 | M4 AI 编曲：LLM 产出清单 → 校验 → 渲染闭环 | 自然语言到成品 |
| ~~G~~ | ~~SoundBank 注册表 + doctor~~ | 已交付：`soundbank list/info/verify` |
| ~~2.1~~ | ~~每轨独立选音源库 + 跨库响度对齐~~ | 已交付：`[[track]] bank`、`--bank`、按库对齐 |
