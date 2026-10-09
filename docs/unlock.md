# unlock — 通用加密音乐解密（1.6.4）

多平台加密音频 → 原始音频。纯标准库、零第三方依赖、纯离线、零外部文件。是 `download/` 域的第三个解密/取回组件（前两个：
fMP4 转码、NCM 解包）。

## 支持格式

| 平台 | 扩展名 | 方式 | 外部依赖 |
|---|---|---|---|
| 网易云 | `.ncm` | AES-128-ECB + 定制 RC4（复用 `ncm` 包） | 无 |
| 酷我 | `.kwm` | u64 种子 + 32 字节循环密钥异或 | 无 |
| 酷狗 | `.kgm` `.kge` `.kgma` `.vpr` | 私钥 + 272 字节修正表 + maskV1 掩码流（内置小表现算，等价 73MB 公钥） | 无 |
| QQ 音乐（V1 静态） | `.tkm` `.bkc*` 及十六进制扩展名 | 128 字节静态密钥流异或 | 无 |
| QQ 音乐（V2 EKey） | `.mflac` `.mgg*` `.mmp4` `.qmc0/2/3/4/6/8` `.qmcflac` `.qmcogg` | 内嵌 EKey → tc_tea 解密 → Map/RC4 流密码 | 无 |

不支持 / 部分支持：
- QQ 音乐 **STag / PcV2MusicEx** footer 的文件不内嵌 EKey，需在线取密钥
  （报错码 29 并给出媒体标识提示）。

## CLI

```bash
# 单个/多个文件解密（自动按扩展名 + 魔数分发，ncm 也走此入口）
downloadhelper unlock "周杰伦 - 晴天.qmcflac" -o output/unlock
downloadhelper unlock a.kwm b.ncm c.kgm -o output/unlock

# 批量扫描目录时顺带解密
downloadhelper batch <缓存目录> -o output/unlock --include-unlock

# 聚合入口等价
sunoaux post unlock <files> -o output/unlock
```

输出命名：`<源文件名主干>.<嗅探到的音频扩展名>`（flac/mp3/ogg/wav/ape…）。
同名冲突自动追加 ` (1)` 序号，`--overwrite` 强制覆盖。

退出码：27=无法识别的加密格式；28=解密失败/结果不可识别；29=缺少外部
密钥（仅在线 EKey 场景）。

## 酷狗公钥（可选加速器）

1.6.4 起 KGM/KGMA/VPR **零依赖**：maskV1 掩码流由内置 272×2 小表递归
算出，经 265 个采样点与官方 73MB 公钥逐字节比对零差异，无需任何外部
文件。若手头有官方公钥，可作为批量场景的加速器提供（跳过逐块递归）：

1. 环境变量 `SUNO_KGM_KEY` 指向文件路径（`.xz` 或解压后的 `.bin` 均可）；
2. 放到默认缓存路径 `~/.cache/sunoauxtool/kugou_key.xz`（或 `.bin`）。

长度不符或找不到时**自动回退**到内置 maskV1，不会报错。

下载来源（可选）：[ghtz08/kugou-kgm-decoder](https://github.com/ghtz08/kugou-kgm-decoder)
仓库 `assets/kugou_key.xz`。

## Python API

```python
from sunoauxtool.download.unlock import unlock_file, unlock_bytes, detect_format

out_path = unlock_file("song.qmcflac", "output/unlock")     # -> Path
audio, ext = unlock_bytes(data, "kwm")                       # -> (bytes, "flac")
fmt = detect_format("kgm", header_bytes)                     # -> "kgm" | None
```

## 算法与正确性

- 全部算法移植自 unlock-music 官方实现（MIT），测试向量与官方 Rust
  单元测试同源：V1 transform 及 0x7FFF 边界、key_compress、QMC2Map、
  qmc2_hash、get_segment_key、改型 RC4、QMC2RC4 256 字节、tc_tea；
- KGM 272 字节修正表与公式对齐 ghtz08/kugou-kgm-decoder（Rust），并
  用其官方加密/解密文件对做实弹校验（本地存在时自动启用）；
- KGMA（酷狗新一代，无固定魔数，靠扩展名 + 头部合理性探测）与 KGM
  同一套公式；maskV1 掩码流移植自 onavcn/kugou-audio-unlock 并与官方
  公钥逐点比对（265 点零差异）；
- KWM 常量（魔数 `yeelion-kuwo*`、固定异或串）逆向自 unlock-music web
  bundle，并经本地 `examples/` 与 worthsee 解密脚本双重交叉校验；
- KWM/QMC 的加密均覆盖整文件（含原始标签），解密结果即完整原始音频，
  **无需**再嵌标签。

## 致谢

- [unlock-music](https://git.unlock-music.dev/um/web)（算法与向量）
- [ghtz08/kugou-kgm-decoder](https://github.com/ghtz08/kugou-kgm-decoder)（KGM 算法与公钥）
- [onavcn/kugou-audio-unlock](https://github.com/onavcn/kugou-audio-unlock)（maskV1 小表来源）
- [HowenXu/qmc-decrypt](https://github.com/HowenXu/qmc-decrypt)（交叉验证参考）
- [worthsee KWM 解密](https://kwm.worthsee.com/)（KWM 交叉验证参考）

仅用于解锁个人已购买/已下载内容做本地备份，请尊重版权。
