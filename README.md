# SunoAuxTool

**Suno / 海绵音乐 / 网易天音等 AI 音乐工具的前期 + 后期处理工具箱**（原 SmartNoteGen，
v1.0.0 起统一为单一发行版 `sunoauxtool`）。

- **前期**：旋律制作、程序化多轨 MIDI 生成、谱面产出（五线谱/简谱/MusicXML）、MIDI↔WAV 互转
- **后期**：音频下载/转码（猫抓取证 + API adapter 留位，DownloadHelper）、DSP（规划中）、
  音乐视频产出（多轨混音 + 7 种视觉含滚动谱面）、音质提升/分离修复（AudioSR 接入，R5）

> 技术栈：Python 3.12+ · Typer · music21 · pretty_midi · FluidSynth · numpy/soundfile · Pillow · PyTorch（AI 可选）
> P1 可选：MusicGen / DiffRhythm（AI 扩编曲与歌曲草稿，默认不安装）

> 模块布局（单包多子模块）：`sunoauxtool`（核心：生成/谱面/分析/导出）·
> `sunoauxtool.video`（原 videomaker）· `sunoauxtool.download`（DownloadHelper，
> 原 Suno-Cat-Catch-Resolve）。旧包名 `smartnotegen` / `videomaker` 兼容 shim
> 已于 **1.4.5 移除**，请使用新包名。
> **完整功能清单（命令树、模块能力、错误码总表）见 [docs/features.md](docs/features.md)**。

---

## 安装

### 1. 准备 venv 并安装依赖

```bash
cd SunoAuxTool
python -m venv venv
venv\Scripts\activate
pip install -r requirements/base.txt      # P0 运行依赖（不含 torch）
pip install -r requirements/dev.txt       # 开发依赖（pytest 等）
pip install -e .                          # 安装 sunoauxtool / sunoaux / downloadhelper 命令
```

### 2. Windows 外部程序（渲染必需）

| 程序 | 安装方式 |
|---|---|
| **FluidSynth** | 下载 fluidsynth 2.x **win64 二进制**（GitHub releases），将 `bin/` 加入 PATH，或在配置 `[paths] fluidsynth` 指定绝对路径 |
| **SoundFont** | 下载 GeneralUser GS v1.471（约 30MB）或 FluidR3，放入 `assets/soundfonts/`，在配置 `[paths] soundfont` 指定路径 |

> 不装 FluidSynth/SoundFont 时，`generate midi` / `generate melody` 仍可用；`render` / `pipeline` 会以退出码 4 提示安装指引。

### 3. MP3 导出（可选）

```bash
pip install lameenc
```

---

## 快速开始（零参数 demo）

```bash
sunoauxtool pipeline
# → 读默认配置（C major / 120bpm / 8 小节 / C-G-Am-F / 3 轨）
# → 生成 MIDI → FluidSynth 渲染 WAV（44.1kHz/16bit）→ 裁剪至 25s + 淡入淡出
# → output/YYYYMMDD/pop_Cmajor_120_8bars_demo_*_suno25s.wav
# → 打印：文件路径 + 元数据（时长/采样率/位深/和弦进行/seed）
```

也可以用聚合入口 `sunoaux`（v1.0.0 起）：`pre` = 前期创作，`post` = 后期处理
（详见 `sunoaux --help`）：

```bash
sunoaux pre midi --chords C-G-Am-F      # = sunoauxtool generate midi
sunoaux post convert cache/song.mp3     # = downloadhelper decode（fMP4 转码）
sunoaux post video render song.wav      # = sunoauxtool.video 渲染（音乐视频）
```

---

## 子命令一览

| 命令 | 用途 | 示例 |
|---|---|---|
| `generate midi` | 程序化多轨 MIDI（和弦/旋律/贝斯 [+鼓]） | `sunoauxtool generate midi --chords C-G-Am-F --bpm 120 --seed 42` |
| `generate melody` | music21 乐理旋律 + 变奏 | `sunoauxtool generate melody --key "C major" --chords C-G-Am-F --variations 3` |
| `render` | MIDI → WAV 渲染 | `sunoauxtool render --input xxx.mid` |
| `score` | MIDI → 谱面（五线谱 SVG/PNG、简谱、MusicXML） | `sunoauxtool score song.mid --format all` |
| `tempo` | 音频测速（ACF + 节奏先验，消倍频歧义） | `sunoauxtool tempo song.wav` |
| `transcribe` | WAV → MIDI 转谱（内置单旋律；复调用 basic-pitch） | `sunoauxtool transcribe song.wav -o out.mid` |
| `analyze` | 调性 / 和弦进行 / 段落结构估计（numpy-only 启发式） | `sunoauxtool analyze song.wav --key --chords --structure` |
| `video-preview` | 视频缩略帧 + 时间轴 scrub 预览页 | `sunoauxtool video-preview out.mp4 --frames 6` |
| `export suno` | Suno 合规导出（10–30s WAV/MP3） | `sunoauxtool export suno --input xxx.wav --duration 25` |
| `pipeline` | 一键闭环 generate→render→export | `sunoauxtool pipeline`（零参数 demo） |
| `config init` | 生成配置文件模板 | `sunoauxtool config init` |
| `config show` | 打印合并后的生效配置 | `sunoauxtool config show` |
| `batch` | 批量生成多个变体（随机化 + 可复现 + 失败隔离） | `sunoauxtool batch --count 5 --seed 42` |
| `ai musicgen` | MusicGen 扩编曲（旋律 → 伴奏） | `sunoauxtool ai musicgen --input m.wav --prompt "upbeat pop"` |
| `ai diffrhythm` | DiffRhythm 歌曲草稿（风格提示 → 带人声歌曲） | `sunoauxtool ai diffrhythm --prompt "slow ballad"` |
| `export suno-pack` | 批量导出 Suno 片段打包（目录 + zip + 清单） | `sunoauxtool export suno-pack *.wav --name my_pack` |
| `export suno-manifest` | 生成 Suno 上传清单（CSV/JSON） | `sunoauxtool export suno-manifest *.wav -o upload.csv` |
| `play` | 系统播放器播放 WAV | `sunoauxtool play output/xxx.wav` |
| `doctor` | 一键环境健康检查 | `sunoauxtool doctor` |
| `diff` | 对比两个 WAV 的音频特征 | `sunoauxtool diff a.wav b.wav` |
| `new` | 交互式引导生成新音乐 | `sunoauxtool new` |
| `inspire` | 灵感库管理（SQLite 存储） | `sunoauxtool inspire init` |
| `errors` | 打印错误码表 | `sunoauxtool errors` |

后期处理三件套（`enhance` / `dsp` / `fetch`）挂在聚合入口的 `post` 组下：
`sunoaux post enhance song.wav`、`sunoaux post dsp song.wav --ops "reverb 0.3"`、
`sunoaux post fetch "歌名" --source catcatch --dry-run`。

完整参数说明见 [docs/usage.md](docs/usage.md)；乐谱生成（五线谱 / 简谱 / MusicXML）见
[docs/score.md](docs/score.md)。

---

## 音乐视频（sunoauxtool.video，原 videomaker）

> 把 SunoAuxTool 产出的音频一键合成**可发布的音乐视频 / 音频可视化**，
> 适配抖音/YouTube/Instagram/官网四大平台。非 AI，已并入主包。

**输入格式**：WAV / MP3 / FLAC / OGG / **MIDI**（MIDI 自动用 FluidSynth 渲染）
**能力**：多轨混音（增益/声像/归一化）、分轨可视化、滚动谱面（五线谱/简谱）、8 种视觉效果、多平台批量

```bash
sunoaux post video render ... 命令随 `pip install -e .` 一并安装

# 抖音竖屏波形
sunoaux post video render output/.../x.wav --preset douyin --style waveform

# MIDI 直接出视频（自动渲染）
sunoaux post video render song.mid -p youtube -s circular_spectrum

# 多轨混音 + 分轨可视化（WAV/MP3/MIDI 可混用，同产混音 WAV）
sunoaux post video render drums.wav "bass.mid:gain=0.8:pan=-0.3" melody.mp3 \
    -p douyin -s tracks --title "三轨混音"

# 一次产出全部平台 + Logo 水印 + 标题
sunoaux post video multi x.wav --presets douyin,youtube,instagram,official \
    --style circular_spectrum --logo logo.png

# 8 种视觉效果：waveform / spectrum / circular_spectrum / reactive / tracks / waveform_scroll / score / bars
# score 样式：滚动谱面（播放头居中、当前音高亮），--tempo-grid 叠加节拍网格 + BPM 标注
videomaker render song.mid -p douyin --style score --tempo-grid --notation jianpu
```

详见 [docs/videomaker.md](docs/videomaker.md)。

---

## Suno-Cat-Catch-Resolve 子项目（Suno 猫抓产物解码）

> 把「猫抓」(CatCatcher) 从 Suno 抓下来的**打不开的文件**转成可播放音频：
> 自动识别哪份是明文、哪份是加密密文，并把明文解码为 Opus / MP3。
> 纯本地处理，独立包，依赖 ffmpeg。

**为什么需要它**：猫抓会给出两份**同一首曲子**的文件——

| 抓取方式 | 常见文件名 | 真实身份 | 可解码 |
|---|---|---|---|
| 缓存捕获 | `Suno _ AI Music.mp3` | **fragmented MP4**（Opus 48kHz 立体声），扩展名错标 | ✅ 直接解码 |
| 直接下载页面媒体 | `<uuid>.m4a` | 服务端下发的**加密密文** | ❌ 无密钥不可破 |

`<uuid>.m4a` 经取证（卡方 χ²≈215 完美均匀 + 周期扫描无异于随机基线）判定为
**AES / ChaCha20 级强加密**，这是数学结论而非工具限制——所以包不会对它假装成功，
而是明确报错并提示改用缓存捕获的那份（它已是完整明文副本）。

```bash
downloadhelper 命令随 `pip install -e .` 一并安装
export SUNO_FFMPEG=/d/tools/ffmpeg/bin/ffmpeg.exe   # ffmpeg 不在 PATH 时：指文件或指目录

downloadhelper probe  "Suno _ AI Music.mp3"      # 取证判定
downloadhelper decode "Suno _ AI Music.mp3" -o ./out
downloadhelper batch  ./downloads -o ./out       # 批量，密文自动跳过
```

> **命名**：三包合一后本子包为 **`sunoauxtool.download`**（CLI 入口 **`downloadhelper`**）。
> 旧目录名 `Suno-Cat-Catch-Resolve`、旧入口 `suno-cat-catch-resolve` 与旧 import 名
> `suno_cat_catch_resolve` 均已移除，无兼容 shim——请改用上面的 `downloadhelper` 命令
> 与 `import sunoauxtool.download`（`smartnotegen` / `videomaker` shim 亦已于 1.4.5 移除）。

详见 [docs/downloadhelper.md](docs/downloadhelper.md)（三包合一后子包位于
`src/sunoauxtool/download/`，原 `src/Suno-Cat-Catch-Resolve/` 目录已移除）。

---

## 配置

配置合并优先级（低 → 高）：**内置默认值 < `config/default.toml` < 用户配置文件 < CLI 参数**。

```bash
sunoauxtool config init            # 生成 sunoauxtool.toml（可修改 SoundFont 路径）
sunoauxtool config show            # 查看生效配置
sunoauxtool --config my.toml generate midi   # 指定配置文件
```

用户配置文件默认查找项目根 `sunoauxtool.toml`，也可用 `--config` 显式指定。

---

## 错误码

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 参数错误/通用错误 |
| 2 | 配置错误（配置文件缺失/非法、SoundFont 路径无效） |
| 3 | 输入文件错误（.mid/.wav 不存在或无法解析） |
| 4 | 渲染失败（fluidsynth 未安装/找不到/进程失败） |
| 5 | 导出失败（时长越界 10–30s、MP3 编码器缺失） |
| 6 | AI 模块不可用（依赖未装 / 显存不足 / DiffRhythm NO-GO） |
| 7 | 渲染环境不完整（module 缺失/损坏、SF2 不可加载） |
| 8 | 批量部分失败 |
| 9 | 批量全部失败 |
| 10 | 视频 ffmpeg 不可用（PATH / `--ffmpeg-path` 均未命中） |
| 11 | 视频分辨率/比例不支持 |
| 12 | 视频输入音频缺失/无法读取 |
| 13 | 视频渲染失败（ffmpeg 进程非零退出） |
| 14 | 视频输出路径不可写 |
| 15 | DSP 处理失败（算子运行期错误） |
| 16 | DSP 参数错误（ops 串解析失败、算子参数非法） |
| 20 | 下载 ffmpeg 不可用（`SUNO_FFMPEG` / `SUNO_FFMPEG_DIRS` / PATH / `--ffmpeg-path`） |
| 21 | 非 fragmented MP4（输入不是可解析的 fMP4） |
| 22 | 输入为加密密文（取证判定强加密，无密钥不可解码） |
| 23 | 下载转码失败（ffmpeg 非零退出） |
| 24 | 下载输出路径不可写 |
| 25 | 下载源凭证缺失（API 下载源未配置凭证） |
| 26 | 下载源请求失败（网络请求/响应解析失败） |

---

## P1 AI 集成（可选）

> AI 依赖默认不安装（P0/P1-非AI 环境零 torch import）。安装后使用：

```bash
# 1. CUDA 版 torch + torchaudio（RTX 4060 / CUDA 12.1；务必用 cu121 索引避免装回 CPU 版）
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121

# 2. 其余 AI 依赖（audiocraft / DiffRhythm 运行库）
pip install -r requirements/ai.txt

# 3. DiffRhythm 仓库（官方不可 pip 安装，需手动克隆到 module/diffrhythm 或设置 DIFFRHYTHM_DIR）
git clone https://github.com/ASLP-lab/DiffRhythm.git module/diffrhythm

# 4. Windows：DiffRhythm 人声合成依赖 espeak-ng（下载 .msi 安装并加入 PATH）
#    https://github.com/espeak-ng/espeak-ng/releases

# 5. 权重下载较慢时使用国内镜像（首次需下载数 GB 权重）
set HF_ENDPOINT=https://hf-mirror.com
```

**用法示例：**
```bash
# MusicGen：以旋律 WAV 为条件扩编曲（medium fp16 默认；显存不足可 --model-size small）
sunoauxtool ai musicgen --input melody.wav --prompt "upbeat pop" --output acc.wav --duration 20 --seed 42

# DiffRhythm：风格提示 → ≥60s 带人声歌曲草稿（chunked=True 默认；草稿不进 Suno 导出链）
sunoauxtool ai diffrhythm --prompt "slow ballad" --lyrics "第一句词" --duration 95
```

详见 [docs/ai-integration.md](docs/ai-integration.md)。

---

## 开发

```bash
pytest            # 运行全部测试（渲染用例使用 mock，无需真实 fluidsynth）
ruff check src    # lint
```
