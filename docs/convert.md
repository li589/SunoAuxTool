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
