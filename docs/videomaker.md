# 视频产出（sunoauxtool.video，原 videomaker）使用指南

> SunoAuxTool 音乐视频 / 音频可视化生成器 v0.3.0
> 把音频（**WAV / MP3 / FLAC / OGG / MIDI**）变成可发布的音乐视频，
> 支持**多轨混音**与**分轨可视化**，适配抖音/YouTube/Instagram/官网四大平台。

---

## 安装

```bash
# 前置：已安装 SunoAuxTool（同 venv）
随主包安装：`pip install -e .`（原 `videomaker` 入口已于 1.4.5 移除，统一用 `sunoaux post video`）

# 外部依赖
ffmpeg --version   # 需在 PATH，或配置 [paths] ffmpeg 指定绝对路径
```

## 快速开始

```bash
# 抖音竖屏波形视频
sunoaux post video render music.wav --preset douyin --style waveform

# MIDI 直接生成视频（自动 FluidSynth 渲染，用 module/ 下的真实引擎 + SoundFont）
sunoaux post video render song.mid -p youtube -s circular_spectrum

# 多轨混音 + 分轨可视化（WAV/MP3/MIDI 可混用）
sunoaux post video render drums.wav "bass.mid:gain=0.8:pan=-0.3" melody.mp3 \
    -p douyin -s tracks --title "分轨混音"

# 一次产出全部 4 个平台
sunoaux post video multi music.wav --presets douyin,youtube,instagram,official --style circular_spectrum

# 带标题 + Logo 水印
sunoaux post video render music.wav -p youtube -s spectrum --title "我的曲子" --logo logo.png
```

---

## 命令一览

| 命令 | 用途 | 示例 |
|---|---|---|
| `render` | 视频渲染（多文件=多轨混音） | `sunoaux post video render a.wav -p douyin -s waveform` |
| `multi` | 多平台批量渲染 | `sunoaux post video multi a.wav --presets douyin,youtube` |
| `presets` | 列出平台预设 | `sunoaux post video presets` / `sunoaux post video presets -n douyin` |
| `config` | 配置管理 | `sunoaux post video config init` / `sunoaux post video config show` |
| `--version` | 版本 | `sunoaux post video version` |

### render 参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--preset, -p` | 平台预设（douyin/youtube/instagram/official） | douyin |
| `--style, -s` | 视觉效果（见下表） | waveform |
| `--title, -t` | 标题（前 3s 淡入淡出） | 无 |
| `--subtitle` | 副标题 | 无 |
| `--output, -o` | 输出路径 | output/自动命名 |
| `--width/--height/--fps` | 覆盖预设分辨率 | 预设值 |
| `--score-midi` | score 样式的谱面 MIDI 路径（输入为 .mid 时可省略） | 无 |
| `--tempo-grid` | 用测速 BPM 绘制节拍网格 + BPM 标注（score 样式） | 关 |
| `--notation` | score 样式记谱法（staff=五线谱 / jianpu=简谱） | staff |

### multi 参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--presets` | 逗号分隔预设列表 | 全部 4 个 |
| `--style, -s` | 视觉效果 | waveform |
| `--title, -t` | 标题 | 无 |

---

## 平台预设

| 预设 | 比例 | 分辨率 | 适用 |
|---|---|---|---|
| `douyin` | 9:16 | 1080×1920 | 抖音/快手/TikTok |
| `youtube` | 16:9 | 1920×1080 | YouTube/B站/官网 |
| `instagram` | 1:1 | 1080×1080 | Instagram/小红书 |
| `official` | 4:5 | 1080×1350 | 公众号/官网嵌入 |

## 视觉效果

| 风格 | 引擎 | 效果 | 速度 |
|---|---|---|---|
| `waveform` | ffmpeg 原生 | 滚动波形 | ⚡ 极快（~1s/10s 音频） |
| `spectrum` | ffmpeg 原生 | 频谱瀑布 | ⚡ 极快 |
| `circular_spectrum` | PIL 创意层 | 圆形放射频谱 + 中心脉动圆 | 🎨 快（rawvideo 管道） |
| `reactive` | PIL 创意层 | 节拍脉冲圆 + 冲击扩散环 | 🎨 快 |
| `tracks` | PIL 创意层 | **分轨频谱**（多轨垂直排列，色彩区分） | 🎨 快 |
| `waveform_scroll` | PIL 创意层 | **滚动波形**（播放头居中，波形流动） | 🎨 快 |
| `score` | PIL 创意层 | **滚动谱面**（五线谱/简谱，播放头居中、当前音高亮） | 🎨 快 |
| `bars` | PIL 创意层 | **频率柱状条** + 峰值保持帽（R15） | 🎨 快 |

双引擎架构：waveform/spectrum 走 ffmpeg 原生滤镜（最快）；创意风格走 PIL 逐帧渲染 + rawvideo 管道直写 ffmpeg（v0.2.1 提速 26 倍：21s → 0.8s/3s 音频）。

---

## 多格式输入（v0.3）

| 格式 | 处理 | 备注 |
|---|---|---|
| WAV | soundfile 直读 | 最常用 |
| MP3 | soundfile 0.14 原生 | 无需转换 |
| FLAC / OGG | soundfile 直读 | 无损/开源格式 |
| **MIDI** | 自动 FluidSynth 渲染为 WAV | 用 `module/` 下真实引擎 + SoundFont |

MIDI 渲染资源（默认路径，可显式覆盖）：
- FluidSynth：`module/fluidsynth/bin/fluidsynth.exe`
- SoundFont：`module/GeneralUser_GS/GeneralUser-GS/GeneralUser-GS.sf2`
- 备用音色库：`module/GeneralUser_GS/ColomboGMGS2_SF2/ColomboGMGS2.sf2`

```bash
# 单个 MIDI → 视频（自动渲染，19s MIDI 约 20s 出片）
sunoaux post video render song.mid -p youtube -s circular_spectrum
```

## 多轨混音（v0.3）

`render` 传入**多个文件**即自动进入多轨模式：

```bash
sunoaux post video render drums.wav "bass.mid:gain=0.8:pan=-0.3" "melody.mp3:gain=1.2:pan=0.4" \
    -p douyin -s tracks --title "三轨混音"
```

每轨参数（冒号分隔：

| 参数 | 含义 | 取值 |
|---|---|---|
| `gain` | 轨道增益 | 0.0 ~ 2.0（>0） |
| `pan` | 声像 | -1(全左) ~ +1(全右)，0 居中 |

混音处理流程：
1. 逐轨加载（格式混用 OK，MIDI 自动渲染）
2. 重采样统一到 44.1kHz
3. 长度对齐（默认补零到**最长轨**）
4. 等功率声像定律 + 轨道增益
5. 峰值归一化到 -1 dBFS（防削波）

产物：
- 视频：`xxx.mp4`
- **混音 WAV**：`xxx.mix.wav`（同目录，可直接用于 SoundCloud/播客等纯音频发布）

> ⚠️ Windows 绝对路径含盘符（如 `C:\music\a.wav`）不支持冒号参数语法
> （会被解析切分）；这种场景请用相对路径，或程序化调用 `TrackSpec(path=...)`。

## 分轨可视化（v0.3）

`--style tracks` 时，画面垂直排列每轨频谱：
- 每轨独立颜色（HSL 色环均分，基于主色派生）
- 每轨独立归一化（保证弱轨也可见）
- 顶部显示轨名（默认文件名）

配合混音 WAV，即可实现"视频 + 纯音频"双产物一次生成。

---

## 滚动谱面（v0.4）

`--style score` 由 `sunoauxtool.score` 的谱面数据驱动，逐帧渲染横向滚动谱面：
- **播放头固定居中**，谱面随时间从右向左流动；正响的音高亮（主色+白描边）
- 多轨垂直排列，五线谱谱号按轨道自动判定；宽音域自动收缩线距，不纵向溢出
- `--notation staff`（默认）：五线谱（符头 + 加线 + 小节线）
- `--notation jianpu`：简谱数字（1-7，变音加 `#`，八度加点；同时值和弦纵向堆叠）
- `--tempo-grid`：用 `sunoauxtool analysis.tempo` 测速，底部画节拍尺
  （小节首拍加粗）并在头部标注实测 BPM；滚动时间轴同步采用测速 BPM

```bash
# 输入本身是 MIDI：谱面数据自动取自输入
sunoaux post video render song.mid -p douyin --style score --tempo-grid

# 输入是渲染后的 WAV：用 --score-midi 指向对应 MIDI（否则报错）
sunoaux post video render song.wav -p youtube --style score --score-midi song.mid --notation jianpu
```

> 说明：谱面为可视化简化谱（不带符干/连杠/调号聚合），深度排版请用
> `sunoauxtool score` 命令导出 SVG/PNG/MusicXML（见 docs/score.md）。

---

## 配置文件

`sunoaux post video config init` 生成 `videomaker.toml`：

```toml
[paths]
ffmpeg = "ffmpeg"          # 或绝对路径
output_dir = "output"

[background]               # 背景
type = "gradient"          # solid | gradient | image
gradient_start = "#1a1a2e"
gradient_end = "#16213e"

[logo]                     # 水印（v0.2.1 新增）
path = ""                  # PNG 路径（空=不叠加）
position = "bottom-right"  # 四角可选
scale = 0.12               # 宽度占视频宽比例
opacity = 1.0              # 透明度

[visual]
style = "waveform"
color = "#4a9eff"          # 可视化主色
```

优先级：内置默认 < videomaker.toml < CLI 参数。

---

## 输出

- 目录：`output/<project>/<日期>/video/{style}_{bpm}_{seed}_{seq}.mp4`
- 每次运行写 `metadata.json`（preset/style/logo/时长/分辨率）

## 错误码

| 码 | 含义 |
|---|---|
| 10 | ffmpeg 未找到 |
| 11 | 分辨率/比例不支持 |
| 12 | 音频读取失败 |
| 13 | 渲染失败（ffmpeg 非零退出） |
| 14 | 输出路径不可写 |

## 故障排除

| 现象 | 处理 |
|---|---|
| ffmpeg 未找到 | 加入 PATH 或 `[paths] ffmpeg = "绝对路径"` |
| 中文乱码 | 默认微软雅黑；确认 `C:\Windows\Fonts\msyh.ttc` 存在，或 `[text] font` 指定 |
| 渲染慢 | PIL 路径已用 rawvideo 管道；仍慢可降 `render_scale`（代码层） |
| 奇数尺寸报错 | v0.2.1 已强制 logo 偶数尺寸；自定义背景请用偶数分辨率 |
