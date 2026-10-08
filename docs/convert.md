# 通用转码中枢（1.6.0）

`convert/` 是 DownloadHelper（`sunoauxtool.download`）的通用转码层：
音频互转 + 视频分离音轨，ffmpeg 统一引擎（复用 `SUNO_FFMPEG` /
`SMARTNOTEGEN_FFMPEG` / `SUNO_FFMPEG_DIRS` 两层环境变量定位，详见
[downloadhelper.md](downloadhelper.md)）。

## 音频互转

支持 mp3 / wav / m4a / flac 任意方向（4×4）：

```bash
downloadhelper convert song.flac --fmt mp3 --bitrate 320k
downloadhelper convert song.wav --fmt m4a --sample-rate 48000
downloadhelper convert song.mp3 --fmt wav --bit-depth 24
sunoaux post convert-audio song.wav --fmt flac     # 聚合入口镜像
```

编码器映射：mp3→libmp3lame、m4a→aac、flac→flac、wav→pcm_s16le/pcm_s24le。
采样率不指定则保持源；wav 位深默认 16。

## 视频分离音轨

支持 mp4 / mov / flv / webm / mkv / avi / m4v：

```bash
downloadhelper extract-audio clip.mp4 --fmt mp3
downloadhelper extract-audio clip.mov --fmt m4a    # AAC 音轨 → -c:a copy 零损直通
downloadhelper extract-audio clip.mp4 --no-copy    # 强制重编码
```

策略：默认 `--copy` 优先——ffprobe 读首个音轨 codec，若可无损放入目标容器
（mp3→mp3、aac/alac→m4a、flac→flac、pcm_*→wav）则直通不重编码；否则回退重编码。
无音轨时报明确错误（码 23）。

## 输出预设（profiles）

| 预设 | 含义 | 参数 |
|------|------|------|
| `suno` | Suno 上传合规 | wav / 44100Hz / 16bit |
| `lossless` | 归档无损 | flac |
| `web` | 分享兼容 | mp3 / 192k / 44100Hz |

显式 `--fmt/--bitrate/--sample-rate/--bit-depth` 优先级高于预设。

```bash
downloadhelper convert song.mp3 --profile suno
```

## 通用选项

| 选项 | 说明 |
|------|------|
| `-o/--out` | 输出目录（默认当前目录），文件名 = 输入主干 + 目标扩展名 |
| `--stem` | 覆盖输出文件名主干 |
| `--overwrite` | 目标已存在时覆盖（默认报错码 23） |
| `--ffmpeg-path` | 显式 ffmpeg 路径 |

错误码沿用 download 域：20=ffmpeg 缺失、23=转码失败/参数非法、24=输出不可写。

## NCM 解包（1.6.1）

网易云加密容器（`.ncm`）还原为原始音频，**不转码、零音质损失**，纯标准库实现：

```bash
downloadhelper ncm song.ncm -o ./out                 # 输出 flac/mp3 + 封面
downloadhelper ncm song.ncm --embed-tags             # 标题/艺术家/专辑/封面写入音频（ffmpeg）
downloadhelper ncm song.ncm --no-cover --stem mine   # 自定义文件名主干
downloadhelper batch ./downloads --include-ncm       # 批量混合扫描（fMP4 + ncm）
sunoaux post ncm song.ncm                            # 聚合入口镜像
```

技术要点：

- 容器：CTENFDAM 魔数 → 密钥段（XOR 0x64 → AES-128-ECB core key）→
  元数据段（XOR 0x63 → 163 key 前缀 → base64 → AES meta key → JSON）→
  封面 → 载荷
- 载荷：网易云定制 RC4 流密码，密钥流按**绝对偏移**取（状态不随读取推进），
  支持分块并行解密
- AES-128-ECB 为 FIPS-197 官方向量锁定的独立实现；正确性另由
  examples/ 真实样本（本地研读，不入库）互操作验证
- 输出文件名按元数据（歌名 - 艺术家）生成，非法字符自动清洗
- 错误码：25=非 NCM 容器、26=容器损坏/密钥段失败
