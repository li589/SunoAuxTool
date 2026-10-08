# SunoAuxTool 使用指南

> 版本：v1.3.0｜ 配套文档：docs/PRD.md、docs/archive/architecture-P0.md、docs/task-plan.md

---

## 1. 全局参数

```bash
sunoauxtool [--config PATH] [--verbose] [--version] <子命令>
```

| 参数 | 说明 |
|---|---|
| `--config PATH` / `-c` | 指定配置文件（默认查找项目根 `sunoauxtool.toml`） |
| `--verbose` | DEBUG 级日志 |
| `--version` | 打印版本号 |

配置合并优先级（低→高）：内置默认值 < `config/default.toml` < 用户配置文件 < CLI 参数。

---

## 2. 子命令详解

### 2.1 `generate midi` — 程序化多轨 MIDI

```bash
sunoauxtool generate midi [--chords CHORDS] [--bpm N] [--key KEY] [--time-signature TS]
                           [--bars N] [--style STYLE] [--seed N] [--with-drums]
                           [--track NAME ...] [--output PATH]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--chords` | `C-G-Am-F` | 和弦进行，`-` 分隔；支持 C/Am/G7/Am7/Cmaj7/Csus4 等 |
| `--bpm` | 120 | 速度 |
| `--key` | `C major` | 调式（music21 可解析，如 `A minor`、`F# minor`） |
| `--time-signature` | `4/4` | 拍号 |
| `--bars` | 8 | 小节数 |
| `--style` | `pop` | 风格：`pop` / `rock` / `electronic` / `classical` |
| `--seed` | 配置值 | 随机种子（可复现） |
| `--with-drums` | false | 追加第 4 轨鼓（通道 9） |
| `--track` | chords melody bass | 轨道名，可多次指定 |
| `--output` | 自动命名 | 输出 .mid 路径 |

**示例：**
```bash
sunoauxtool generate midi --chords "C-G-Am-F" --bpm 120 --bars 8 --seed 42
sunoauxtool generate midi --chords "G-D-Em-C" --style rock --with-drums --seed 7
```

**可复现性：** 相同参数 + 相同 `--seed` → 字节级一致的 .mid。

### 2.2 `generate melody` — music21 乐理旋律 + 变奏

```bash
sunoauxtool generate melody [--key KEY] [--chords CHORDS] [--variations N] [--seed N] ...
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--variations` | 1 | 变奏数量；kinds = `rhythm`（节奏）/ `ornament`（装饰音）/ `retrograde`（逆行），按序循环 |
| 其余 | 同 `generate midi` | — |

输出 1 个主旋律轨 + N 个变奏轨（同文件多轨）。强拍（第 1、3 拍）与句尾目标音对齐和弦音 ≥80%，全部音符在调式音阶内。

**示例：**
```bash
sunoauxtool generate melody --key "C major" --chords C-G-Am-F --variations 3 --seed 5
```

### 2.3 `render` — MIDI → WAV

```bash
sunoauxtool render --input xxx.mid [--soundfont PATH] [--fluidsynth PATH] [--output PATH]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--input` | 必填 | 输入 .mid |
| `--soundfont` | 配置值 | .sf2 路径 |
| `--fluidsynth` | 配置值 | fluidsynth 可执行文件（PATH 名或绝对路径） |
| `--output` | 输入同名 `_rendered.wav` | 输出 .wav |

输出 44.1kHz / 16bit WAV。fluidsynth 缺失 → 退出码 4；SoundFont 缺失 → 退出码 2。

### 2.4 `export suno` — Suno 合规导出

```bash
sunoauxtool export suno --input xxx.wav [--duration 10..30] [--format wav|mp3]
                         [--sample-rate N] [--bit-depth N] [--fade-ms N] [--output PATH]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--duration` | 25 | 目标时长，**必须在 10–30s**（越界 → 退出码 5） |
| `--format` | `wav` | `wav` / `mp3`（mp3 需 lameenc） |
| `--sample-rate` | 44100 | 输出采样率 |
| `--bit-depth` | 16 | 输出位深 |
| `--fade-ms` | 50 | 淡入淡出毫秒数 |

处理链：合规校验 → 裁剪/循环至目标时长 → 淡入淡出 → 重采样 → -1dBFS 归一化 → 写出。
输入不足目标时长时自动循环补齐。

### 2.5 `pipeline` — 一键闭环

```bash
sunoauxtool pipeline [--chords ...] [--bpm ...] [--key ...] [--bars ...]
                      [--style ...] [--seed ...] [--with-drums]
                      [--duration 10..30] [--format wav|mp3]
```

零参数即跑通 demo 管线：generate → render → export，中间产物自动清理，
最终输出 `output/YYYYMMDD/{style}_{key}_{bpm}_{bars}bars_{seed}_{ts}_suno{duration}s.{ext}`。

### 2.6 `config init` / `config show`

```bash
sunoauxtool config init [--path sunoauxtool.toml]   # 生成配置文件模板
sunoauxtool config show                              # 打印合并后的生效配置
```

### 2.7 `batch`（P1-3 骨架）

```bash
sunoauxtool batch --count 5 --seed 42
```

P0 版本提示"批量生成属于 P1-3 里程碑"（退出码 1）。

### 2.8 `ai musicgen` / `ai diffrhythm`（P1 二期）

```bash
# MusicGen：以旋律 WAV 为条件扩编曲/生成器乐伴奏
sunoauxtool ai musicgen --input melody.wav --prompt "upbeat pop" \
                         [--output out.wav] [--duration 20] [--model-size medium|small] \
                         [--seed N] [--device cuda|cpu]

# DiffRhythm：风格提示（+ 可选歌词）→ 完整歌曲草稿（带人声）
sunoauxtool ai diffrhythm --prompt "slow ballad" \
                           [--input ref.wav] [--output song.wav] \
                           [--lyrics "歌词"] [--duration 95] [--device cuda|cpu] \
                           [--diffrhythm-dir /abs/path/to/DiffRhythm]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `ai musicgen --input` | 必填 | 输入旋律 WAV（melody conditioning 条件） |
| `ai musicgen --prompt` | 必填 | 风格提示，如 `"upbeat pop"` |
| `ai musicgen --output` | 输入旁 `*_musicgen.wav` | 输出伴奏 WAV（32kHz） |
| `ai musicgen --duration` | 对齐输入（10–30s） | 目标时长（秒），越界退出码 1 |
| `ai musicgen --model-size` | `medium` | `medium`（映射 `facebook/musicgen-melody`，1.5B fp16，支持旋律条件）/ `small`（300M 降档；不支持旋律条件，自动降级为纯文本生成） |
| `ai musicgen --seed` | 随机 | 随机种子（同参数 + 同 seed 可复现） |
| `ai musicgen --device` | `cuda` | `cuda` / `cpu`（CPU 较慢） |
| `ai diffrhythm --prompt` | 必填 | 风格提示，如 `"slow ballad"` |
| `ai diffrhythm --input` | 可选 | 参考音频 WAV（当前版本保留接口，实际以风格提示为主） |
| `ai diffrhythm --output` | 自动命名 | 输出歌曲草稿 WAV（44.1kHz，含人声） |
| `ai diffrhythm --lyrics` | 空（哼唱） | 歌词纯文本（行分隔），自动转为 LRC 时间戳 |
| `ai diffrhythm --duration` | 95 | 目标时长：**95 或 96–285s**（官方限制），非法退出码 1 |
| `ai diffrhythm --device` | `cuda` | `cuda` / `cpu` |
| `ai diffrhythm --diffrhythm-dir` | 由 `DIFFRHYTHM_DIR` / `module/diffrhythm` 决定 | DiffRhythm 仓库根目录（绝对/相对路径）。命令行级覆盖仓库位置，优先级高于 `DIFFRHYTHM_DIR` 环境变量与默认的 `module/diffrhythm`，无需把仓库放在 `module/` 下 |

**行为与合规：**
- MusicGen 输出 32kHz WAV，可被 `export suno` 消费（导出链内部重采样到 44.1kHz）。
- MusicGen 默认 `medium` 实际加载 `facebook/musicgen-melody`（1.5B，支持旋律条件 `generate_with_chroma`；`musicgen-medium` 不支持 chroma，属实测修正）；`small` 降档为 300M 纯文本生成（不包含旋律对齐）。
- MusicGen 显存不足时给出友好提示与降档建议（`--model-size small`），不崩溃（退出码 6）。
- DiffRhythm 使用 `chunked=True` 分块推理（8GB 显存必需，适配器自动注入补丁，无需手动改脚本）；依赖 espeak-ng（Windows 需 .msi 安装并加入 PATH）；显存 <8GB 时明确提示不可用（退出码 6）。
- **DiffRhythm 草稿（含人声）不自动进入 Suno 导出链**（违反纯器乐合规），用途为"本地听感预览"。
- 未安装 AI 依赖时，两命令给出安装指引并退出码 6，不触发 torch import。

### 2.9 `score` — MIDI → 谱面（#12）

```bash
sunoauxtool score <mid> [--format all|svg,png,jianpu,jianpu_svg,musicxml,text]
                  [--key KEY] [--time-signature N/D] [--clef 轨道=treble|bass,...]
                  [--out-dir DIR]
```

- 6 种格式：五线谱 SVG/PNG（**Pillow 渲染，非 matplotlib**）、简谱 SVG/PNG、
  MusicXML 4.0（纯标准库）、简谱文本。
- `generate midi|melody --score` 同目录落谱；`pipeline --score` 附加产物失败只告警。
- 深度说明（分层架构、字体、校验）见 [docs/score.md](score.md)。

### 2.10 `tempo` — 音频测速（#13）

```bash
sunoauxtool tempo <wav> [--min-bpm 40] [--max-bpm 240] [--prior-bpm 120]
```

- numpy-only：onset 包络 → ACF → 节奏先验（log-Gaussian，中心 120BPM）消解
  倍频歧义 → (bpm, phase) 联合梳状搜索（相位得分抛物线细化）。输出
  `(bpm, confidence, beat_offset)`。
- `--prior-bpm 0` 关闭先验；≥160BPM 素材在默认先验下会被折半，需显式指参。
- **beat_offset 相位精度（1.5.1）**：合成点击轨 mean 1.25ms / max 4.24ms
  （通量峰位校正 `1.25·argmax(Hann²[p]−Hann²[p+hop])`）；软起音素材存在
  ~+60ms 谱通量形态固有偏差（最大谱增位点 ≠ 声学起始点）。

### 2.11 `transcribe` — WAV → MIDI 转谱（#13）

```bash
sunoauxtool transcribe <wav> [-o out.mid] [--bpm auto|N] [--grid 1/16]
                        [--backend builtin|basic-pitch] [--program 0]
```

- `builtin` 后端只适合**单旋律/主导声部**（谐波 salience + 相对凹谷切重复音 +
  网格量化）；复调请用 `--backend basic-pitch`（可选依赖，未装退出码 6）。
- 窗长 2048 / 帧中心时间戳：为 1/16 网格量化精度做的取舍（4096 会因窗尾泄漏
  让音界提前 ~80ms）。

#### 安装 basic-pitch（2026-09-22 实测）

`basic-pitch` 最新版即 **0.4.0**，其依赖标记决定了装法，直接 `pip install` 在本项目
（`requires-python >=3.12`）上**会失败**：

| 环境 | 依赖标记命中 | 结果 |
|------|------------|------|
| Windows + Python **<3.11** | `onnxruntime`（自动） | 直接装即可 ✅ |
| Windows + Python **≥3.11** | `tensorflow<2.15.1`（无 3.12 轮子） | 装不上 ❌ |

但 **0.4.0 的代码本身能在 3.12 上跑**（已实测：ONNX 后端导入正常、`predict()` 端到端
出 MIDI），阻碍只在 pip 解析阶段。绕开办法是跳过依赖解析、手动补装：

```bash
pip install basic-pitch --no-deps
pip install resampy "mir-eval" onnxruntime
# librosa / scipy / numpy / pretty_midi / scikit-learn 主环境已有，无需重复
```

**坑**：若 `--no-deps` 后漏装任何推理后端，`basic_pitch/__init__.py` 的后端选择链
（`if/elif`，**无 else 分支**）会在模块级抛 `NameError: _default_model_type is not defined`。
本适配器已把这类「装了包但没装后端」收口成**退出码 6**，并提示 `pip install onnxruntime`，
不会再漏成退出码 1 的裸 NameError。后端优先级：TF > CoreML > TFLite > ONNX。

### 2.12 `analyze` — 调性 / 和弦 / 段落（R13）

```bash
sunoauxtool analyze <wav> [--key] [--chords] [--structure] [--json]
```

- 子项全关时默认三项全跑。numpy-only：`estimate_key` 用色度直方图 ×
  Krumhansl-Kessler 权重 Pearson 相关；`estimate_chords` 用 48 个三和弦模板
  余弦相似度（相邻同和弦合并）；`estimate_structure` 用自相似矩阵 + Foote
  棋盘核新奇度取峰值切段。
- 均为**启发式估计**，用于辅助编曲/打点，不做权威判定。

### 2.13 `video-preview` — 视频缩略帧预览（R15）

```bash
sunoauxtool video-preview <video> [--frames 6] [-o out_dir]
```

- 用 ffprobe 取时长 → 在 `(i+0.5)*时长/n` 处抽 n 帧 jpg → 生成单文件 HTML，
  点缩略图即 seek 到对应时间点。
- 产物落在**新建目录** `<out>/preview/<视频名>/`（默认 `output/preview/...`），
  与既有音频/视频产物不冲突；`<out>` 留空时取配置里的 `paths.output_dir`。
- 依赖 ffmpeg/ffprobe；视频不存在退出码 3。

### 2.14 `post enhance` — AudioSR 音质提升（R5）

```bash
sunoaux post enhance <wav> [-o out.wav] [--model basic|speech] [--seed 42]
                         [--steps 50] [--chunk 15.0] [--overlap 2.0]
```

- 音频超分/高频重建（**不是人声分离**）：12kHz/24kHz 低清素材 → 48kHz PCM_24；
  长音频自动分块（15s 块 / 2s Hann 重叠交叉淡化），输出为**单声道**（上游行为）。
- 依赖走源码目录模式（`src/versatile_audio_super_resolution` 或 `AUDIOSR_DIR`），
  权重首次运行自动下载（~2.6GB，模型 `haoheliu/audiosr_basic`）；未装退出码 6。
- **耗时参考（RTX 4060，2026-10-07 实测）**：8s@12kHz 单块 DDIM50 ≈ 19.5s（纯推理）；
  整首歌按 15s 分块线性放大（5 分钟歌约 45 分钟 GPU），**只适合对成品单曲做精修**，
  不要放进批量/流水线。
- **网络**：权重/tokenizer 校验走 huggingface.co，直连不通时**必须设镜像**：
  `HF_ENDPOINT=https://hf-mirror.com`；权重已缓存后可加 `HF_HUB_OFFLINE=1` 跳过联网校验。
  未设镜像时每次加载会先卡 5 轮重试（~25s）再回退缓存。
- 安装细节与验证记录见 `requirements/vasr.txt`。

---

## 3. 配置文件详解

```toml
[paths]
soundfont = "assets/soundfonts/GeneralUser_GS_v1.471.sf2"  # SoundFont 路径
fluidsynth = "fluidsynth"                                   # fluidsynth 可执行名/绝对路径
output_dir = "output"                                       # 输出目录

[defaults]
bpm = 120
key = "C major"
time_signature = "4/4"
bars = 8
chords = "C-G-Am-F"
style = "pop"
tracks = ["chords", "melody", "bass"]
with_drums = false

[export]
format = "wav"
sample_rate = 44100
bit_depth = 16
duration = 25
fade_ms = 50

[random]
seed = null
```

---

## 4. 错误码与排查

| 退出码 | 含义 | 排查 |
|---|---|---|
| 0 | 成功 | — |
| 1 | 参数/通用错误 | 检查和弦符号、参数组合、`ai` 时长越界 |
| 2 | 配置错误 | `config show` 查看生效配置；检查 SoundFont 路径 |
| 3 | 输入文件错误 | 确认 .mid/.wav 存在且可解析 |
| 4 | 渲染失败 | 安装 fluidsynth（README 指引）并配置路径 |
| 5 | 导出失败 | 时长需在 10–30s；mp3 需 `pip install lameenc` |
| 6 | AI 不可用 | 安装 `requirements/ai.txt`（含 CUDA torch）；DiffRhythm 还需 espeak-ng + 仓库 + ≥8GB 显存 |
| 7 | 渲染环境不完整 | 确认 `module/fluidsynth` 与 SoundFont 存在/可加载；或 `--soundfont`/`--fluidsynth` 覆盖 |
| 8 | 批量部分失败 | 查看失败项日志，成功项不受影响 |
| 9 | 批量全部失败 | 查看错误日志，检查参数与环境 |
