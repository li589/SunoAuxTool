# Suno 访问通道设计：Web 凭证（session）+ 中转站（media API）

> 状态：设计稿（2.5 候选）。2026-10-29 实测了中转站通道全链路；Web 凭证通道为待实现设计。

## 1. 背景与现状

Suno 官方 API 未开放，社区普遍走两条路：逆向 Web 前端接口（用浏览器登录态当凭证），
或经由聚合中转站付费调用。本项目已有的基础：

| 已有能力 | 位置 | 说明 |
|---|---|---|
| 官方明文下载三段式 | `download/suno_dl.py`（`post suno-dl`，v1.8.0） | CDN 直拼 → clip 载荷直链 → `GET /api/download/clip/{id}` 轮询；wav 走 `convert_wav` |
| Suno 逆向结论 | `output/ref/suno_analysis_report.md`（2026-10-09） | SW-Mango 播放加密还原、`/api/mango/rights` 密钥协议、下载/多轨/wav 转换全接口清单 |
| AI 清单闭环 | `compose/ai.py`（v2.3.0） | LLMClient 协议 + 环境变量注入 + fake provider——**同一套注入模式将复用到这里** |

## 2. 双通道对比（2026-10-29 实测）

| | 通道 A：中转站 `music` 模型 | 通道 B：Web 凭证直连 |
|---|---|---|
| 成本 | ¥0.48/条（实测），余额制 | 免费（吃账号额度） |
| 出片 | ~60 秒，**一次两条**（Suno 惯例双版本），133s/48kHz/立体声 MP3 | ~60-120 秒，同样双版本 |
| 实现 | 一个 HTTP 任务式客户端（半天） | 逆向维护成本持续存在 |
| 稳定性 | 中转站聚合多供应商（vendor 字段：APM），单点风险转移 | 接口随前端改版漂移，需持续跟进 |
| 合规/风险 | 正规付费 | 违反 Suno ToS，**有封号风险**；凭证是用户自己的账号 |
| 凭证 | API key | 浏览器 Cookie（手动导入） |

**结论**：A 为默认推荐通道；B 作为零成本备选实现，但只做"导入现成 Cookie"，
**不做**账号密码登录/验证码破解（安全与合规双重红线）。

### 通道 A 实测记录（dolphin.token6688.com）

```
POST /v1/media/generate   {"model":"music","prompt":"...","params":{...}}
   → {"code":200,"data":{"status":"pending","task_id":"task-..."}}
GET  /v1/tasks/{task_id}  → is_final / data[{type:"audio",url}] / billing.actual_cost_credits
   ~60s 出片；返回 2 条 CDN 明文 mp3（cdn.token6688.com），48kHz 立体声
POST /v1/chat/completions（同站）→ `compose ai` 已接（SUNOAUX_AI_*）
```

## 3. 通道 A 落地设计（`post relay-music`，P1，工作量小）

- 模块：`download/relay_music.py`，CLI `sunoaux post relay-music generate "描述" --wait`。
- 复用 `media/generate` + `tasks/{id}` 轮询（间隔 5s，上限 10 分钟）；
  产物直接落 `output/relay/<task_id>/v{n}.mp3`。
- 配置：`SUNOAUX_RELAY_API_KEY` / `SUNOAUX_RELAY_BASE_URL` / `SUNOAUX_RELAY_MUSIC_MODEL`（默认 `music`）。
- 错误码：复用 25（凭证缺失）/ 26（请求失败）；新增 47（任务超时/失败）可选。
- 与 `compose ai` 共享中转站 key 时允许 `SUNOAUX_RELAY_API_KEY` 缺省回落到 `SUNOAUX_AI_API_KEY`。

## 4. 通道 B 设计（`post suno-web`，P2）

### 4.1 凭证生命周期

```
浏览器登录 suno.com → F12 复制 Cookie（__session / token）
  → `sunoaux post suno-web login --cookie "<整串>"`（写入本地配置，0600）
  → 请求带 Cookie；收到 401/403 → 明确报"凭证过期，请重新导出"（不复用、不自动续期）
```

- 凭证只存本地用户配置（不进仓库、不进日志；日志脱敏为 `***`）。
- 不实现密码登录：验证码/2FA 无法可靠自动化，且是封号风险最高的行为。

### 4.2 API 面（P1 只读 → P2 生成）

| 能力 | 接口（逆向已知，见分析报告） | 分期 |
|---|---|---|
| 我的曲目列表 | `GET /api/me` / feed 类接口 | P1 |
| 单 clip 元数据 | `GET /api/feed/...`（载荷含 audio_url） | P1 |
| 下载 | **复用 `post suno-dl` 三段式**（已实现并验证） | P1 |
| 生成 | `POST /api/generate`（tags/prompt/instrumental/title）+ 轮询 clip 载荷 | P2 |
| 歌词/续写 | `/api/generate/lyrics` 等 | P3 |

P1 只读先行：零生成风险、立刻有用（把 Web 上已有的作品批量拉回本地），
且下载链路完全复用已验证的 `suno-dl`。

### 4.3 错误码

复用 25（凭证缺失）/ 26（请求失败）/ 21-24（下载转码段）；不新开段。

### 4.4 风险与对策

| 风险 | 对策 |
|---|---|
| 接口漂移 | 响应结构校验 + 明确报"接口可能已改版"，不静默吞错 |
| 封号 | 只做用户主动发起的操作；生成功能默认关闭（`--i-know-the-risk` 显式开启） |
| 凭证泄漏 | 本地存储 + 日志脱敏 + 文档明示"等同账号密码" |
| 中转站跑路 | 通道 A/B 互为备份，接口都收敛到同一个 `MusicProvider` 协议 |

## 5. 收敛点：MusicProvider 协议

两个通道最终都实现同一协议（模式照抄 `compose.ai.LLMClient`）：

```python
class MusicProvider(Protocol):
    def generate(self, prompt: str, *, instrumental: bool = True) -> list[Path]: ...
```

上层（未来的 `sunoaux post music "描述"` 聚合命令）只认协议，通道选择走
`--provider relay|web`，与 `compose ai --provider` 的用户体验保持一致。
