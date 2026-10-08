# 转码与 NCM 解包组件方案（1.7.0 → 1.8.0 → 1.8.1）

日期：2026-10-08 · 状态：**待确认后实施**
原则：全部落在 `sunoauxtool.download`（品牌 DownloadHelper）组件域内，复用既有
ffmpeg 定位/取证/批量基建；`examples/` 参考项目**零代码依赖**，仅 README 友情链接。

## 一、定位

DownloadHelper 从「Suno 猫抓取回工具」升级为**媒体转换中枢**：

```
输入源                     转换核心                      输出
─────────────────────────────────────────────────────────────
猫抓缓存 fMP4 ──┐
Suno 取回源   ──┤   transcoder（已有）    ┌→ Opus/MP3（已有）
.ncm 网易云   ──┼→  convert（新增 1.7.0） │→ mp3/wav/m4a/flac 互转
视频 mp4/mov… ──┤   ncm（新增 1.8.0）     │→ 视频分离音频
任意音频      ──┘   forensics（已有）     └→ NCM 还原原始音频
```

## 二、目录与模块设计

```
src/sunoauxtool/download/
├── sources/            # 取回源（已有 catcatch；不动）
├── forensics.py        # 取证判定（已有）
├── transcoder.py       # fMP4 解码（已有）
├── convert/            # ★ 1.7.0 新增
│   ├── __init__.py     # convert_audio() / extract_audio() 门面
│   ├── engine.py       # ffmpeg 统一引擎：定位（复用 SUNO_FFMPEG 层）、
│   │                   #   stderr 线程排空、错误映射 TranscodeError(码 20-24)
│   ├── profiles.py     # 输出预设：suno（44.1k/16bit/wav）、lossless（flac），
│   │                   #   web（mp3 192k）、可覆盖 bitrate/sample_rate/bit_depth
│   ├── audio.py        # mp3/wav/m4a/flac 互转（码率/采样率/位深参数映射）
│   └── video_extract.py# mp4/mov/flv/webm/mkv → 音频：优先 -c:a copy 直通
│                       #   （流已是目标格式时零损），回退重编码
├── ncm/                # ★ 1.8.0 新增（纯标准库，零第三方依赖）
│   ├── __init__.py     # unpack_ncm() 门面
│   ├── aes_ecb.py      # 手写 AES-128-ECB + PKCS#7（约 200 行）
│   ├── stream.py       # 网易云定制 RC4 变体：KSA keybox + 按绝对偏移取
│   │                   #   密钥流（状态无关→可分块并行），预计算表 + 大整数 XOR
│   ├── unpack.py       # 容器解析：CTENFDAM 魔数 → 密钥段(XOR 0x64→AES→
│   │                   #   去 neteasecloudmusic 前缀) → 元数据段(XOR 0x63→
│   │                   #   163 key 前缀→base64→AES→music: JSON) → 封面 → 载荷
│   ├── meta.py         # JSON 元数据 → ID3(mp3)/Vorbis(flac) 标签 + 封面写入
│   └── errors.py       # NotNcmError / CorruptNcmError（退出码 25-26）
└── cli.py              # 统一入口（见下）
```

## 三、CLI 接口

```
downloadhelper convert <input> [-o out] [--fmt mp3|wav|m4a|flac]
                       [--bitrate 192k] [--sample-rate 44100] [--bit-depth 16|24]
                       [--profile suno|lossless|web]
downloadhelper extract-audio <video> [-o out] [--fmt mp3|wav|m4a|flac] [--copy-first]
downloadhelper ncm <file.ncm> [-o out] [--tags/--no-tags] [--cover/--no-cover]
downloadhelper batch <dir> [--include-ncm --include-convert]  # 扩展批量扫描
sunoaux post convert / extract-audio / ncm   # 聚合入口镜像（薄转发二次注册）
```

错误码沿用 20-24 并新增 25(ncm 非法容器) / 26(ncm 密钥段损坏)。

## 四、分批路线（每批一版）

| 批次 | 版本 | 内容 | 验收 |
|------|------|------|------|
| 一 | **1.7.0** | convert/ 全部（engine/profiles/audio/video_extract）+ CLI + 聚合镜像 + docs/convert.md + README 友情链接 + .gitignore examples/ | 互转矩阵 4×4 往返测试、直通/重编码分支、预设断言 |
| 二 | **1.8.0** | ncm/ 全部（AES/流密码/容器/元数据）+ CLI + 测试 | 合成 .ncm fixture（自加密往返）+ 上游公开测试向量；标签/封面断言 |
| 三 | **1.8.1** | batch 扩展（目录混合扫描：fMP4+ncm+任意音频→统一目标格式+报告）、doctor 增 ffmpeg 编码器探测（libmp3lame/aac/flac）、文档收尾 | 混合目录 e2e、报告 JSON/CSV、doctor 输出 |

## 五、examples/ 整合策略（零依赖声明）

- `examples/` 加入 `.gitignore`，**不提交、不 import、不复制代码**
- README「致谢/相关项目」小节加友情链接：
  - [ncm2mp3](https://github.com/xzrdmm/ncm2mp3)（Python 标准库 NCM 解锁）
  - [ncmdump](https://github.com/taurusxin/ncmdump)（C++ 实现）
  - [ncm2mp3-js](https://github.com/)（Web/ekey 路线）
  - SUNO-Capture（Chrome 扩展：suno.com 页面注入下载按钮）
  - DLBunny（Suno 在线下载站）
- NCM 算法按公开格式规范独立实现（AES S-box 伽罗华域、InvMixColumns 系数
  是参考项目踩过的坑，测试向量防错）

## 六、测试策略

- convert：合成各格式源文件（ffmpeg 生成），4×4 互转参数化；`--profile suno`
  断言 44.1k/16bit；copy 直通 vs 重编码用 ffprobe codec 断言
- ncm：**自加密往返**（用我们的实现加密已知音频→解密→逐字节比对）+ 容器
  边界（魔数错/段截断/密钥段损坏→25/26）；密钥流表用公开向量断言
- CI：ffmpeg 在 ubuntu-24.04 已装，无新增 LFS/依赖负担
