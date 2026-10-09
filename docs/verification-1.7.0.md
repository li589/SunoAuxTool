# 1.7.0 功能验证与实战测试报告

日期：2026-10-09 · 环境：Windows 11 · Python 3.12.9（主 venv）· torch 2.5.1+cu121

## 一、环境诊断（`sunoauxtool doctor` 实跑）

**结论：全部正常**（0 错误 / 0 警告）。

| 项 | 状态 |
|---|---|
| Python / fluidsynth / 双 SoundFont | ✅（module/ 内置路径） |
| ffmpeg + ffprobe（N-125328）| ✅ |
| 转码编码器 mp3 / m4a(aac) / flac | ✅ 全齐 |
| torch + CUDA | ✅（可用显存 ≈6.9GB） |
| audiocraft（MusicGen）| ✅ |
| diffrhythm（**源码模式口径**，module/diffrhythm）| ✅ 1.7.0 起 doctor 与适配器对齐 |
| espeak-ng | ✅ |
| AudioSR 源码目录 | ✅ |
| basic_pitch（onnxruntime 后端）| ✅ |

## 二、核心链冒烟（真实引擎实跑）

| 环节 | 命令 | 结果 |
|---|---|---|
| 旋律生成 | `sunoaux pre melody --key "C major" --chords C-G-Am-F --seed 5 --variations 1` | ✅ 2 轨 MIDI（project-date 布局） |
| 谱面 | `sunoaux pre score <mid> --format svg,jianpu` | ✅ 8 小节 / 96 音 / 双格式 |
| 渲染 | `sunoaux pre render --input <mid> -o <wav>` | ✅ FluidSynth 真渲染 18.4s |
| 测速 | `sunoauxtool tempo <wav>` | ✅ **119.9 BPM（真值 120，置信 0.89·高）** |
| 转谱（内置）| `sunoauxtool transcribe <wav>` | ✅ 单旋律后端出 MIDI |
| 转谱（复调）| `sunoauxtool transcribe <wav> --backend basic-pitch` | ✅ **ONNX 真推理**（TF 未装的告警为 basic-pitch 上游预期行为） |
| 音频互转 | `sunoaux post convert-audio <mp3> --fmt flac` | ✅ 真实 320kbps mp3 → flac |
| 取证 | `sunoaux post probe <mp3>` | ✅ 元数据正确（184.3s / 320kbps） |
| 聚合入口 | `sunoaux pre/post --help` | ✅ AI 命令已镜像（musicgen / diffrhythm） |

产物留档：`output/verify17/20261009/`。

## 三、AI 模块可用性矩阵（逐适配器核验）

| 模块 | is_available | 推理后端/路径 | 端到端基线 |
|---|---|---|---|
| MusicGen（audiocraft）| ✅ | CUDA（可用显存 6.9GB）| 1.6.0 实测真跑（GPU） |
| DiffRhythm（源码模式）| ✅ | module/diffrhythm + espeak-ng | 1.6.0 实测真跑 |
| AudioSR（源码模式）| ✅ | src/versatile_audio_super_resolution | 1.6.0 实测真跑 |
| BasicPitch（ONNX）| ✅ | onnxruntime；**1.7.0 本次冒烟再证真推理** | 本报告 §二 |

说明：重模型完整生成（MusicGen/DiffRhythm/AudioSR）未在本次重复执行——1.6.0 基线已验证本机端到端，本次以「依赖链 + 可用性 + 轻量真推理（basic-pitch）」为口径，避免数小时级 GPU 占用。

## 四、解密域（unlock）验证口径

- 本机无新增真实加密样本；1.6.3-1.6.5 的实弹口径不变：
  - KGM：ghtz08 官方加密/解密文件对（真向量实弹）；
  - QMC：官方 Rust 单元测试同源向量（transform/0x7FFF 边界、key_compress、Map、RC4 256B、tc_tea）；
  - KWM：worthsee 解密脚本字节级交叉验证；
  - maskV1：与官方 73MB 公钥 265 采样点零差异。
- STag/MusicEx 场景经 1.6.5 EKey 三层供给（显式/密钥库/在线模板）测试覆盖。

## 五、已知边界（非缺陷）

1. `transcribe --backend basic-pitch` 首行打印上游 TF 告警（basic-pitch 包行为）。
2. doctor 的 CUDA「获取详情失败」为 `get_device_properties` 在部分驱动下的兼容提示，不影响可用性判定。
3. tempo ≥160BPM 素材仍受默认节奏先验折半影响（`--prior-bpm 0` 可关，1.5.4 已知边界）。
4. 内置转谱后端只适合单旋律；复调走 basic-pitch（已实测）。
