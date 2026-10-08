# 计划：1.5.1 beat 相位精度（原「centered-STFT 立项」）

> 状态：**✅ 已实施（2026-10-08）**。最终实测（真实管线，45 组 BPM 正确 case）：
> **mean 1.25ms / p95 3.86ms / max 4.24ms**。实施中在计划外追加一项改进：
> 相位细化后**按细化偏移重算得分**再参与跨 BPM 比较（网格分只是同一峰的近似，
> max 7.4 → 4.2ms）。原立项结论（否决 centered 重写）不变。

> 状态：**已立项，待实施**（2026-10-08）。结论先行：**否决 centered-STFT 重写，
> 采纳两项便宜修正**——校正常数理论化 + 相位得分抛物线细化。
> 依据：60 组合成点击轨 × 4 方案对照实验（本文件全部数据可由
> `.workbuddy/tmp/beat_centered_experiment.py` 复现）。

## 一、背景与问题

`analysis/tempo.py` 的节拍相位（`beat_offset`，第一拍秒位置）当前实测误差
**mean 11.5ms / max 20.9ms**（合成点击轨，22050/44100/48000 三档 sr ×
90/120/150/180 BPM × 5 相位）。记忆中的既定立项假设是：

> 「onset 包络有 0~1 窗长提前偏置（非 centered STFT），半窗校正只修掉均值，
> 残差 ±18ms 是成帧方式的本质缺陷 → 需要 frame_view 改 centered 才能突破」

**本立项用实验证伪了这个假设**（见下）。

## 二、实验与根因

### 2.1 符号偏差诊断（关键发现）

现行误差几乎**全是恒定符号偏差**：bias = −11.5ms，std 仅 5.6ms。
说明 ±18ms 残差不是随机噪声，而是**校正常数取错了**——`frame/(2·sr)`（1024 样本）
少补了 384 样本（= 3/4 hop）。

### 2.2 校正量扫描（60 组 click 轨）

| 校正（样本） | bias(ms) | std(ms) | mean&#124;err&#124;(ms) | max(ms) |
|---:|---:|---:|---:|---:|
| 1024（现行 frame/2） | −11.5 | 5.6 | 11.5 | 20.9 |
| 1280 | −3.9 | 3.0 | 4.2 | 9.3 |
| **1408（甜点）** | **−0.1** | **2.1** | **1.6** | **8.3** |
| 1536 | +3.7 | 1.9 | 3.7 | 11.2 |

### 2.3 centered-STFT 对照（证伪立项假设）

centered 成帧（pad frame//2、帧中心时间戳）+ 校正 384 样本：

| 方案 | bias(ms) | std(ms) | mean&#124;err&#124;(ms) | max(ms) |
|---|---:|---:|---:|---:|
| A 现行 + 1408 | −0.1 | 2.1 | 1.6 | 8.3 |
| **A 现行 + 1408 + 相位抛物线细化** | **−0.2** | **1.6** | **1.2** | **4.6** |
| C centered + 384 | −0.3 | 2.2 | 1.7 | 8.3 |
| C centered + 384 + 细化 | −0.4 | 1.6 | 1.2 | 4.6 |

**两条路线全指标打平**。centered 重写需要动 `frame_view`/`stft_magnitude`
共享底座（transcribe.py 的 ±20ms 口径与 4096 窗尾教训都在非 centered 口径下调出、
key.py 的 Chroma 数值也会漂移），收益为零 → **否决**。

### 2.4 校正常数的原则化（替代魔法数字）

谱通量峰 = Hann² 窗能量差分最大处。纯理论值
`p* = argmax(w²[p] − w²[p+hop])` 与实测甜点差一个稳定比例
（log1p 压缩 + 沿频率求和使峰位后移）：

| (frame, hop) | p* | 实测甜点 | 甜点/p* | 1.25·p* 方案 mean&#124;err&#124; |
|---|---:|---:|---:|---:|
| 2048, 512（默认） | 1132 | 1408 | 1.244 | **1.2ms** |
| 1024, 256 | 566 | 704 | 1.244 | **1.1ms** |
| 2048, 256 | 1242 | 1536 | 1.237 | **2.1ms** |
| 4096, 1024 | 2265 | 2944 | 1.300 | 6.7ms（hop 过大，绝对分辨率不足） |

→ 通用公式 **`corr = 1.25 · p*`** 跨参数可移植（前三组 mean ≤ 2.1ms；
4096/1024 属病态参数组合，文档标注即可）。

### 2.5 明确出范围：软起音形态偏差

软起音（20ms attack 正弦音）素材上**所有方案（含现行与 centered）一律
+59ms 恒定偏差**。这是谱通量「最大谱增位点 ≠ 声学起始点」的固有属性，
与成帧/校正无关，任何 STFT 层改动都无效。若未来要形状无关的相位，
需换 onset 检测器（复数域差分 / 自适应白化等），另行立项。
对拍网格对齐（`--tempo-grid` 视频节拍尺）用途影响有限。

## 三、实施方案（1.5.1）

### 3.1 `src/sunoauxtool/analysis/tempo.py`

1. **校正常数理论化**：删除 L250-254 的 `best_offset += frame / (2.0 * sr)`
   半窗注释块，替换为模块级函数：

   ```python
   def _flux_offset_samples(frame: int, hop: int) -> float:
       """谱通量峰相对真实起音的系统提前量（样本）。

       p* = argmax(Hann²[p] − Hann²[p+hop]) 是纯窗函数理论峰位；实测/log 通量
       峰比它晚 ~1.25×（log1p 压缩 + 沿频率求和所致，四组 (frame,hop) 标定
       ratio 1.237~1.300）。见 docs/reports/plan-1.5.1-beat-phase.md §2.4。
       """
       if frame <= hop:
           raise ParameterError(f"frame 须 > hop: {frame} <= {hop}", code=1)
       w2 = np.hanning(frame) ** 2
       p_star = int(np.argmax(w2[: frame - hop] - w2[hop:]))
       return 1.25 * p_star
   ```

   `estimate_bpm` 内：`best_offset += _flux_offset_samples(frame, hop) / sr`。

2. **相位得分抛物线细化**：`_phase_best` 中在最优网格点 k（非端点）处对
   `(score[k-1], score[k], score[k+1])` 三点抛物线求极值偏移
   （clip 到 ±半步长，step = period/steps），把 0.25 帧网格粒度消掉。
   实测收益：max 8.3 → 4.6ms，std 2.1 → 1.6ms。

### 3.2 测试（`tests/test_analysis_tempo.py`）

- 收紧既有容差：`test_click_track_confidence_and_phase` 的 `phase_err < 0.06`
  → `< 0.02`；`test_beat_offset_known_phase_regression` 的 `abs=0.08` → `abs=0.02`。
- 新增：多 (frame, hop) 参数组标定回归（默认组 + (1024,256)，断言 mean|err| < 5ms）；
  `frame <= hop` 参数校验（退出码 1）。
- 真实素材既有用例（若有）只验证**不回退**（60ms 级断言保留），不作精度主张。

### 3.3 文档

- `docs/usage.md` §2.10：补一行「beat_offset 相位精度：合成点击轨 mean 1.2ms /
  max 4.6ms（1.5.1 起）；软起音素材存在 ~+60ms 谱通量形态偏差（固有限制）」。
- `docs/reports/audit-2026-10-07.md`：B2 条目补「1.5.1 精度增强」标注。

## 四、验收标准

1. 合成点击轨（3 sr × 4 bpm × 5 相位 = 60 组）：bias |·| ≤ 2ms，
   mean|err| ≤ 2ms，max ≤ 6ms（抛物线细化后实验值 1.2/4.6，留余量）。
2. BPM 精度不回退：既有 ±3% 断言全绿。
3. 根套件全绿（当前 1441 例基线）+ 覆盖率 ≥ 87% + ruff clean。
4. `--tempo-grid` 视频节拍尺对齐目测无回退（手动冒烟，可选）。

## 五、风险

| 风险 | 缓解 |
|---|---|
| 1.25 系数仅在合成素材标定 | 真实素材用例只验证不回退；docstring 写明标定来源与适用边界 |
| 抛物线细化在相位网格端点退化 | 端点（k=0 / k=steps−1）不细化，退回网格值 |
| frame ≤ hop 病态参数 | `_flux_offset_samples` 前置校验 ParameterError(1) |
| 非 (2048,512) 参数组合精度低 | 文档标注；(4096,1024) 类组合属绝对分辨率不足，不承诺 |

## 六、版本与发布

- 版本：**1.5.1**（patch：行为精度修正，无 API 变更）。
- 流程：实现 → 全量套件 + ruff → CHANGELOG → bump 四文件 → commit/push → tag + Release。
