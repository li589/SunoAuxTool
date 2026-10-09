# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 风格。

## [1.6.4] - 2026-10-09（KGM 零公钥化 + KGMA 支持 + KWM 交叉验证）

### 变更
- **KGM/KGE/VPR 零外部依赖**：内嵌 maskV1 两张 272 项递归表，可直接现算
  出与官方 73MB 公钥**逐字节等价**的 pub 流（用 ghtz08 官方密钥抽样 265
  点含边界验证，零失配）。外置公钥文件降级为可选加速项（`SUNO_KGM_KEY`），
  缺失/长度不符时静默回退，不再报 29；官方加密/解密测试对在纯 maskV1
  路径下实弹通过。
- **新增 KGMA（酷狗新格式）**：头部布局与 KGM 相同但无固定魔数，按扩展名
  + 头部可解析性识别（`kgm.plausible_header`）；ghtz08 向量按 .kgma 分发
  解密实弹通过。

### 新增
- 测试 +5：maskV1/官方公钥等价性（skipif）、零依赖实弹、KGMA 分发、
  宽松头校验、公钥长度不符回退。

### 交叉验证
- KWM：新增 worthsee.com 在线转换站逆向参考（examples/kwm.worthsee），
  其 decrypt.js 与 1.6.3 实现在魔数、种子派生、padEnd(32) 语义、固定异或
  串上逐常量一致——独立双源确认。

## [1.6.3] - 2026-10-09（unlock 通用解密：酷我/酷狗/QQ 音乐多格式）

### 新增
- **`sunoauxtool.download.unlock`**（零第三方依赖，纯离线）：
  - **KWM（酷我）**：魔数 `yeelion-kuwo`，头部 24-32 的 u64 种子十进制串
    与固定字符串异或生成 32 字节循环密钥（算法与常量经官方 web bundle
    逆向校验）；
  - **KGM/KGE/VPR（酷狗）**：17 字节文件私钥 + 272 字节修正表 + 外置公钥
    （~73MB，`SUNO_KGM_KEY` 环境变量或 `~/.cache/sunoauxtool/kugou_key.xz`
    懒加载，缺失报 29）；VPR 额外 VprMaskDiff 掩码；ghtz08 官方加密/
    解密测试对实弹验证逐字节一致；
  - **QMC（QQ 音乐）**：V1 静态密钥（tkm/bkc*/十六进制扩展名）+ V2 内嵌
    EKey（mflac/mgg*/qmc*/mmp4；PcV1Legacy/QTag footer；STag/MusicEx 无
    内嵌密钥时报 29 提示在线获取）；tc_tea（腾讯定制 CBC）+ EKey 双层
    解密 + 按主密钥长度自动选择 Map/RC4 流密码；全部对齐 unlock-music
    官方 Rust 实现测试向量；
- **`downloadhelper unlock`**：多文件解密入口（自动按扩展名/魔数分发，
  含 ncm 透传），输出按嗅探结果命名；
- **`downloadhelper batch --include-unlock`**：批量扫描同时解密受支持
  加密格式；`sunoaux post unlock` 聚合镜像；
- 错误码扩展：27=无法识别的加密格式，28=解密失败，29=缺少外部密钥；
- 测试 +48：官方向量（V1 transform/0x7FFF 边界/key_compress/QMC2Map/
  hash/segment_key/RC4/QMC2RC4/tc_tea）、KGM 朴素实现对拍 + 实弹向量、
  自打包端到端（kwm/qmcflac/qmc3/tkm）、footer 解析、CLI/批量/聚合。

### 说明
- unlock 组件不对解密结果重嵌标签：QMC/KWM 加密覆盖整文件（含原始
  ID3/Vorbis 标签），解密即完整还原。
- 酷狗公钥文件不随包分发（体积与合规考虑），README 附获取方式与
  上游致谢链接。

## [1.6.2] - 2026-10-09（转码中枢整合：批量混合扫描 + 标签嵌入 + doctor 探测）

### 新增
- **`downloadhelper batch --include-ncm`**：批量扫描同时处理 fMP4 解码与
  .ncm 解包，报告分类汇总（解码/NCM/跳过/失败）。
- **`downloadhelper ncm --embed-tags`**：解包后把标题/艺术家/专辑与封面
  经 ffmpeg 嵌入音频（mp3→ID3v2+APIC，flac→Vorbis+picture），流复制
  不重编码；`-f` 封装器按扩展名推断（`.tagged.flac` 保留原后缀防
  muxer 推断失败）。
- **doctor 转码编码器探测**：逐一检查 libmp3lame / aac / flac，缺失时
  给出警告并计数（不阻塞）。
- 测试 +3：batch 混合扫描（有/无 --include-ncm）、标签嵌入经 ffprobe
  验证（真实 FLAC 载荷）。

## [1.6.1] - 2026-10-09（NCM 解包组件：网易云容器还原原始音频）

### 新增
- **`sunoauxtool.download.ncm`**（纯标准库，零第三方依赖）：
  - 手写 AES-128-ECB + PKCS#7（FIPS-197 附录 C.1 官方向量锁定正确性）；
  - 网易云定制 RC4 流密码：KSA keybox + **两级 box 查表**按绝对偏移取
    密钥流（状态不随读取推进→可分块并行），大整数 XOR 加速整段解密；
  - 容器解析：CTENFDAM 魔数 → 密钥段（XOR 0x64→AES→去
    neteasecloudmusic 前缀）→ 元数据段（XOR 0x63→163 key 前缀→base64→
    AES→music: JSON）→ crc32/gap → 封面 → 载荷；
  - 载荷签名判定 flac/mp3，元数据命名输出文件（含非法字符清洗），
    封面导出 JPEG。
- CLI：`downloadhelper ncm` + 聚合镜像 `sunoaux post ncm`；
  错误码 25（非 NCM 容器）/ 26（容器损坏）。
- 测试 18 例：AES 官方向量、RC4 往返与偏移确定性、**自打包 NCM 往返**
  （pack helper 仅存在于测试侧，产品不提供加密能力）、容器边界
  （魔数/截断/错误 core key/空载荷/重复输出）、**examples/ 真实样本
  互操作校验**（样本不入库，缺失时 skip；4s FLAC 经 ffprobe 验证有效）。
- 标签/封面嵌入音频文件（ffmpeg -metadata 路线）留待 1.6.2 与批量整合一并做。

## [1.6.0] - 2026-10-09（通用转码中枢：音频互转 + 视频分离音轨）

### 新增
- **`sunoauxtool.download.convert`**（DownloadHelper 媒体转换中枢首批能力）：
  - **音频互转** mp3/wav/m4a/flac 全方向（libmp3lame / pcm_s16le·s24le /
    aac / flac 编码映射），支持码率/采样率/位深参数；
  - **视频分离音轨** mp4/mov/flv/webm/mkv/avi/m4v：ffprobe 读音轨 codec，
    可无损放入目标容器时 `-c:a copy` **零损直通**（aac→m4a 等），否则回退
    重编码；无音轨明确报错；
  - **输出预设** `suno`（44.1k/16bit wav）/ `lossless`（flac）/ `web`
    （mp3 192k），显式参数优先。
- CLI：`downloadhelper convert` / `downloadhelper extract-audio`；
  聚合镜像 `sunoaux post convert-audio` / `sunoaux post extract-audio`
  （`post convert` 保持 fMP4 decode 旧映射不变）。
- ffmpeg 引擎复用两层环境变量定位；错误码沿用 download 域 20/23/24。
- 测试：33 例（ffmpeg 合成源 4×4 互转矩阵、直通/重编码分支、预设/位深/
  采样率断言、错误路径）。
- `.gitignore examples/`（参考项目仅本地研读）；README 新增「致谢 / 相关
  项目」友情链接（ncm2mp3 / ncmdump / ncm2mp3-js / SUNO Capture / DLBunny，
  零代码依赖）。方案文档：docs/reports/plan-1.7-convert-ncm.md。

## [1.5.6] - 2026-10-09（全流程验证 bugfix：inspire 懒建表 + musicgen 深依赖收口）

### 修复
- **`inspire add/list` 全新数据库报 `no such table: inspirations`**（全流程
  验证发现）：只有显式 `inspire init` 会建表，直接 add/list 即炸。DDL 提为
  模块常量并移入 `_conn()` 懒初始化，任何操作幂等建表；`init_db()` 行为不变。
  +2 例回归测试。
- **`ai musicgen` 深层依赖缺失漏成意外错误（退出码 9）**：audiocraft 顶层
  可导入（`is_available()` 通过）但其内部 `import triton` 失败时，
  `from audiocraft.models import MusicGen` 抛 ModuleNotFoundError 未被收口。
  现捕获并转 AiDependencyError(6)，附 Windows 修复提示
  （`pip install 'triton-windows<3.2'`，3.8 与 audiocraft JIT API 不兼容）。
  +1 例回归测试。

### 验证
- 全流程实跑 20+ 模块（本机，含真实 AI 链）：MusicGen（triton 修复后 GPU
  真生成 15s）、DiffRhythm（module 源码模式 95s）、AudioSR（48kHz 超分）、
  basic-pitch（ONNX 复调转谱）均真跑通过。

## [1.5.5] - 2026-10-09（真实引擎音频回归语料）

### 新增
- **`tests/test_regression_real_audio.py`（12 例）**：真值 MIDI（程序化生成）
  → **真 FluidSynth + GeneralUser GS 渲染** → tempo / transcribe 无回归断言。
  区别于既有 mock_fluidsynth / 合成正弦 fixture，首次把「真实 SF2 音色 +
  渲染子进程 + 分析链」整体纳入回归保护。语料两段：120 BPM C 大调 16 音、
  90 BPM a 小调 12 音（四分断奏）。
- 断言口径（2026-10-08 实测基线 × 数倍余量）：BPM 绝对误差 ≤0.6、拍相位
  ≤30ms（基线 ≤8ms）、置信度 ≥0.5；转谱音高 ±2 半音容差匹配 ≥90%（基线
  100%——内置后端在真实钢琴音色上无容差直配仅 ~50%，谐波重叠所致，属
  文档化已知边界）、音符数 truth ~ truth+12、1/8 拍网格对齐、内部测速一致。
- **环境自适应 skip**：SF2 缺失/为 LFS 指针、fluidsynth（module 内置或 PATH）
  不可用时整文件 skip——CI（apt fluidsynth + `git lfs pull` sf2）可跑，
  语料 session 级渲染一次（<2s），不落库、零 LFS 带宽开销。

## [1.5.4] - 2026-10-09（≥160BPM 边界提示）

### 新增
- **`tempo` CLI 快节奏折叠提示**：默认节奏先验（log-Gaussian 中心 120）会把
  ≥160BPM 素材折半（已知边界）。现在在默认先径下额外跑一次关闭先验的估计，
  检测到「更快（≥160 且 ≥1.8×）且可信（conf ≥ 0.3）的档」时打印提示
  「如确认是快节奏素材请加 --prior-bpm 0 重测」。**仅提示，不改变返回值口径**；
  用户显式指定 `--prior-bpm` 时不触发（视为知情选择）。

## [1.5.3] - 2026-10-09（PyPI 发布管道）

### 新增
- **`.github/workflows/publish.yml`**：推 `v*` tag → build（sdist+wheel）→
  twine check → PyPI 发布（Trusted Publishing / OIDC，token 兜底见注释）。
  首次使用需在 PyPI 一次性登记 pending publisher。
- pyproject 打包元数据补齐：classifiers（状态/平台/主题）+ project.urls
  （Homepage/Repository/Changelog）。
- 本地已验证 `python -m build`：wheel 含全部 13 个子包 + 3 个 console_scripts
  入口点（sunoaux / sunoauxtool / downloadhelper），twine 元数据口径正确。

## [1.5.2] - 2026-10-09（L5 性能债：highpass 向量化）

### 修复
- **`filters.highpass` 逐样本 Python 循环 → `scipy.signal.lfilter` 向量化**
  （L5，审计遗留最后一条性能项）：传递函数不变
  （`b=[α,-α], a=[1,-α]`，α = 1/(1+2πfc/fs)，零初始状态逐点等价），
  长音频从 O(N) Python 循环降为 C 实现（30s 音频实测 ~9ms）。
- 新增数值等价回归：lfilter vs 循环参考实现逐点一致（mono + stereo 逐声道）。

## [1.5.1] - 2026-10-09（beat 相位精度：通量峰位校正理论化 + 相位抛物线细化）

> 「centered-STFT 立项」的实验结论落地（plan-1.5.1-beat-phase.md）：±18ms 残差
> 被证伪为恒定偏差（半窗校正常数少补 3/4 hop），centered 重写零增益已否决。

### 修复
- **`tempo.beat_offset` 校正常数**：`frame/(2·sr)` 半窗校正 → 通量峰位公式
  `1.25·argmax(Hann²[p]−Hann²[p+hop])`（`_flux_offset_samples`，跨
  (frame,hop) 参数组标定 ratio 1.237~1.300；frame ≤ hop 前置校验 → 1）。
- **相位得分抛物线细化**：0.25 帧网格上的三点抛物线极值 + 按细化偏移重算
  得分再参与跨 BPM 比较（端点退回网格值）。

### 效果
- 合成点击轨（22050/44100/48000 × 90~180BPM × 5 相位，BPM 正确的 45 组）：
  相位误差 **mean 11.5ms / max 20.9ms → mean 1.25ms / p95 3.86ms / max 4.24ms**。
  `--tempo-grid` 视频节拍尺对齐精度同步受益。
- 测试容差收紧：0.06s / 0.08s → 0.02s；新增多参数组标定回归与公式参考值用例。
- 已知边界（非本版引入）：≥160BPM 素材默认先验折半（`--prior-bpm 0` 可命中）；
  软起音素材 ~+60ms 谱通量形态固有偏差（出范围，见 plan §2.5）。

## [1.5.0] - 2026-10-09（F4 DSP 链增强：三段 EQ + 噪声门 + batch 结构化报告）

> 审计批次表 F4（"1.5/1.6 候选"）落地；`loudnorm --ceiling` 已在 1.4.6（B1）先行交付，
> 本版补齐其余三项。特性版本（minor bump）。

### 新增
- **`eq` 频段 EQ 算子**（`dsp/filters.py` + `dsp/ops.py`）：
  `eq <peak|lowshelf|highshelf> <freq_hz> <gain_db> [q]`——RBJ Audio EQ
  Cookbook biquad（scipy lfilter 逐声道）。peak 在中心频率处增益**解析精确**
  （|H(ω₀)| = 10^(gain/20)，测试按此断言）；shelf 固定 S=1 斜率。
  `gain=0` 恒等；频段名非法 / freq ≥ Nyquist / |gain| > 24 dB / q ≤ 0 → 16。
- **`gate` 噪声门算子**：`gate <threshold_db> [attack_ms] [release_ms]`
  （默认 -50/5/100）。低于门限渐闭到静音：attack 即时打开 + maximum_filter1d
  防抖保持，release 在**亏损域**（deficit = 1-need）一阶低通平滑关闭——与
  limiter 同套路（直接低通 need 会让短促开段关门从低值起衰，实测踩过）。
  与 `expand`（软比例衰减）互补：硬门限 + 时间平滑。
- **batch `--report` 结构化报告**（`batch.write_report()`）：`.json`（版本/
  时间/命令回放/汇总/逐项明细含采样参数）/`.csv`（固定 12 列，utf-8-sig
  Excel 友好）。**部分失败也完整落盘**（失败项含 error 字段）；扩展名非法
  → ParameterError(1)。

### 文档
- `docs/dsp.md` 算子一览补 `eq` / `gate` 两行。

## [1.4.8] - 2026-10-08（F5 basic-pitch 脚手架 + F6 torch.load 补丁链 + F2 覆盖率）

> 审计建议批次表第 3 行的「按方向分批」第一批：工具链补强 + 覆盖率，零行为变更
> （除两处顺手修复的健壮性 bug，见下）。

### 新增
- **F5 `scripts/setup_basicpitch.py`**：basic-pitch 一键安装脚手架。
  `--no-deps` 装主包（绕开 py≥3.11 的 `tensorflow<2.15.1` 死锁标记）+
  `resampy mir-eval onnxruntime` 三个轻量依赖；幂等（已就绪自动跳过）；
  `--check` 只探测，退出码 0/6 与 `AiDependencyError` 口径一致。
- **F6 `setup_vasr.py` → `patch_torch_load()`**：克隆上游后自动给所有
  `torch.load(...)` 单行调用补 `weights_only=False`（B6 预防：torch≥2.6 起
  默认翻转，19 处加载点会全部炸掉）。已对真实树实弹执行 19/19 并通过语法
  编译；幂等、CRLF 保留、跨行调用保守跳过并计数报告。

### 修复
- **`_parse_pitch_name_to_midi` 降号音名全灭**：`text[:i].upper()` 把
  `"Bb"` 变 `"BB"`，`_PITCH_TONE` 表查不到——docstring 自己的 `'Bb2'`
  示例都返回 None。改为原样查找 + `capitalize()` 回退（`bb2`/`Bb`/`GB` 均可）。
- **`_beats_per_bar("4/0")` 除零崩溃**：`except` 补 `ZeroDivisionError`，
  回退 4.0。

### 移除
- `_chords_track` sustain 分支中数学上不可达的越界 `break`
  （`t=(n-1)·bpb/max(4,n) < bpb` 恒成立）。

### 测试
- `tests/test_generators.py` +11（F2）：procedural.py 覆盖率 **81% → 95%**
  （half/block 密度、音域外双回退、variation 三档、辅助函数边界全套）。
- `tests/test_setup_scripts.py` 新增 12 例（F5/F6）：check 三态、main 退出码、
  补丁幂等/跨行跳过/CRLF 保留/缺失目录安全。

## [1.4.7] - 2026-10-08（审计收尾：F1 doctor / F3 video CLI 测试 / L3 死代码）

> 来源：`docs/reports/plan-1.4.7.md`（审计建议批次表第 2 行）。无破坏性变更。

### 新增
- **doctor 三项新探测**（`sunoauxtool doctor`，F1）：
  - ffmpeg / ffprobe：复用 download 包三层定位（SUNO_FFMPEG / SUNO_FFMPEG_DIRS /
    PATH / 已知目录），命中显示路径与版本；缺失计 error 并给出修法
    （video / dsp concat / preview 硬依赖）；
  - AudioSR：`resolve_audiosr_dir` 轻量检查（不触发 import），缺失计 warn
    并提示 AUDIOSR_DIR / 克隆位置；
  - basic_pitch：`find_spec` 轻量探测 + 四推理后端检查（规避其 `__init__`
    无 else 分支的 NameError 陷阱），无后端提示 `pip install onnxruntime`。

### 修复
- **`video config init` 崩溃**（F3 测试发现）：默认 Config 含 None 字段
  （logo.path 等），`asdict` 后 `tomli_w.dumps` 抛 "NoneType is not TOML
  serializable"——序列化前递归剔除 None，模板语义不变（省略 = 内置默认值）。

### 移除
- **pipeline.py 死代码**（L3）：`tmp_dir` 创建后从未使用（白建白删一个
  mkdtemp）——tmp 中转目录周期整段移除；失去唯一调用方的
  `_force_remove_tree` 连同 4 个专属测试一并删除；残留容忍测试改写为
  「.tmp 用户残留不碰不删」前提。

### 测试
- `tests/test_video_cli.py` 新增 14 例（F3）：presets/config/version、
  render 失败路径、multi 未知预设校验 + 聚合入口 `sunoaux post video ...`
  二次注册转发——`video/cli.py` 覆盖率 29% → **66%**。
- `tests/test_doctor.py` +5（F1）：ffmpeg 命中/缺失、AudioSR 提示、
  basic_pitch 无后端、新区块存在性。
- `tests/test_pipeline.py` 14 → 10（L3 移除 4 例死代码专属测试）。

## [1.4.6] - 2026-10-07（bugfix：审计 B1-B5 修复）

> 来源：`docs/reports/audit-2026-10-07.md` 全项目审计确认的 5 个中等优先级 bug，
> 全部实测复现后修复并附回归测试。

### 修复
- **B1 `loudnorm` 峰值保护**（`dsp/ops.py`）：增益后自动串联 `limiter` 把峰值
  硬顶在 ceiling 之下（默认 -0.3 dBFS）——高动态素材（crest>10，如稀疏脉冲/
  古典乐）拉响度曾可达峰值 2.0，靠写盘硬 clip 兜底产生削波失真。
  `ceiling=<dB>` 自定义，`ceiling=off` 显式关闭恢复旧行为。
- **B2 `tempo.beat_offset` 双重除以 rate**（`analysis/tempo.py`）：0.25s 相位的
  点击轨曾返回 0.0029s（被 rate 除了两次），`--tempo-grid` 视频节拍对齐必错。
  同时补上**半窗中心校正**（onset 通量峰按窗起始帧计时，系统性比真实起音提前
  0~1 窗长）：补偿 `frame/2` 后合成点击轨相位误差从 -64ms 收敛到 ±18ms 内
  （22050/44100/48000 三档 sr 实测）。相位断言口径改为按拍网格取模。
- **B3 K 加权系数缺 bilinear 步骤**（`dsp/loudness.py`）：旧实现对 48kHz 系数做
  幂次缩放，非 48k 频响有 -2.05dB@1kHz（44.1k）/ +5.97dB@100Hz（22.05k）的
  系统性偏置。改为从 ITU-R BS.1770 原型（G/Q/fc）对每个 sr 重新做 bilinear
  变换（pyloudnorm 同口径）；48kHz 复现 ITU 手工系数表（rel 1e-5 内）。
- **B4 `concat` 声道数不匹配无诊断**（`dsp/ops.py`）：mono 拼 stereo 曾抛裸
  `np.concatenate` ValueError；现在前置校验声道数并抛 `DspParamError(16)`
  带业务诊断。
- **B5 错误码表补全**（`exceptions.py` + README）：`ERROR_CODES` 补 10-14
  （video）与 20-24（download）两段实际在用但漏登记的码；README 错误码表
  同步补全（原只列到 9）。

### 测试
- `tests/test_analysis_tempo.py` +1（B2 已知相位回归，0.25s 相位 + 多 sr 验证）。
- `tests/test_dsp_ops.py` +7（B1 峰值顶限/ceiling=off 旧行为/自定义 ceiling/
  非法 ceiling；B3 48k 系数回归/频响采样率不变性/LUFS 跨 sr 一致性；
  B4 声道不匹配双向校验）。
- `tests/test_error_codes.py` 表完整性断言更新 + video/download 段语义对齐
  子包异常实际退出码。

## [1.4.5] - 2026-10-07（DSP 动态处理补全 + 旧包名 shim 移除 + 真实 ffmpeg e2e 测试）

> 原计划拆为 1.4.0（DSP）+ 1.5.0（shim 移除）两版；因 shim 移除改动很小，
> 合并为本版直接发布，跳过 1.4.0。

### 新增
- **DSP `limiter` 算子**：`sunoaux post dsp <wav> --ops "limiter [-0.3] [5]"`。
  前瞻拐点限幅器（ceiling dBFS 默认 -0.3，lookahead 默认 5ms，release 50ms）：
  attack 用 lookahead 窗口最小值滤波（增益先于峰值下降），release 在"亏损域"
  （`1-need`）上做一阶低通——**brickwall 有数学保证**（最终增益 ≤ 每样本需求增益），
  且安静段零开机瞬态（直接低通增益本身会从 ~0 爬升，已实测踩过并修正）。
- **DSP `expand` 算子**：`sunoaux post dsp <wav> --ops "expand 2 [-30]"`。
  软拐点向下扩展器（ratio ≥1，=1 恒等；阈值 dBFS 默认 -30）：低于阈值的部分按
  ratio 衰减，压低底噪/弱段；阈值处增益恰为 1（与直通连续）。
- **真实 ffmpeg e2e 测试（`tests/test_video_e2e.py`，4 例）**：FFmpegEngine
  waveform/spectrum（含背景图 + drawtext 标题链）与 FrameEngine rawvideo 管道
  （bars / waveform_scroll + 背景注入）真实渲染 320x180@10fps 1s 音频，ffprobe
  校验流/时长/尺寸。全套件增量 ~1s；未装 ffmpeg 时自动 skip。

### 移除（破坏性）
- **旧包名 shim 移除**（自 v0.7.0 弃用并多次公告，原定 1.5.0 提前至此版执行）：
  - 删除 `src/smartnotegen/`、`src/videomaker/` 两个 import shim——
    `import smartnotegen` → `import sunoauxtool`；`import videomaker` →
    `import sunoauxtool.video`；
  - 删除 `smartnotegen` / `videomaker` 两个 CLI 入口别名——视频命令改用
    `sunoaux post video render|multi`（原 `videomaker render|multi`）；
  - 根包与全部子命令（`sunoauxtool` / `sunoaux` / `downloadhelper`）不受影响。

### 测试
- `tests/test_dsp_ops.py` +7（limiter brickwall/静音直通/参数校验/CLI 端到端、
  expand 衰减/阈值连续/ratio=1 恒等/参数校验）。
- `tests/test_pipeline.py` 6 → 14（P2：pipeline.py 覆盖率 82% → 100%，见开发期间
  提交 `8e33145`）。
- 全量 1362 passed，覆盖率 89%（门槛 87%），ruff clean。

## [1.3.0] - 2026-09-22（R14 DSP 混响 + R15 视频/预览扩展）

版本映射依 `docs/reports/next-phase-plan.md`（R14+R15 → v1.3.0）。

### 新增
- **DSP 混响算子（R14）**：`sunoaux post dsp <wav> --ops "reverb 0.3 [1.2]"`。
  `dsp/reverb.py` 合成指数衰减噪声 IR 并做 **L1 归一化**（`sum|ir|=1` → 卷积是收缩映射，
  数学上不爆音，无需 limiter）+ numpy 手写 FFT 卷积（不引 scipy）+ wet/dry 混合。
- **视频视觉层 `bars`（R15）**：频率柱状条 + 峰值保持帽（numpy+PIL），经 `video_visuals`
  扩展点注册。至此 PIL 创意层 8 种风格（含 ffmpeg 引擎的 waveform / spectrum）。
- **视频预览（R15）**：`sunoauxtool video-preview <video> [--frames N] [-o out]`。
  ffprobe 取时长 → 在 `(i+0.5)*时长/n` 处抽帧（避开首帧黑场与结尾越界）→ 生成单文件
  scrub HTML（点缩略图 seek 到对应时间点）。`preview.py` 新增 `probe_duration` /
  `extract_preview_frames` / `build_video_preview_html` / `VideoPreviewGenerator`
  （`runner` 为注入点，测试零 ffmpeg 依赖）。

### 合规边界
- **混响只在 standalone `post dsp --ops` 链可用**：`DspProcessor`（pipeline / batch /
  export 内部链）仍恒禁混响（Suno 合规），报错并指向 standalone 路径。

### 输出布局
- 视频缩略帧落在**新建目录** `<out>/preview/<视频名>/`（默认 `output/preview/...`），
  不与既有音频/视频产物目录冲突；`output/` 本身 gitignored。

### 测试
- `tests/test_dsp_ops.py` +104（混响 + CLI + 合规边界）；`tests/test_video_extra.py` 10 例
  （含像素探针：底部留白纯背景 / 柱体存在 / 峰值帽衰减 / 注入背景沿用 / 静音不抛错）；
  `tests/test_preview_video.py` 13 例。全量通过，覆盖率 88.21%（门槛 87%）。

## [1.2.0] - 2026-09-21（R12 统一插件架构 + R13 分析扩展）

### 新增
- **统一插件架构（R12）**：`sunoauxtool/plugins.py` 的 `discover(point, builtins)` =
  内置硬编码 + `importlib.metadata.entry_points(group="sunoauxtool.<point>")`；
  坏插件只 `warnings.warn` 并跳过（绝不拖垮 CLI）；支持 `base` 类型校验与
  `instantiate=False`（注册表保留类本身）。
  接入 **5 个扩展点**：`download_sources` / `video_visuals` / `transcribe_backends` /
  `ai_backends` / `render_engines`；`transcribe --backend` 改走注册表。
  附可安装示例插件 `examples/plugin_demo/` 与文档 `docs/plugins.md`。
- **分析扩展（R13，numpy-only）**：`analysis/key.py`（chroma × Krumhansl-Kessler 剖面
  Pearson 相关估计调性）、`chords.py`（逐窗 chroma × 48 个三和弦模板余弦 + 连续同和弦
  合并）、`structure.py`（自相似矩阵 + Foote 棋盘核新奇度 → 段落边界）。
  CLI：`analyze <wav> [--key] [--chords] [--structure] [--json]`（全关时默认三项全跑）。
  均为启发式估计，用于辅助编曲/打点，不做权威判定。

### 修复
- **`create_visualizer` 吞掉 `**extra`（真实缺陷）**：`--style score` 传入的
  score / beat_times / bpm / notation **从未送达构造器**，实际只渲染占位帧。
  此前测试直连 `ScoreVisualizer` 所以一直没暴露；已改 `cls(config, **extra)` 并加回归测试。

## [1.1.0] - 2026-09-21（R9 VASR 固化 + R10 清理 + R11 下载源凭证接入）

### 新增
- **VASR 补丁真入库（R9）**：`patches/vasr_super_resolution_long_audio.patch` 提取
  `[SunoAuxTool patch R5]` 末块 bug 修复（已校验可干净应用到上游 `d312fba`）；
  `scripts/setup_vasr.py` 幂等地克隆上游 → 剥离内嵌 `.git` → 重打补丁；
  `tests/test_ai_audiosr.py` 160 行桩化测试（不依赖真实 torch/权重）；
  `requirements/vasr.txt` 指向补丁与 setup 脚本。
- **下载源凭证接入（R11）**：新增 `SourceAdapter.check()` 自检钩子——基类默认
  「本地源，无需凭证」、API 源**掩码回显** token（`abcd***yz`，绝不回显明文）、
  catcatch 校验目录存在性。`post fetch --dry-run` 只校验不下载；
  凭证缺失干净报 **25**、catcatch 目录不存在报 **3**。
  `list_sources()` 改走 `discover_sources()`；`sources.toml` 三源 schema 写入
  `docs/downloadhelper.md`（凭证绝不入库）。

### 变更
- **一致性清理（R10）**：删除死代码 `_not_implemented`（全仓零调用）；修正过时注释
  （R6「交付占位」→ 已交付；P1「骨架/二期」→ 已完整实现）。

## [1.0.0] - 2026-09-21（更名 SunoAuxTool + 单包化统一 + R3-R7 全量交付）

### 变更（破坏性/结构性）
- **项目更名 SmartNoteGen → SunoAuxTool**：GitHub 仓库已改名 `li589/SunoAuxTool`；
  发行名与 Python 包名 `smartnotegen` → **`sunoauxtool`**（全仓 600 处引用迁移）。
- **三包合一（单发行版制）**：`videomaker` → `sunoauxtool.video` 子包、
  `Suno-Cat-Catch-Resolve` → **`sunoauxtool.download`（品牌名 DownloadHelper）** 子包，
  各自独立 pyproject 撤销，版本号统一镜像发行版（守卫测试同步更新）。
- **兼容 shim 保留 ≥1 版本**：`import smartnotegen` / `import videomaker` 经
  sys.modules 别名转发到新位置（触发 DeprecationWarning）；CLI 旧入口
  `smartnotegen` / `videomaker` 命令保留，新增 `sunoauxtool` / `downloadhelper` 入口。
- 异常基类 `SmartNoteGenError` 类名保留（外部 `except` 不破坏），
  新增别名 `SunoAuxToolError`（同一对象）。
- 环境变量 `SUNO_FFMPEG` / `SMARTNOTEGEN_FFMPEG` / `SUNO_FFMPEG_DIRS` 全部保留。

### 新增
- **聚合 CLI 入口 `sunoaux`（R3）**：`pre`（melody/midi/score/render/transcribe，前期创作）
  + `post`（probe/convert/fetch/video/dsp/enhance，后期处理）两组子命令，统一入口。
  薄转发层（`sunoauxtool.aggregate`）直接二次注册既有命令函数——零参数复制、零业务逻辑、
  错误码原样透传；`post dsp` 已交付（R6：DSP 算子链，不再为占位）。
- **AudioSR 音质提升适配器（R5）**：`post enhance <wav> [-o out] [--model basic|speech]
  [--steps 50] [--chunk 15] [--overlap 2]`。适配器 `sunoauxtool/ai/audiosr.py` 延迟导入
  （未装依赖不影响主包与 CI，退出码 6 带安装指引）；长音频自动分块 + Hann 交叉淡化 +
  块级峰值还原；输出单声道 48kHz WAV。依赖说明见 `requirements/vasr.txt`
  （源码目录模式，权重首次运行自动下载 ~2.6GB）。
- **DSP 功能包（R6）**：`post dsp <wav> --ops "算子串"` 管道式算子链
  （norm / loudnorm / fade-in / fade-out / trim / resample / lowcut / compress / concat）。
  numpy/scipy 实现，零 ffmpeg 依赖；loudnorm = EBU R128 简化版（BS.1770 K 加权 +
  双门控积分响度，自研 `dsp/loudness.py`，无 pyloudnorm）。错误码分段：
  **15 = DSP 处理失败、16 = DSP 参数错误**。规格文档 `docs/dsp.md`。
- **下载源统一接口（R7）**：`post fetch <query> --source catcatch|suno-api|haimeng|tianyin`。
  猫抓路径 = `downloadhelper batch` 直通（报告口径不变）；三个 API 源为配置驱动留位
  （gitignored `sources.toml` + 统一 HTTP 契约，mock 契约测试锁定），错误码
  **25 = 凭证缺失、26 = 请求失败**。不做客户端逆向（既定边界）。
- `requirements` 层面：主包依赖新增 `pillow`（video 子模块需要，原为 videomaker 隐式依赖）
  与 `scipy==1.18.0`（R6 DSP：resample_poly + EBU R128 K 加权；CI 曾因缺 scipy 收集失败）。

### 测试
- 根套件 1046 → **1256 例**：download 子包 4 个测试文件（147 例）并入根 `tests/`；
  R3/R5/R6/R7 新增聚合 CLI、AudioSR、DSP、下载源测试（原独立 CI 步骤撤销，
  现在天然被根套件收集——消除「子包测试无人发现」的机制性根源）。
- 覆盖率口径随单包化扩大到 video/download 模块：**87.53%**（门槛 87%）。

### 工程化
- CI：Suno 子包独立步骤删除；零 torch 断言改用 `sunoauxtool` 入口；
  ruff `extend-exclude` 暂排除 `src/versatile_audio_super_resolution`（R5 入库时清理）。
- `docs/downloadhelper.md`（原子项目 README 并入 docs）；README/子文档定位语更新。

## [0.6.0] - 2026-09-21（谱面子系统 + 音频分析 + videomaker 滚动谱面，#9–#14）

### 新增（主包）
- **谱面子系统 `src/smartnotegen/score/`（#9–#12）**：多轨谱面生成，四层架构
  `theory → model(中间表示) → layout(排版) → drawing(共享绘制层) → svg/png/jianpu/musicxml`。
  零第三方依赖，音乐字形全部自绘。
  - 6 种导出格式：五线谱 SVG / PNG / 简谱 SVG / PNG / MusicXML / 简谱文本；
    `score_export.py` 顶层共享导出服务（`generate --score` 与 `score` 命令共用）。
  - **MusicXML 4.0 导出为纯标准库手写**（`score/musicxml.py`），带离线结构校验
    （逐小节时间游标闭合）。
  - **PNG 渲染使用 Pillow，而非 matplotlib**——绘图指令流（drawing 层）一次记录、
    SVG 与 PNG 分别序列化，保证两种位图几何同源；选 Pillow 是因为零重量级传递依赖、
    且与 videomaker 的帧渲染同栈。
  - CLI：`smartnotegen score <mid> --format svg,png,jianpu,musicxml,...`，
    `--clef` / `--key` / `--time-signature` 覆盖；非法调式/拍号前置校验为
    ParameterError(1)。`generate midi|melody --score` 同目录落谱，
    `pipeline --score` 显式请求失败即抛、附加产物失败只告警。
- **音频分析 `src/smartnotegen/analysis/`（#13，numpy-only，无 librosa）**：
  - `tempo` 命令：onset 包络 → ACF → 抛物线细化 → **节奏先验（log-Gaussian，
    中心 120BPM）消解倍频歧义** → (bpm, phase) 联合梳状搜索。合成点击轨误差
    ≤0.2%；真实 120BPM 素材测得 120.4。已知边界：≥160BPM 素材会被默认先验
    折半（`--prior-bpm 0` 关闭）。
  - `transcribe` 命令：内置单旋律/主导声部转谱（谐波 salience + 相对凹谷切重复音
    + 网格量化）；复调交给可选 basic-pitch 后端（`ai/basicpitch.py` 延迟导入，
    未装退出码 6；真实推理路径未在本仓库验证）。
    窗长 2048（4096 因窗尾泄漏会让 1/16 量化必错），时间戳取帧中心。
- **videomaker 0.4.0：`--style score` 滚动谱面 + `--tempo-grid`（#14）**：
  - `visuals/score.py` `ScoreVisualizer`：由 `Score` 模型驱动（拍→秒预换算 +
    逐帧 bisect 窗口查表），播放头居中、当前音高亮；staff（五线谱：符头+加线+
    小节线）与 jianpu（简谱数字+变音+八度点，同时值和弦纵向堆叠）双记谱法
    （`--notation`）。几何铁律：行中心对称 + 宽音域自动收缩半线距。
  - `--tempo-grid`：接入 `analysis.tempo`（`estimate_bpm` + `beat_grid`），
    底部节拍尺（小节首拍加粗）+ 头部 BPM 标注；滚动时间轴采用测速 BPM。
  - 输入非 `.mid` 且未指 `--score-midi` 时 RenderError 明确报错。

### 工程化
- `spike/` 目录为谱面/简谱渲染期的原型产物（SVG/PNG 试验输出 + 试探脚本），
  **刻意不入库**（不进版本控制）。
- 根套件 1026 → **1046 例**（score 子系统 33 + analysis 54 + videomaker score
  样式 19 等），本地覆盖率 **92.28%**（门槛 87%）。
- 版本号对齐守卫扩展到三个包（smartnotegen / videomaker / Suno-Cat-Catch-Resolve），
  含「已安装 editable 元数据 vs 源码」滞后检测（2026-09-20 实际踩中的漂移形态）。

### 文档
- `docs/score.md`（谱面生成指南）、`docs/videomaker.md`（新增 score 样式节）、
  README 命令表补 `score` / `tempo` / `transcribe`。

## [Unreleased]

### 新增
- **`src/Suno-Cat-Catch-Resolve` 子项目 v0.1.0**（`src/` 下独立 editable 兄弟包，与
  `smartnotegen` / `videomaker` 并列）：Suno「猫抓」产物逆向取证与转码。
  - `fmp4.py`：ISO BMFF 原子解析、mdat 分片提取（识别 fragmented MP4）
  - `forensics.py`：熵 / 卡方 χ² / 周期扫描 → 判定明文 or 密文（核心取证层）
  - `transcoder.py`：ffmpeg 封装（Ogg Opus 无损重封装 / MP3 转码 / 探测）
  - `cli.py`：`probe` / `decode` / `batch` / `version`（Typer）
  - `exceptions.py`：错误码 20-24（延续分段：smartnotegen 0-9、videomaker 10-14）
  - 核心层零第三方依赖，仅 CLI 依赖 typer。
- **取证结论入库**：猫抓直下的 `<uuid>.m4a` 为服务端下发的加密密文
  （χ²≈215 完美均匀、无周期性 → AES/ChaCha20 级强加密），无密钥不可破；
  缓存捕获的 fMP4 是同一首曲子的完整明文副本，直接解码即可。

### 配置
- 主 `pyproject.toml` 的 `[tool.setuptools.packages.find]` 新增
  `exclude = ["Suno-Cat-Catch-Resolve*"]`：该目录名含连字符、不是合法包名，
  必须排除，否则主包打包时会收录成非法包名。

### 工程化
- **`src/videomaker/` 与 `docs/reports/`、`styles/` 纳入版本控制**：videomaker 源码（v0.1.0 → v0.3.0
  全部交付）此前只存在于工作区、从未入库；本次连同 `tests/test_videomaker.py`（30 例）与
  `tests/test_videomaker_v03.py`（26 例）、4 份交付报告一并入库。
- **清理 `src/videomaker/` 16 处 lint 问题**：未使用导入（F401 ×13）、空 f-string（F541）、
  歧义变量名 `l`（E741）、死变量 `ring_alpha`（F841）。主包 `packages.find` 会收录
  videomaker，而 CI 的 `ruff check src/` 此前从未扫过它——推送到 main 会直接红灯，故一并修正。

### 修复
- **CI 适配真实引擎依赖（FluidSynth / SoundFont / ffmpeg）**：CI 此前长期红灯
  （`gh run list` 显示最近 4 次运行全部 failure，从未绿过）。根因是 `ubuntu-latest`
  拿不到 Windows 版 `fluidsynth.exe`——捆绑二进制在类 Unix 上「存在但不可执行」，
  渲染环境探测据此判 BROKEN（历史遗留 4 例失败 + 覆盖率 86.30% 不达标）。
  - 新增 `platform_paths.py`：**平台感知回落**。Windows 上恒不触发
    （`IS_WINDOWS` 为真时 `system_fluidsynth()` 返回 None），既有行为逐字不变。
  - `env.PathResolver` 与 `render.FluidSynthRenderer` 两处 fluidsynth 解析，在
    「文件存在但不可执行」时回落到系统 fluidsynth；无回落可用则维持原有分级报错
    （module 路径 → ModuleError(7)，非 module → RenderError(4)），不静默放过坏环境。
  - `ci.yml` 增加 `apt-get install fluidsynth ffmpeg`，并新增「渲染环境自检」步骤
    （校验 fluidsynth/ffmpeg 可用 + SoundFont 存在），让环境问题早暴露。
    SoundFont 与 Windows 二进制本就随版本控制入库（293M），CI 无需额外下载。
- **CI 拉取 Git LFS 实体（SoundFont）**：`module/**/*.sf2|exe|dll|ogg` 由
  `.gitattributes` 交给 Git LFS 管理，而 `actions/checkout` 默认**不拉取 LFS 实体**，
  CI 上拿到的是文本指针文件（以 `version https://git-lfs...` 开头）。fluidsynth 读到的
  前 4 字节是 `vers` 而非 `RIFF`，遂把合法音色库判为 BROKEN，进而抛 ModuleError(7)
  （表现为 `test_inspire_diff::test_new_non_tty` 返回 7）。CI 增加
  `git lfs pull --include="module/GeneralUser_GS/**/*.sf2"`，只拉必需音色库、
  不为 demo `.ogg` 消耗 LFS 带宽。
- **SF2 可加载性校验不再依赖声卡**：fluidsynth 加载 SoundFont 时会一并初始化音频输出，
  CI 容器没有声卡会让校验失败。改用 `file`（写入）驱动绕开音频设备——该驱动在
  Windows 与 Linux 版 fluidsynth 上都存在，故无需平台分支；并以临时目录作为 cwd
  隔离其产出的 `fluidsynth.wav`，不污染工作目录。
  （对比记录：一度尝试 `dummy` 哑驱动，但 Ubuntu 版 fluidsynth 并不编译 dummy
  ——实测驱动为 alsa/file/jack/oss/pipewire/pulseaudio/sdl2，故该方案被否决。）
- **CI 自检步骤加强**：打印 SoundFont 文件头校验（RIFF）与大小、SF2 加载退出码，
  让「LFS 未拉取」这类环境问题在跑测试前就暴露。
- **跨平台测试修正**：占位二进制（`b"MZ"`）统一补 `chmod(0o755)`。POSIX 的
  `os.access(X_OK)` 要求真实执行位（Windows 上等价于存在性检查），此前
  `test_env.py` 3 例在 CI 上因此把「存在的假二进制」误判为 BROKEN。

### 测试
- 新增 13 例：`platform_paths` 回落判定（Windows/POSIX/未安装）、env 与 render 两层
  的回落与分级报错分支（ModuleError(7) / RenderError(4)）、PATH 查找路径。
- **新增 `tests/test_commands_helpers.py`（28 例）**：补齐 `commands/helpers.py` 此前
  只被 CLI 端到端间接覆盖的分支——`_guard` 两条异常路径 × debug 开关、
  `_get_duration` 异常回落、`_write_single_metadata` 开关短路、`_diff_metadata`
  参数提取与畸形 JSON、`_prompt` / `_config_prompt` 全分支、
  `_apply_detected_to_config` 的 bpm 无引号写入与反斜杠转义。
- 覆盖率：`commands/helpers.py` 73% → **100%**；整体本地 **87.24% → 88.48%**，
  把与 87% 门槛的余量从 0.01pp 拉到 ~1.2pp（此前任何新增分支都可能误红 CI）。
- **`src/Suno-Cat-Catch-Resolve/` 新增测试套件（147 例，语句覆盖率 100%）**：
  该子包此前**零测试**。四个测试文件分别覆盖：
  - `test_fmp4.py`：原子解析（32/64 位长度、size=0 延伸至 EOF、非 ASCII 类型中断、
    截断输入）、mdat 分片拼接、`summarize` 统计；
  - `test_forensics.py`：熵 / 卡方 / 周期扫描的边界与判据常量、五种容器魔数 +
    MP3 帧同步识别、明文 / 强加密 / 弱加密（重复密钥 XOR）三条判定分支；
  - `test_transcoder.py`：ffmpeg 定位与五级回落（含环境变量两层）、`_sanitize` 规整、
    异常分级（20/21/22/23/24）、`decode_fmp4` 全分支；
  - `test_cli.py`：`probe` / `decode` / `batch` / `version` 四命令，退出码 2/22/23，
    batch 汇总报告与「非 fMP4 静默跳过」语义。
  - 设计要点：合成样本用纯 Python 拼 ISO BMFF 原子（单元测试不依赖 ffmpeg）；
    密文样本用**固定种子**生成以保证 χ² 稳定；**每个文件末尾都保留真实 ffmpeg
    端到端用例**（缺 ffmpeg 自动 skip），避免「只在 mock 下成立」的假绿。
  - conftest 有 autouse fixture 清空 `SUNO_FFMPEG*` 环境变量：ffmpeg 定位类用例
    不该被运行者机器的环境左右（否则「应回落 / 应报错」的用例会静默假绿假红）。
  - 新增子包覆盖率配置（`fail_under = 95`、omit `__main__.py`、排除 `__main__` 守卫行）。

### 工程化（CI）
- **CI 新增 `Test suno-cat-catch-resolve subpackage` 步骤**：独立安装该子包并运行
  其测试（`--cov-fail-under=95`）。原因是主包 `packages.find` 已排除该目录，
  其测试不在根 `testpaths`（`tests/`）内——**不单独跑就完全不会被 CI 收集**，
  这正是它此前长期「无测试、无人发现」的机制性原因。
  该 job 已装 ffmpeg，因此子包里的真实端到端用例会真正执行而非 skip。
- 顺带修正：`src/videomaker/pyproject.toml` 版本号 → `0.3.0`（详见下方 docs 条目）。
- **CI runner 由 `ubuntu-latest` 钉为 `ubuntu-24.04`**：`ubuntu-latest` 当前解析到
  24.04，但 GitHub 会择期把它切到 26.04（滚动标签不由本项目控制）。本项目 CI 依赖
  apt 的 fluidsynth/ffmpeg 与 Git LFS 实体，一次静默的基础镜像切换足以让刚修好的
  CI 重新变红。钉住版本后，升级变成**由我们决定时机**的动作。

### 工程化（仓库卫生）
- **取消跟踪 `.coverage`**：该文件早已在 `.gitignore` 中，却仍是 git 跟踪对象，
  导致每跑一次 pytest 就出现 ` M .coverage` 噪音、掩盖真实改动。已 `git rm --cached`
  （磁盘文件保留）。
- **删除死代码 `procedural._interval()`**：全仓库零引用（仅定义无调用），
  删除后语句总数 3510 → 3508。
- **GitHub Actions 升到 v7**：`actions/checkout` v4 → v7、`actions/setup-python` v5 → v7，
  消除 node20 运行时的弃用告警。
- **更正 `.gitignore` 中 `/*.mid` 的陈旧注释**：其注释声称"test_generators 写入 CWD"，
  但复核确认 `test_generators.py` 三例均已显式使用 `tmp_path`，全量跑测后根目录
  无任何 `.mid` 残留——规则保留作防御网，注释改为反映真实情况（触发条件已不复现）。

### 修复（依赖与工具链）
- **ffmpeg 定位不再只靠硬编码本机路径**（`suno_cat_catch_resolve/transcoder.py`）：
  原顺序为「显式参数 > PATH > 三个硬编码 Windows 目录」，换机即失效。现改为
  **显式参数 > `SUNO_FFMPEG`（文件或目录） > PATH > `SUNO_FFMPEG_DIRS`（pathsep 多目录）
  > 硬编码兜底**，硬编码条目降级为最后手段并注明"仅本机有效"。
  环境变量写了无效路径时继续回落而非直接报错；空串等同未设置。
  `FFmpegNotFoundError` 的消息改为逐条列出四种修法。
  测试侧新增 autouse fixture 清空这三个环境变量，避免开发机自身的环境
  把「应回落到 PATH / 应抛错」的用例变成假绿或假红。
- **统一 `xformers` 与 `torch` 版本**（`requirements/ai.txt`）：本机 torch 为
  `2.5.1+cu121`，而装的是 `xformers 0.0.29.post3`（为 **torch 2.6.0** 构建），
  导致 `xFormers can't load C++/CUDA extensions`、内存高效注意力不可用（2026-08-09
  QA 遗留观察第 6 条）。xformers 的轮子与 torch 版本严格绑定（其 CHANGELOG：
  `0.0.28.post3` 要求 PyTorch 2.5.1；`0.0.29.*` 要求 2.6.0），故**不改 torch**
  （项目刻意锁定 cu121，避免动到 audiocraft / DiffRhythm 链路），改**钉住 xformers**：
  `xformers==0.0.28.post3 --index-url https://download.pytorch.org/whl/cu124`。
  注：Windows 上 `0.0.28.post3` 的轮子**只发布在 cu124 索引**（2026-09-20 实测：
  cu118/cu121 索引的 win_amd64 轮子最高到 `0.0.24`），故索引写 cu124。

### 文档
- **新增 `docs/features.md`（三组件全量功能清单）**：以源码与 CLI `--help` 实测为准，
  列出 23 + 5 + 4 = 32 条命令、各组件模块能力、风格/平台预设、6 种视觉风格与双引擎路由、
  Suno 合规约束、取证判据，以及 **0–24 错误码总表**与三组件对照矩阵。README 顶部加入口。
- **统一 CHANGELOG 早期版本日期**：`[0.1.0]` / `[0.2.0]` 原写作 `2025-08-09`，
  与 `[0.3.0]` 起的 2026 年时间线相差整一年。以 git 首个提交
  （`359e041` `2026-08-09` *Initial commit: SmartNoteGen v0.1.0*）为准，
  二者均为 `2026-08-09`；`docs/` 下 6 处同类日期一并更正。
  （MM-DD 保持不动：git 中 `v0.2.0` / `v0.3.0` 的提交日确比 CHANGELOG 记录的
  **发布日**晚 1–2 天，属"先发布后提交"，非错误。）
- `README.md` 新增「Suno-Cat-Catch-Resolve 子项目」一节（含两类产物对照表与命名约定）。
- videomaker 交付报告归档至 `docs/reports/`（v0.1 → v0.3.0 共 4 份）。
- **新增「子项目变更历史：videomaker」附录**（本文件末尾）：把此前只散落在 `docs/reports/`
  的 v0.1.0 → v0.3.0 版本线正式并入主 CHANGELOG，含各版能力、已知限制与测试规模。
- `docs/reports/ci-remediation-plan.md`：CI 修复路线与决策记录。
- `src/Suno-Cat-Catch-Resolve/README.md`：补 ffmpeg 定位的四种方式与 `SUNO_FFMPEG*`
  环境变量说明；测试数更新为 147 例。

## [0.5.4] - 2026-09-20（旋律生成增强：动机驱动乐句 + 伴奏织体 + 读回拍速修复）

> 说明：本条目描述的是**实际交付到 main 的实现**。此前 `[0.5.4]` 段落曾以
> 「2 小节乐句 / 休止符呼吸 / 句尾长音」描述一版从未入库的中间稿，
> 与最终代码不符，此处按实际实现重写。

### 增强
- **`procedural._melody_track` 重写为动机驱动**：从「每拍均匀四分音符随机游走」升级为
  可记忆的乐句结构——
  - 4 小节一乐句，乐句内复用同一节奏动机（`MOTIFS`：蹦跳 / 附点 / 流动八分 / 平稳），
    乐句之间轮换，形成律动钩子；
  - 强拍（每小节第 1、3 拍）由两遍规划法落在当前和弦音上，和声清晰、有明确调性；
  - 弱拍用 `_step_toward` 级进（相邻音程 ≤2 半音）趋向下一个强拍目标，杜绝跨八度狂跳；
  - 乐句轮廓在 拱形 / 波浪 / 上行 / 下行 之间轮换，旋律有起伏而非无序游走。
- **真正读取 `melody_profile`**：`register`（解析 `C4-C6` 之类音名区间）控制旋律音域，
  `variation_strength` 控制动机活动度（此前两者均未生效）。
- **`_chords_track` 织体增强**：
  - 新增 `block` 密度模式（`_chords_track_block`）：整小节所有和弦音同时按下；
  - `sustain` 模式改为奇数小节块状和弦 + 偶数小节慢速琶音分解，伴奏不再单调。

### 修复
- **`models/midi.py` 读回拍速错误**：改用 `pretty_midi.get_tempo_changes()` 读取 MIDI 真实
  拍速事件，替代按音符密度猜测的 `estimate_tempo()`——后者会把 120 BPM 的文件猜成 180，
  导致读回后的 beat 时间轴整体缩放。

### 新增
- 自定义风格 `styles/piano-cheerful.toml`（欢快钢琴 solo：C4-C6 明亮音域、variation 0.7），
  与 `styles/starsea.toml`（现代抒情 ballads）、`styles/piano-ballad.toml`（钢琴+弦乐叙事）。
- 内置 `STYLE_PRESETS` 新增 `piano-cheerful`（钢琴主奏 + 块状和弦伴奏）。
- 新增 `_parse_pitch_name_to_midi` / `_step_toward` 工具（音名区间解析 / 级进取音）。

### 测试
- 版本号 `0.5.3` → `0.5.4`（`__init__.py` / `pyproject.toml` / `test_cli.py::test_version` 同步）。
- 全量 388 例测试通过，旋律变更向后兼容（无 `melody_profile` 时退化为级进旋律）。

## [0.5.3] - 2026-08-14（DiffRhythm 仓库路径可命令行配置 + 测试）

### 增强
- **`ai diffrhythm` 新增 `--diffrhythm-dir` 选项**：命令行级指定 DiffRhythm 仓库根目录，优先级高于 `DIFFRHYTHM_DIR` 环境变量与默认 `module/diffrhythm`，无需将仓库放在 `module/` 下（接入适配器既有 `model_dir` 参数）。

### 测试
- `tests/test_ai_diffrhythm.py` 新增 `repo_dir` 优先级参数化单测（`model_dir` > `DIFFRHYTHM_DIR` > 默认 `module/diffrhythm`）。

## [0.5.2] - 2026-08-14（代码重构 + 覆盖率提升 + CI 增强）

### 重构
- **拆分 cli.py**（1410 → 1100 行）：辅助函数抽离到 `commands/helpers.py`（15 个辅助函数）
- 保持向后兼容：`from smartnotegen.cli import app` 仍有效

### 测试增强
- 新增 29 例测试（rhythm_patterns 10 + counterpoint 4 + notes 9 + fluidsynth 3 + export 3）
- 全量 329 测试全绿，覆盖率 **87.41%**（门槛 85% → 87%）
- `.gitignore` 增加 `.coverage` / `coverage.xml`

## [0.5.1] - 2026-08-12（工程化深化）

### 工程化
- **GitHub Actions CI**：push 自动 lint + test + coverage 门禁
- **pyright 类型检查**配置（basic 模式）
- 新增 11 例测试，覆盖率门槛 80% → 85%
- README 补齐 P3 命令

## [0.5.0] - 2026-08-12（P3 三期：Suno 衔接）

### 新增（P3-B2 / P3-B3 / P3-B1）
- **`export suno-pack`**：批量导出 Suno 片段打包（目录 + zip + manifest.json）
- **`export suno-manifest`**：生成 CSV/JSON 上传清单（utf-8-sig，Excel 兼容）
- **Suno API 调研**：官方 API 已开放，推荐方案 B（独立脚本），见 `docs/ai-integration.md` §6

## [0.4.1] - 2026-08-12（打磨完善）

### 修复
- **new 向导参数 bug**：GenerationRequest 使用 merged 配置的 seed，用户输入的 BPM/和弦/小节正确生效
- **灵感库 chords 提取**：_load_metadata 优先取 midi 产物完整参数，chords/bpm/bars 正确入库
- **config init Windows 路径**：TOML 路径反斜杠转义，避免解析失败
- **版本号规范化**：0.1.0 → 0.4.1（与 CHANGELOG 对齐）

### 增强
- **config init 交互式向导**：自动检测 module/ 路径，交互式引导配置；`--yes` 非交互模式
- **doctor 增加配置检查**：检查 config/default.toml + smartnotegen.toml 存在性

### 测试
- 全量 279 测试全绿（新增 8 例）
- 补充 inspire（features 边界/chords/组合筛选/导出）+ preview（base64/自动标签/频谱）测试

## [0.4.0] - 2026-08-12（P3 二期：创作工作台）

### 新增（P3-C1 / P3-C3 / P3-C2）
- **灵感库（SQLite）**：`inspire init/add/list/show/rm/export`，自动从 metadata.json 提取元数据，支持标签 + 评分 + 多维筛选
- **版本对比**：`diff <wav1> <wav2>` 对比时长/RMS/峰值/频谱中心/频段能量 + 参数
- **参数引导**：`new` 交互式向导，非 TTY 自动执行 pipeline

## [0.3.0] - 2026-08-11（P3 一期：创作体验闭环）

### 新增（P3-A1 / P3-A2 / P3-A3 / P3-E3 / P3-E2）
- **HTML 预览页**：pipeline/render/export 后自动产出 preview.html（波形 + 频谱 + 播放器，base64 内嵌，离线可用）
- **play 子命令**：系统默认播放器播放 WAV
- **音频特征摘要**：RMS/峰值/频谱中心/频段能量写入 metadata.json
- **doctor 子命令**：一键环境诊断（Python/fluidsynth/SF2/CUDA/AI 依赖/espeak）
- **config 预览节**：[preview] 配置 + --no-preview

## [0.2.0] - 2026-08-09（P1 二期 AI 冲刺）

### 新增（T-S1 / T-P1-1 / T-P1-2）

- **T-S1 DiffRhythm spike（8GB 显存）**：完成 CUDA torch（2.5.1+cu121）+ espeak-ng 1.52.0 安装与验证、权重经 hf-mirror 下载（约 7.5GB）、`chunked=True` 补丁落地与实测推理；报告归档于 `docs/ai-integration.md` §4（峰值显存 / 95s 耗时 / 音质 / GO-NO-GO）。
- **T-P1-1 MusicGen 适配器完整实现**：`ai musicgen` 支持旋律 WAV 条件扩编曲（`generate_with_chroma`）、默认 medium fp16 / `--model-size small` 降档、显存检查防 OOM（不足提示降档建议）、`--seed` 可复现（实测字节级一致）、输出 32kHz WAV 可被 `export suno` 消费；延迟导入 audiocraft，未装依赖退出码 6 并给出安装指引。**实测修正**：audiocraft 仅 `facebook/musicgen-melody`（1.5B）支持旋律条件，因此 `--model-size medium` 映射到 musicgen-melody；`small`（300M）不支持 chroma，自动降级为纯文本生成。性能基线：20s 输出峰值显存 5530 MiB、chroma 相关 0.706。
- **T-P1-2 DiffRhythm 适配器完整实现**：`ai diffrhythm` 支持风格提示 + `--lyrics` 歌词生成 ≥60s 带人声歌曲草稿（`chunked=True` 默认，自动注入补丁）；`--duration` 支持 95 或 96-285s；`--device cuda|cpu`；显存 <8GB 明确提示（退出码 6）；**草稿不自动进 Suno 导出链**（含人声，仅本地听感预览），元数据标注 `contains_vocals=true`。
- **AI 环境落地**：`requirements/ai.txt` 更新（DiffRhythm 官方不可 pip 安装 → 改为仓库克隆说明 + 运行依赖清单）；espeak-ng Windows 说明（含 `PHONEMIZER_ESPEAK_LIBRARY` DLL 定位）；hf-mirror 权重下载指引。
- **测试**：新增 `tests/test_ai_musicgen.py`（15 例）+ `tests/test_ai_diffrhythm.py`（17 例）+ `tests/test_cli.py` AI 元数据用例（2 例），全部 mock 大模型（不真跑、不依赖 GPU/权重，无 GPU 环境可跑）；既有 207 测试全绿，全量 **241 用例**，覆盖率 **91%**。
- **文档**：`docs/ai-integration.md` spike 报告 + MusicGen 性能基线；`docs/usage.md` ai 子命令参数（`--model-size/--lyrics/--duration/--device`）；README AI 安装指引（含 DiffRhythm 仓库克隆 / espeak-ng / hf-mirror）。

### 说明

- 既有 3 个 P0 环境假设测试（`test_ai_musicgen_exit_6` / `test_ai_diffrhythm_exit_6` / `test_ai_adapters_unavailable_in_p0`）改为 monkeypatch find_spec，保证在"已安装 AI 依赖"的环境（如本机二期环境）与"未安装"环境均稳定通过。
- 覆盖率配置：`pyproject.toml` 不再 omit `src/smartnotegen/ai/*`（AI 模块测试计入覆盖率；AI 适配器顶部零 torch import，推理路径由 mock 测试覆盖）。

## [0.1.0] - 2026-08-09（P1 一期非 AI 冲刺增量）

### 新增（P0 里程碑）

- **CLI 入口**：`smartnotegen` 子命令体系（generate midi / generate melody / render / export suno / pipeline / batch / config / ai），错误码 0–6 映射。
- **程序化 MIDI 生成**：`generate midi` 产出 ≥3 轨（和弦/旋律/贝斯），`--with-drums` 追加第 4 轨鼓；`--seed` 可复现（同 seed 字节级一致）。
- **乐理旋律生成**：`generate melody` 基于 music21 调式/和弦约束生成旋律，支持节奏/装饰音/逆行 3 种变奏；强拍/句尾和弦音对齐率 ≥80%。
- **MIDI→WAV 渲染**：`render` 通过 FluidSynth 渲染 44.1kHz/16bit WAV；fluidsynth 缺失时退出码 4 并给出安装指引。
- **Suno 合规导出**：`export suno` 输出 10–30s 纯器乐 WAV/MP3（裁剪/循环、淡入淡出、重采样、-1dBFS 归一化）；时长越界退出码 5。
- **一键管线**：`pipeline` 零参数闭环 generate→render→export，中间产物自动清理。
- **配置体系**：`config init` / `config show`；四级合并优先级（内置 < default.toml < 用户配置 < CLI）。
- **P1 AI 骨架**：`ai/musicgen.py`、`ai/diffrhythm.py` 延迟导入适配器，P0 环境明确提示安装依赖（退出码 6），不触发任何 torch import。
- **测试**：覆盖 config/chords/generators/midi/render/export/cli 的 pytest 用例（渲染用例使用 mock，不依赖真实 fluidsynth）。

### 一期非 AI 冲刺增量（M-1 / P1-3 / P2-1 / P2-2 / P2-4 / P2-5 / P2-3）

- **M-1 module 环境接入**：`render`/`pipeline` 默认使用 `module/` 下真实 fluidsynth + GeneralUser-GS/ColomboGMGS2 双音色库（主库缺失自动回退备选）；路径三分级探测 OK/MISSING/BROKEN；缺失时明确报错并给出修复指引（退出码 7）；仅 `--dry-run` 允许 mock；`--soundfont`/`--fluidsynth` 可覆盖。
- **P1-3 批量生成完整实现**：`batch --count N [--seed S] [--chords-choices ...] [--style ...] [--variations] [--render] [--export] [--parallel]`；四维度随机化（和弦/节奏/风格/旋律变奏）；seed 派生 `seed*1000+i` 可复现；失败项隔离 + 单次重试；退出码 0/8/9；批次清单写入 metadata.json。
- **P2-5 输出管理**：默认 `<root>/<project>/<YYYYMMDD>/{style}_{bpm}_{seed}_{seq}.{ext}` 命名（跨运行防覆盖）；每次运行产出 metadata.json（参数/seed/耗时/版本/路径）；`[output] layout` 双档（project-date/legacy）保兼容。
- **P2-1 DSP 调优**：render 后、export 前独立 DSP 阶段：峰值归一化 -1dBFS、淡入 100ms/淡出 300ms（0–5000ms 校验）、可选 EQ/压缩（默认关）、Suno 导出链恒禁混响；非法参数显式报错。
- **P2-2 乐理规则**：`music_theory/` 包（平行五度/八度检测、声部交叉、二声部对位、和弦转位、节奏型库 ≥6 内置 + 自定义 JSON/字符串）；全部默认关闭，不破坏 P0 输出。
- **P2-4 预设风格库**：`styles/` 包 + 流行/摇滚/电子/古典 4 基线 TOML（BPM/乐器/节奏型/旋律特性/和弦偏好/DSP 默认）；自定义 TOML/JSON 注册；与 `--style`/batch 联动。
- **P2-3 工程化**：日志分级（`--verbose`/`--quiet`/`--debug`）；`smartnotegen errors` 错误码表（新增 7/8/9）；依赖锁定（requirements/*.txt == 版本）；`scripts/install.bat` 一键安装；`scripts/build_package.ps1` PyInstaller 打包（含 module/ 资源说明）。
- **测试**：新增 env/output_manager/dsp/music_theory/styles/batch/error_codes/logging/render_m1/postprocess 测试；全量 197 用例全绿，覆盖率 ≥88%。

### 说明

- 本机已配置 `module/fluidsynth` 与双 SoundFont，`render` / `pipeline` 开箱即用；删除 `module/` 时报错误码 7 而非静默 mock。
- 版本号保持 0.1.0（`--version` 断言兼容，P2-3 版本规范化延后至测试断言许可后执行）。

---

## 子项目变更历史：videomaker

> `src/videomaker/` 是主包的独立 editable 兄弟包，**版本线独立于 `smartnotegen`**
> （错误码分段 10-14，主包占用 0-9）。
> 下列版本于 2026-08-21 → 2026-08-29 陆续交付，但**直到 2026-09-20 才随源码一并纳入版本控制**
> （此前只存在于工作区，见 `[Unreleased]` → 工程化）。
> 各版本完整交付报告见 `docs/reports/videomaker-*.md`。

### [0.3.0] - 2026-08-29（多格式输入 + 多轨混音 + 分轨可视化）

- **多格式输入**：WAV/FLAC/OGG 用 soundfile 直读；**MP3 由 soundfile 0.14 原生支持**
  （无需外部转换）；**MIDI 复用主包 `FluidSynthRenderer`** 渲染为临时 WAV
  （`module/fluidsynth/bin` + GeneralUser-GS.sf2 真实引擎）。
- **多轨混音**：`render a.wav "bass.mid:gain=0.8:pan=-0.3" melody.mp3` 多文件自动进多轨模式；
  重采样统一 44.1k → 长度对齐 → 轨道增益 + **等功率声像定律** → **峰值归一化 -1 dBFS**；
  双产物 `xxx.mp4` + `xxx.mix.wav`（混音可直接作纯音频发布）。
- **分轨可视化**（`--style tracks`）：垂直排列每轨频谱，HSL 色环区分轨道，**逐轨独立归一化**
  （弱轨也清晰可见），顶部显示轨名。
- **滚动波形**（`--style waveform_scroll`）：播放头居中 + 预计算波形查表滑动窗口
  （补上 v0.2 推迟的 W4）。
- **测试**：`test_videomaker.py` 30 例 + `test_videomaker_v03.py` 26 例 = **56 例全通过**。
- **已知限制**：Windows 绝对路径含盘符（`C:\...`）不支持 `:gain=` 冒号参数语法
  （冒号被解析切分），需用相对路径或程序化 `TrackSpec`；`tracks` 样式建议 ≤6 轨。

### [0.2.1] - 2026-08-23（rawvideo 提速 + Logo 水印 + multi 批量）

- **rawvideo 管道**（本版最关键改造）：FrameEngine 由「PIL 逐帧落盘 PNG → ffmpeg 读文件序列」
  改为 **PIL 帧 numpy→bytes 直写 ffmpeg stdin**，零中间文件——
  3s 音频 21s → **0.8s（提速 26 倍）**。
  配套修复：ffmpeg 进度日志量大，**stderr 不排空会填满缓冲导致 `proc.stdin.write()` 阻塞死锁**，
  须加 daemon 线程持续 drain。
- **Logo/水印**（`--logo` / `--logo-pos`）：ffmpeg 路径走临时 PNG overlay 双输入，
  PIL 路径走 `alpha_composite` 四角合成；支持四角定位 + 透明度 + 尺寸比例。
- **multi 批量命令**：一个音频一次产出多平台视频，复用同一次音频分析（发布闭环）。
- **规避的 3 个新版 ffmpeg 坑**：① `-loop 1` + `-shortest` 死锁（改单帧图片输入 + 链尾
  `format=yuv420p`）；② overlay 输入顺序必须为 `[底图][叠加层]`，反了会把 logo 当底图；
  ③ x264 拒绝奇数尺寸（yuv420p 要求，logo 缩放强制 `//2*2`）。
- **测试**：`test_videomaker.py` 33 例全通过（25 → 33，新增 logo 相关）。

### [0.2.0] - 2026-08-23（创意层激活）

- 起因：v0.1.0 审计发现 **PIL 创意层是 100% 死代码**——用户请求
  `circular_spectrum` / `reactive` 时实际静默回退到 ffmpeg 普通波形。
- **双引擎路由**：`waveform` / `spectrum` → ffmpeg 原生（showwaves / showspectrum）；
  `circular_spectrum` / `reactive` → **PIL FrameEngine**（前者 ffmpeg 无对应滤镜，
  后者需 RMS/onset 驱动）。
- **预计算架构**（性能核心）：`analyze()` 一次产出频谱矩阵 + RMS 包络 + 波形 + onset，
  注入全帧共享的 `VisualContext` 查表渲染（原先每帧重算全量 STFT）。
  配套 PIL 半分辨率渲染（`render_scale=0.5`）+ ffmpeg lanczos 上采样。
- 打通**背景叠加链路**（统一产出 PIL Image 走 overlay 双输入）与**文字叠加/标题卡**
  （微软雅黑；Windows 盘符冒号需转义为 `C\:/Windows/Fonts/msyh.ttc`）。
- **测试**：`test_videomaker.py` 25 例通过；全量回归 280 测试无失败。

### [0.1.0] - 2026-08-21（包骨架）

- 建立独立包骨架与 Typer CLI（`render` / `presets` / `config` / `version`）。
- **ffmpeg 渲染引擎**（showwaves / showspectrum）+ PIL 创意层基础实现。
- **4 套平台预设**：douyin(9:16) / youtube(16:9) / instagram(1:1) / official(4:5)。
- **4 种视觉效果**：waveform / spectrum / circular_spectrum / reactive。
- 输出管理（路径规划 + metadata.json）与**错误码体系 10-14**。
- **新版 ffmpeg 滤镜语法适配**：`mode=single` 需移除、`color=` 参数不支持、
  必须显式绑定音频输入 `[0:a]showwaves=...[v]`。
- **测试**：4 风格 × 4 平台端到端全部通过。
