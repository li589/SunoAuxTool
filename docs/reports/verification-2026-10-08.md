# SunoAuxTool 全流程验证报告（v1.5.5 → v1.5.6）

日期：2026-10-08 · 演示曲：《雨巷备忘录》a 小调 / 96 BPM / Am-F-C-G / 16 小节 / seed 2026
产物目录：`output/fulldemo/`

## 一、验证矩阵（20+ 模块全实跑）

| # | 链路 | 模块/命令 | 结果 | 关键产物 / 结论 |
|---|------|-----------|------|-----------------|
| 1 | 环境 | `doctor` | ✅ | fluidsynth/SF2/ffmpeg/AI 依赖全绿 |
| 2 | 环境 | `generate midi` | ✅ | 4 轨 40s；voice-leading 告警 10 处（设计行为） |
| 3 | 创作 | `generate melody --variations 2` | ✅ | 3 轨旋律变奏（rhythm/ornament） |
| 4 | 创作 | `score --format all` | ✅ | 五线谱 SVG/PNG + 简谱 SVG/PNG/TXT + MusicXML |
| 5 | 音频 | `render`（真 FluidSynth） | ✅ | 42.5s WAV |
| 6 | 音频 | `tempo` | ✅ | **96.0 BPM 精确命中真值**（conf 0.86，第一拍 0.63s） |
| 7 | 音频 | `transcribe`（内置后端） | ✅ | 回转谱 191 音 / 音域 36-89 |
| 8 | 音频 | `transcribe --backend basic-pitch` | ✅ | ONNX 复调转谱 6s 出 MIDI |
| 9 | 音频 | `analyze` | ✅ | 和弦序列 93 段 + 结构分段 4 段 |
| 10 | 音频 | `export suno --duration 30` | ✅ | 30s/44.1kHz/16bit 合规片段 |
| 11 | 音频 | `inspire add/list/show` | ✅（修复后） | 入库 id=1 ★5；**发现 bug→已修** |
| 12 | 后期 | `post dsp`（gate/lowcut/norm/limiter/fade） | ✅ | 母带版含 F4 新算子 gate |
| 13 | 后期 | `diff`（raw vs master） | ✅ | RMS +10dB、lowcut 频段迁移可见 |
| 14 | 后期 | `post video render --style score --tempo-grid` | ✅ | 1080×1350 谱面视频 + 节拍尺 |
| 15 | 后期 | `post video render --style waveform` | ✅ | 1080×1920 douyin 竖版 |
| 16 | 后期 | `post video multi` | ✅ | 4 平台 7.3s 全出片 |
| 17 | 后期 | `video-preview` | ✅ | 6 缩略帧 + scrub HTML |
| 18 | 取回 | `downloadhelper probe` | ✅ | χ²=376717 判明文 fMP4（合成 AAC 样本） |
| 19 | 取回 | `downloadhelper decode --fmt mp3` | ✅ | MP3 有效（ffprobe 验证） |
| 20 | 取回 | `downloadhelper batch` | ✅* | AAC 载荷下 opus remux 失败属预期（真实缓存为 Opus） |
| 21 | 批量 | `batch --render --export --report` | ✅ | 3 变体全链 + JSON 报告 |
| 22 | 管线 | `pipeline --score --eq --compressor` | ✅ | 一键出 MP3+谱面+内嵌预览页 |
| 23 | AI | `post enhance`（AudioSR） | ✅ | 48kHz 超分 15s（分块+交叉淡化） |
| 24 | AI | `ai musicgen`（MusicGen-medium） | ✅（修复后） | **GPU 真生成 15s 伴奏**（32kHz） |
| 25 | AI | `ai diffrhythm` | ✅ | **真生成 95s 歌曲草稿**（module 源码模式） |

## 二、验证发现并修复（→ v1.5.6，commit 7a92cbd）

1. **inspire 懒建表**：全新数据库 `add/list` 报 `no such table: inspirations`
   （只有显式 `inspire init` 建表）。DDL 移入 `_conn()` 幂等懒初始化。+2 测试。
2. **musicgen 深依赖收口**：audiocraft 顶层可导入但内部 `import triton` 失败 →
   意外错误(9)。现收口 AiDependencyError(6) + Windows 修复提示。+1 测试。
3. **环境修复**：安装 `triton-windows==3.1.0`（匹配 torch 2.5.1；3.8 与
   audiocraft JIT API 不兼容）→ MusicGen 在 Windows GPU 实跑成功。

## 三、PyPI Trusted Publisher 登记（用户侧一次性操作）

Publish to PyPI 失败根因即此项未登记。步骤：

1. 登录 https://pypi.org （账号需为 `li589` 对应的 PyPI 账号，无则注册）
2. 进入 Account settings → **Publishing**（https://pypi.org/manage/account/publishing/）
3. 在 "Add a new pending publisher" 填：
   - **PyPI project name**：`sunoauxtool`（首次发布用 pending publisher 方式）
   - **Owner**：`li589` · **Repository**：`SunoAuxTool`
   - **Workflow name**：`publish.yml` · **Environment name**：`pypi`
4. 提交后重新触发发布：`gh run rerun 37741032013`（v1.5.5）或
   `gh run rerun <v1.5.6 publish run id>`；或推一个新 tag
5. 首次发布成功后，后续 tag 自动走 OIDC，无需 token

## 四、AI 链验证对 1.6.0 的意义

1.6.0 计划随版本发布 AI 功能——本次验证证明四大 AI 模块（MusicGen /
DiffRhythm / AudioSR / basic-pitch）在本机均已具备真实运行条件，
1.6.0 剩余工作主要是：模型懒加载体验（首次 12min 加载）、显存档位提示、
diffrhythm 依赖探测（doctor 显示未装但源码模式可用，口径需对齐）。
