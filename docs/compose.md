# compose：代码驱动的多轨编曲（2.0）

> 状态：**2.0 PR-D 已交付**（清单解析 + DSL + Arranger 自动编曲 + `compose init/validate/build`）。
> 分轨渲染（`compose render --stems`）与总谱 v2 在 PR-E/F 接入。

`compose` 是"数字轴"入口：一份 `song.toml`（或 Python DSL）完整描述一首曲子，
构建出**全保真 MIDI**（format 1，含速度表/拍号/调性/CC/音色库）与**编排快照**，
零交互、字节级可复现。

## 1. 三个命令

```bash
sunoaux compose init night                 # 生成 night/song.toml（8 轨 6 段模板，可直接构建）
sunoaux compose validate night/song.toml   # 校验；--json 给机器读
sunoaux compose build  night/song.toml     # -> out/night/night.mid + arrangement.json
```

| 命令 | 关键选项 | 产物 |
|---|---|---|
| `init` | `--out/-o DIR`、`--title`、`--force` | `DIR/song.toml`（8 轨 6 段 / 36 小节；`init` 会立刻编排一遍自检） |
| `validate` | `--json` | 摘要（曲名/调性/速度/段落/轨道/音符/时长）或结构化错误 |
| `build` | `--out/-o DIR`、`--dry-run`、`--no-arrange`、`--no-humanize` | `<曲名>.mid` + `arrangement.json`；`--dry-run` 只打印通道分配与产物路径 |

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

## 6. 与旧命令的关系

| 场景 | 用哪个 |
|---|---|
| 快速出 3–4 轨 demo | `sunoaux pre midi`（旧 `generate midi`，2.0 冻结不动） |
| 多轨、可复现、要清单/审计 | `sunoaux compose build` |
| 只要有声 | `sunoaux pre midi` + `sunoaux pre render` |

旧命令在 2.0 全周期保留；`compose` 不改变它们的行为。

## 7. 后续（按 PR 推进）

| PR | 内容 | 用户可见变化 |
|---|---|---|
| ~~D~~ | ~~Arranger v1~~ | 已交付：清单只写 `role`/`energy` 就能生成音符 |
| E | 渲染 v2（多库、stems、混音） | `compose render --stems` → mix.wav + stems/ |
| F | 总谱 v2（Part/移调/大谱表/分页） | `compose build --score svg,musicxml` |
| G | SoundBank 注册表 | `soundbank list/info/verify`，每轨独立选库 |
