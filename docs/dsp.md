# DSP 功能包（R6）

`post dsp` 提供管道式 DSP 算子链：一次调用、多个算子按序执行。纯 numpy/scipy 实现
（不依赖 ffmpeg/sox），错误码分段：**15 = DSP 处理失败**、**16 = DSP 参数错误**。

## 用法

```bash
sunoaux post dsp <wav> --ops "<算子串>" [-o out.wav] [--bit-depth 16|24]
```

- 输出默认 `<输入主干>_dsp.wav`（同目录）；`--bit-depth 24` 写 PCM_24。
- `concat` 的相对路径按输入文件所在目录解析。
- 未知算子 / 非法参数 → 退出码 16；静音做 loudnorm 等运行期失败 → 退出码 15。

## 算子一览

| 算子 | 语法 | 说明 |
|---|---|---|
| `norm` | `norm [-1]` | 峰值归一化到目标 dBFS（可放大可衰减；静音不缩放） |
| `loudnorm` | `loudnorm [-16] [ceiling=-0.3]` | **EBU R128 简化版**：K 加权（BS.1770 两阶段 biquad，逐 sr bilinear）+ 400ms/100ms 分块 + 绝对门 -70 LUFS / 相对门 -10 LU → 目标 LUFS。**峰值保护（1.4.6）**：增益后自动串联 limiter 硬顶在 ceiling dBFS（默认 -0.3，防高动态素材削波）；`ceiling=off` 关闭 |
| `fade-in` / `fade-out` | `fade-in 0.5` | 余弦淡入/淡出（秒）；超出音频长度 → 16 |
| `trim` | `trim 10-25` | 裁剪 [START, END) 秒；区间非法/越界 → 16 |
| `resample` | `resample 32000` | scipy `resample_poly`（polyphase 抗混叠）；采样率相同为 no-op |
| `lowcut` | `lowcut 80` | 一阶高通低频切（复用既有 `filters.highpass`） |
| `eq` | `eq <peak\|lowshelf\|highshelf> <freq_hz> <gain_db> [q]` | **频段 EQ（1.5.0 F4）**：RBJ cookbook biquad。`peak` 中心频率处精确增益（Q 控制带宽，默认 1.0）；`lowshelf`/`highshelf` 低/高搁架（固定 S=1 斜率）。`gain_db=0` 恒等；\|gain\| > 24 dB / freq ≥ Nyquist / q ≤ 0 / 频段名非法 → 16。例：`eq lowshelf 120 -3, eq peak 2500 4 0.8` |
| `gate` | `gate <threshold_db> [attack_ms] [release_ms]` | **噪声门（1.5.0 F4）**：低于阈值（dBFS，默认 -50）的部分渐闭到静音；attack（默认 5ms）即时打开 + 防抖保持，release（默认 100ms）平滑关闭。与 `expand`（软比例衰减）互补：gate 是硬门限 + 时间平滑。阈值 ≥0 / attack/release < 0 → 16 |
| `compress` | `compress 3 [-14]` | 软拐点压缩：ratio（≥1）+ 阈值 dBFS（默认 -12） |
| `expand` | `expand 2 [-30]` | **向下扩展（1.4.0）**：低于阈值的部分按 ratio 衰减（ratio ≥1，=1 恒等；阈值 dBFS 默认 -30）。用于压低底噪/弱段 |
| `limiter` | `limiter [-0.3] [5]` | **前瞻拐点限幅（1.4.0）**：峰值硬顶在 ceiling dBFS（默认 -0.3）之下，brickwall 有数学保证；lookahead ms（默认 5）+ release 50ms 固定。ceiling ≥0 / lookahead <0 → 16 |
| `concat` | `concat b.wav [xf=0.5]` | 拼接（可选秒数交叉淡化，等功率余弦）；采样率不一致自动重采样；**声道数不一致 → 16（1.4.6 前置校验，原为裸 ValueError）** |
| `reverb` | `reverb 0.3 [1.2]` | **混响（R14）**：合成指数衰减噪声 IR + FFT 卷积 + wet/dry（wet ∈ [0,1] 默认 0.3；IR 时长秒 默认 1.2）。wet/时长非法 → 16 |

算子串语法：逗号分隔 `name` / `name 主参数` / `name key=value`，
例：`"norm -1, fade-in 0.5, trim 10-25, resample 32000, concat bed.wav xf=0.5"`。

### 混响的合规边界（R14）

`reverb` **只在 standalone 后处理链**可用：`sunoaux post dsp <wav> --ops "reverb 0.3"`。
`export suno` 链与 `pipeline`（二者走 `DspProcessor` 内部链）**恒不带混响**——Suno 合规
要求无混响；在那里请求混响会得到**退出码 1** 并提示改用 standalone 路径。

IR 为**确定性合成**（指数衰减噪声，L1 归一化），非真实采样 IR：不入库、无版权与 LFS 负担；
且 `sum|ir| = 1` 使卷积成为收缩映射（输出峰值 ≤ 输入峰值），数学上不会爆音，
故无需事后 limiter。输出截断到输入长度（insert 效果，时长不变）。

## 架构

```
src/sunoauxtool/dsp/
├── processor.py   # P2-1 管线内链（render→export），增量兼容不动
├── filters.py     # 高通/压缩器（numpy，P2-1 起既有）
├── ops.py         # R6 通用算子框架：parse_ops → 注册表 → apply_ops
└── loudness.py    # EBU R128 简化版积分响度（BS.1770 口径）
```

新算子接入：写 `op_xxx(audio, sr, args) -> (audio, sr)` 纯函数 + 在 `OPS` 注册 +
数值断言单测（对齐 `tests/test_dsp_ops.py` 口径），无其它接线。

## 验证口径（tests/test_dsp_ops.py，23 例）

- `norm`：峰值 == 10^(dB/20)；
- `fade-in`：起点 0 / 终点 1 / 中点 0.5（余弦）；
- `resample`：采样率与时长按比例、440Hz 主频保持；
- `loudnorm`：997Hz 满幅正弦实测 ≈ -3.0 LUFS（理论 -3.70 raw + K 加权 +0.7dB，±0.8）；
  归一后实测 == 目标（±0.3 LU）；
- `concat xf`：总长 = 两段之和 − 交叉淡化；中点 = 等功率混合值。
