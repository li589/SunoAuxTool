# SunoAuxTool 功能清单（三组件全量）

> 版本快照：`sunoauxtool 0.5.4` · `videomaker 0.3.0` · `suno-cat-catch-resolve 0.1.0`
> （历史快照，组件现已统一为单一发行版 `sunoauxtool` 1.0.0；旧版本号不再更新）
> 生成日期：2026-09-20 ｜ 依据：源码（`src/`）与 CLI 实测 `--help`，非文档转述

## 一、概览

同一个仓库下三个**独立可安装**的包，共用一套错误码分段。

| # | 组件 | 发行名 / 入口 | 定位 | 依赖方向 |
|---|---|---|---|---|
| 1 | 音乐生成 CLI | `sunoauxtool` | 程序化多轨 MIDI → 渲染 WAV → Suno 合规导出 | 无（底座） |
| 2 | 音频可视化 | `videomaker` | 音频 → 音乐视频（多平台预设、多视觉风格） | → sunoauxtool |
| 3 | Suno 取证转码 | `Suno-Cat-Catch-Resolve` | 猫抓 Suno 产物逆向取证 + 解码转码 | 独立（零依赖） |

```
sunoauxtool  ──►  videomaker            （渲染/混音复用）
sunoauxtool  ──►  Suno 合规片段 ──►  Suno 平台
suno-cat-catch-resolve                   （独立，CLI 自足；核心层零第三方依赖）
```

主链路：**generate → render → DSP → export suno**（`pipeline` 一条命令串起）。

---

## 二、组件一：sunoauxtool 0.5.4

### 2.1 命令树（24 条命令）

顶层选项：`--config/-c`、`--verbose`、`--quiet`、`--debug`、`--version`。
所有命令统一由 `_guard` 包裹：异常 → 稳定退出码 + 分级日志（`--debug` 附堆栈）。

#### generate（2）

| 命令 | 功能 | 关键参数 |
|---|---|---|
| `generate midi` | 程序化生成多轨 MIDI（和弦/旋律/贝斯 [+鼓]） | `--chords --bpm --key --time-signature --bars --style --seed --with-drums --track`（可重复）`--voice-leading --counterpoint --inversion --rhythm --project --output-dir --output` |
| `generate melody` | music21 乐理驱动旋律生成 + 变奏 | 同上（旋律向） |

#### export（3）

| 命令 | 功能 |
|---|---|
| `export suno` | Suno 合规导出（10–30s 纯器乐 WAV/MP3，**恒禁混响**） |
| `export suno-pack` | 批量导出 Suno 片段 + 打包 |
| `export suno-manifest` | 生成 Suno 上传清单（CSV/JSON） |

#### config（2）

| 命令 | 功能 |
|---|---|
| `config init` | 生成配置模板（交互式引导，`--yes` 非交互） |
| `config show` | 打印合并后的生效配置 |

#### ai（2，P1 适配器）

| 命令 | 功能 |
|---|---|
| `ai musicgen` | MusicGen：旋律 WAV → 伴奏 WAV |
| `ai diffrhythm` | DiffRhythm：风格提示 → 歌曲草稿 WAV |

> 适配器顶部**零 torch import**，未装依赖时以错误码 6 明确失败，不拖慢 P0 环境。

#### inspire（6，SQLite 灵感库）

| 命令 | 功能 |
|---|---|
| `inspire init` | 初始化灵感库（创建 `sunoauxtool.db`） |
| `inspire add` | 从 WAV + 元数据提取并入库 |
| `inspire list` | 列出灵感（支持标签/评分/多维筛选） |
| `inspire show` | 查看灵感详情 |
| `inspire rm` | 从库中移除（**不删文件**） |
| `inspire export` | 导出灵感文件到指定目录 |

#### 顶层（9）

| 命令 | 功能 |
|---|---|
| `render` | MIDI → WAV（FluidSynth + SoundFont，**真实引擎**） |
| `score` | MIDI → 谱面（五线谱 SVG/PNG、简谱、MusicXML 4.0；6 格式，见 [score.md](score.md)） |
| `pipeline` | 一键管线 generate → render → DSP → export（零参数可跑通 demo） |
| `batch` | 批量生成多变体（随机化 + 可复现 + 失败隔离） |
| `new` | 交互式引导生成新音乐（新手指南） |
| `diff` | 对比两个 WAV 的音频特征（时长/RMS/峰值/频谱中心/频段能量） |
| `play` | 用系统默认播放器播放 WAV |
| `doctor` | 一键环境健康检查（Python/FluidSynth/SF2/CUDA/AI 依赖/espeak…） |
| `errors` | 打印错误码表 |

`pipeline` 的 DSP/导出开关：`--duration --format --fade-in --fade-out --eq --compressor
--reverb`（**显式报错**，当前未支持）`--no-preview --dry-run`。

### 2.2 核心能力模块

| 模块 | 能力 |
|---|---|
| `generators/procedural.py` | 程序化多轨生成：动机驱动的旋律乐句 + 伴奏织体 |
| `generators/music21_melody.py` | music21 乐理驱动旋律 + 变奏 |
| `models/`（chords / midi / notes） | 和弦、MIDI 文档、音符数据模型 |
| `music_theory/voice_leading.py` | 平行五度/八度检测（`VoiceLeadingChecker`） |
| `music_theory/counterpoint.py` | 二声部对位约束（`CounterpointEngine`） |
| `music_theory/inversion.py` | 和弦转位（低音平滑，`InversionResolver`） |
| `music_theory/rhythm_patterns.py` | 节奏型注册表（`RhythmPatternRegistry`） |
| `music_theory/postprocess.py` | 生成后处理 |
| `dsp/filters.py` | `highpass`（低频切 EQ）、`compressor`（轻压缩）、`expander`（向下扩展）、`limiter`（前瞻拐点限幅，brickwall） |
| `dsp/processor.py` | `DspProcessor` + `DspOptions`（淡入淡出等） |
| `render/fluidsynth.py` | FluidSynth 真实渲染（`FluidSynthRenderer`） |
| `export/audio.py` | 通用音频导出 |
| `export/suno.py` | Suno 合规校验（时长 10–30s、采样率白名单、禁混响） |
| `sunopack.py` | Suno 片段打包与清单 |
| `preview.py` | HTML 预览页（含音频特征；`pipeline --score` 时内嵌五线谱 SVG）+ 视频缩略帧 scrub 预览（`video-preview`，产物落在 `output/preview/<视频名>/`） |
| `score/`（9 子模块） | 乐谱子系统：乐理 → 中间表示 → 排版 → 五线谱 / 简谱 / MusicXML / 位图 |
| `score_export.py` | 谱面格式归一化与统一落盘（`score` 子命令 / `generate` / `pipeline` 共用） |
| `output_manager.py` | 输出布局管理（`project/date` + seq 防覆盖 + `metadata.json`） |
| `env.py` / `platform_paths.py` | 环境探测与**平台感知回落**（Windows 恒不触发 POSIX 分支） |
| `styles/registry.py` + `styles/presets/*.toml` | 风格注册表与预设 |
| `batch.py` | 批量执行与失败隔离 |
| `inspire.py` | 灵感库（SQLite） |
| `exceptions.py` | 错误码 0–9 |
| `commands/helpers.py` | 命令层公共件（时长探测、元数据写入、diff、交互提示） |

**风格预设（4）**：`pop` / `rock` / `electronic` / `classical`（`styles/presets/*.toml`，支持自定义）。

### 2.3 关键约束

- **Suno 合规**：目标时长 ∈ [10, 30] s；采样率 ∈ {8k, 16k, 22.05k, 24k, 44.1k, 48k, 96k}；
  纯器乐；**请求混响即显式报错**（不静默忽略）。
- **可复现**：`--seed` 全链路生效，`metadata.json` 记录种子与参数。

---

## 三、组件二：videomaker 0.3.0

### 3.1 命令（5）

| 命令 | 功能 |
|---|---|
| `render` | 单条视频渲染：音频 → MP4（多文件 = **多轨混音**） |
| `multi` | 一个音频一次产出**多平台**视频（发布闭环） |
| `presets` | 列出/查看平台预设详情 |
| `config` | 配置查看/初始化（`show` / `init`，`--path` 指定文件） |
| `version` | 打印版本 |

`render` 参数：`--output/-o --preset/-p --style/-s --width --height --fps
--title/-t --subtitle --font-size --logo --logo-pos`。
输入支持 **WAV/MP3/FLAC/OGG/MIDI**（MIDI 走 sunoauxtool 的 FluidSynth 真实引擎）；
多轨语法 `file.wav:gain=0.8:pan=-0.3`。

### 3.2 视觉风格（6）与双引擎路由

| 风格 | 引擎 | 说明 |
|---|---|---|
| `waveform` | ffmpeg 原生（`showwaves`） | 波形 |
| `spectrum` | ffmpeg 原生（`showspectrum`） | 频谱 |
| `circular_spectrum` | PIL FrameEngine | 环形频谱 |
| `reactive` | PIL FrameEngine | 节拍/能量响应 |
| `tracks` | PIL FrameEngine | **分轨**频谱垂直排列，HSL 色环区分，逐轨独立归一化 |
| `waveform_scroll` | PIL FrameEngine | **滚动波形**，播放头居中，波形查表滑动窗口 |

### 3.3 平台预设（4）

`douyin`(9:16) · `youtube`(16:9) · `instagram`(1:1) · `official`(4:5)；默认 `douyin`。

### 3.4 核心能力模块

| 模块 | 能力 |
|---|---|
| `analysis/audio_analysis.py` | `analyze()` 一次产出频谱矩阵 + RMS 包络；`analyze_multitrack()` 额外产出 `track_spectrograms` / `track_rms`；含 onset 检测与音频特征 |
| `engines/ffmpeg_engine.py` | 原生滤镜引擎（快路径） |
| `engines/frame_engine.py` | PIL 逐帧 + **rawvideo 管道直写 ffmpeg stdin**（零 PNG 中间文件，3s 音频 21s → 0.8s），stderr 线程排空防死锁 |
| `visuals/`（base/spectrum/reactive/tracks） | 视觉实现与注册表 |
| `compositor.py` | 风格 → 引擎路由（`FFMPEG_STYLES`） |
| `mixer.py` | 多轨混音：重采样对齐 → 等功率声像 → 峰值归一化 −1 dBFS → 产出 `*.mix.wav` |
| `text.py` | `drawtext` 文字/标题封装 |
| `audio_io.py` | 多格式读取（soundfile，MP3 原生） |
| `output_manager.py` / `config.py` / `exceptions.py` | 输出、配置、错误码 10–14 |

---

## 四、组件三：Suno-Cat-Catch-Resolve 0.1.0

发行名与可导入名**分离**：目录 `Suno-Cat-Catch-Resolve`，包 `suno_cat_catch_resolve`。

### 4.1 命令（4）

| 命令 | 功能 | 关键参数 |
|---|---|---|
| `probe` | 取证：判定明文 / 密文并给出证据 | `--ffmpeg-path` |
| `decode` | 解码 fMP4（密文会显式报错，不产出垃圾文件） | `-o/--out --fmt opus\|mp3\|both --stem --bitrate --ffmpeg-path` |
| `batch` | 扫描目录批量解码，密文自动跳过并汇总报告 | `-o/--out --fmt --bitrate --ffmpeg-path` |
| `version` | 打印版本与 ffmpeg 位置 | — |

### 4.2 取证判据（核心结论）

熵**不足以**区分明文/密文（Opus 7.9985 vs 密文 8.0000）；可靠判据是**卡方 χ²(df=255)**：

| 输入 | χ² 实测 | 判定 |
|---|---|---|
| 明文压缩音频 | ≈ 11820 | 明文 |
| 强加密密文 | ≈ 215（均匀随机均值 255） | 密文 |

判据 `χ² ≤ 360` 判为密文，再用**周期扫描**排除重复密钥 XOR。
结论：猫抓直下的 `<uuid>.m4a` 是 AES/ChaCha20 级强加密，无密钥数学上不可破；
缓存捕获的 fMP4（常被误标 `.mp3`，实为 Opus-in-fMP4）是同一首曲子的**完整明文副本** → 解码它。

### 4.3 模块

| 模块 | 能力 |
|---|---|
| `fmp4.py` | ISO BMFF 原子解析（32/64 位长度、size=0 延伸 EOF、截断处理）、mdat 分片提取、`summarize` |
| `forensics.py` | 熵 / 卡方 / 周期扫描、5 种容器魔数 + MP3 帧同步识别、`classify` / `identify` / `Verdict` |
| `transcoder.py` | ffmpeg 定位（五级回落）、Ogg Opus 无损重封装 / MP3 转码 / `probe`、错误分级 20–24 |
| `cli.py` | Typer 命令行；核心层零第三方依赖 |

---

## 五、三组件对照

| 维度 | sunoauxtool | videomaker | Suno-Cat-Catch-Resolve |
|---|---|---|---|
| 版本 | 0.5.4 | 0.3.0 | 0.1.0 |
| CLI 命令数 | 23 | 5 | 4 |
| 错误码段 | 0–9 | 10–14 | 20–24 |
| 主依赖 | Typer / music21 / pretty_midi / FluidSynth / numpy | ffmpeg / Pillow / soundfile | 仅 CLI 需 Typer，**核心层零三方依赖** |
| 测试 | 443 例 | 56 例 | 147 例 |
| 覆盖率 | 88.48% 本地 / 88.26% CI（门槛 87%） | — | 100%（门槛 95%） |
| CI 步骤 | `pytest --cov-fail-under=87` | 随根套件（`ruff check src/` 会 lint 到） | 独立 `pip install -e` + `pytest --cov-fail-under=95` |
| 是否被主包收录 | 是 | **是**（故 CI lint 覆盖） | 否（`packages.find` 排除，须单独跑） |

## 六、错误码总表（0–24）

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 参数错误 / 通用错误（未知子命令、非法参数组合、非法 DSP 参数） |
| 2 | 配置错误（配置缺失/非法、显式路径无效） |
| 3 | 输入文件错误（`.mid`/`.wav` 不存在或无法解析） |
| 4 | 渲染失败（fluidsynth 进程非零退出） |
| 5 | 导出失败（时长越界、MP3 编码器缺失） |
| 6 | AI 模块不可用（依赖未装、显存不足、DiffRhythm NO-GO） |
| 7 | 渲染环境不完整（module 缺失/损坏、fluidsynth 不可执行、SF2 不可加载） |
| 8 | 批量部分失败 |
| 9 | 批量全部失败 |
| 10 | ffmpeg 不可用（videomaker） |
| 11 | 分辨率/比例不支持 |
| 12 | 输入音频缺失或无法读取 |
| 13 | 渲染失败（ffmpeg 非零退出） |
| 14 | 输出路径不可写 |
| 20 | ffmpeg 不可用（suno） |
| 21 | 不是可解析的 fragmented MP4 |
| 22 | 输入是加密密文（无密钥不可解码） |
| 23 | 转码失败 |
| 24 | 输出路径不可写 |

（15–19 未分配，作为后续组件的预留分段。）

## 七、典型端到端链路

```bash
# 链路 1：从零到 Suno 可上传片段
sunoauxtool pipeline --style pop --seed 7 --duration 20 --format mp3
sunoauxtool export suno-manifest ./output -o ./upload.csv

# 链路 2：音频 → 多平台音乐视频
videomaker render song.wav -p douyin  -s reactive -t "曲名"
videomaker multi  song.wav -p douyin,youtube,instagram,official

# 链路 3：猫抓产物取证 → 解码
suno-cat-catch-resolve probe  "Suno _ AI Music.mp3"
suno-cat-catch-resolve decode "Suno _ AI Music.mp3" -o ./out --fmt both
suno-cat-catch-resolve batch  ./downloads -o ./out
```
