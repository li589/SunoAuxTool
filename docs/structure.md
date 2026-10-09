# 仓库结构说明（1.7.0）

```
SunoAuxTool/
├── src/sunoauxtool/            # 主包（发行名 sunoauxtool）
│   ├── ai/                     # AI 适配器：musicgen / diffrhythm / audiosr / basicpitch
│   ├── analysis/               # 音频分析：tempo / transcribe / ext（numpy-only）
│   ├── commands/               # CLI 辅助（helpers）
│   ├── download/               # 后处理取回域：转码 / NCM / unlock 解密 / 取证
│   │   └── unlock/             #   通用解密：kwm / kgm / qmc + ekey_source
│   ├── dsp/                    # DSP 算子（norm / loudnorm / reverb / …）
│   ├── export/                 # 导出（音频封装 / metadata）
│   ├── generators/             # 旋律 / MIDI 程序化生成
│   ├── models/                 # 数据模型
│   ├── music_theory/           # 乐理（和弦 / 调式 / 对位）
│   ├── render/                 # FluidSynth 渲染
│   ├── score/                  # 谱面子系统（theory→model→layout→drawing→svg/jianpu/png/musicxml）
│   ├── styles/                 # 风格标签
│   ├── video/                  # 音乐视频（原 videomaker）
│   └── 顶层模块                # cli / aggregate / pipeline / batch / config / env / …
├── tests/                      # pytest（[tool.pytest] testpaths=tests，按域分组）
│   ├── ai/                     #   AI 适配器
│   ├── analysis/               #   音频分析 + CLI
│   ├── core/                   #   主 CLI / pipeline / dsp / doctor / 发布守卫等
│   ├── download/               #   后处理域（含 unlock 全套）
│   ├── pre/                    #   创作域（generators / theory / render / inspire）
│   ├── score/                  #   谱面 + 导出 CLI
│   ├── video/                  #   视频域
│   ├── conftest.py             #   根夹具（全树生效）
│   └── test_version_alignment.py  #   发布守卫（版本/CHANGELOG 对齐，刻意置根）
├── module/                     # 运行时资产（8GB 级，不入库/部分 LFS）
│   ├── fluidsynth/  GeneralUser_GS/    # 渲染引擎 + SoundFont
│   ├── diffrhythm/             # DiffRhythm 源码模式（clone + 权重）
│   └── …
├── src/versatile_audio_super_resolution/   # AudioSR 上游克隆（gitignored，源码模式）
├── src/DownloadHelper/         # 本地遗留（gitignored；out/ 为运行产出，勿动）
├── docs/                       # 使用与过程文档（archive/ 存历史规划）
├── examples/                   # 参考站点离线快照（gitignored，研读用）
├── output/                     # 运行产出（gitignored；project/date 布局）
├── requirements/               # base / dev / ai 分层依赖
├── scripts/  patches/  config/ # 构建 / 上游补丁 / 用户配置（sources.toml gitignored）
└── CLI 入口五件套              # sunoaux（聚合）/ sunoauxtool（全量）/ downloadhelper / 旧名兼容
```

## 约定

- **聚合入口镜像关系**：`sunoaux pre *` → `sunoauxtool generate/render/score/transcribe/ai musicgen/ai diffrhythm`；`sunoaux post *` → `downloadhelper *` / videomaker / enhance。
- **测试放哪**：与被测域同名子目录；跨域守卫（版本对齐）留根；共享夹具只进根 conftest。
- **gitignored 运行区**：`output/`、`module/`（SF2 走 LFS 除外）、`src/DownloadHelper/`、`src/versatile_audio_super_resolution/`、`*.db`（灵感库运行态）——目录治理时**只补 ignore，不清理内容**。
