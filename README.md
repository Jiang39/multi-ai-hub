# Multi-AI Hub

把多个 AI 编程客户端的**额度查询、签到状态、统一管理面板与 OpenAI/Anthropic 兼容反代网关**收拢到一个本地服务里。纯 Python 标准库，无需第三方依赖。

## 功能

- **6 个平台统一接入**：WorkBuddy、Qoder 国际版、QwenWork、Loomy、小浣熊、TraeWork CN
- **统一面板**（默认 `127.0.0.1:8798`）：每账号额度卡、近 7/30 天用量、账号×模型限流矩阵、服务端备注名
- **统一网关**（默认 `127.0.0.1:8899`）：
  - Anthropic `/v1/messages`（原生协议，含流式、工具调用）
  - OpenAI `/v1/chat/completions`
  - `平台/模型` 前缀路由到指定平台；无前缀走 WorkBuddy 原生协议
- **账号池治理**：sticky 哈希选号、账号级 + 模型级双层冷却、429 自动换号、连续失败临时停用、prompt 缓存命中统计
- **网关日志按大小轮转 + 定时清理**（5MB / 3 份 / 14 天）

## 快速开始

```bash
# 1. 自检（联网冒烟，确认登录态与平台协议就绪）
python hub.py selftest

# 2. 启动面板
python hub.py serve --port 8798

# 3. 启动网关
python hub.py gateway --port 8899
```

查看接入状态：`python hub.py status`
手动跑签到：`python hub.py checkin`（或 `--platform qwenwork`）

### 账号自动纳管

`python hub.py sync` 会扫描所有平台的当前登录态与历史凭据，把新登录的账号自动加入
WorkBuddy 网关池与签到快照，并在面板中显示。它按 uid 去重、幂等，重复运行不会重复纳管。
建议配合 Windows 计划任务定时运行：签到 `python hub.py checkin` 每 4 小时一次
（幂等，已签到账号返回 ALREADY，不会重复领取），纳管 `python hub.py sync` 每 10 分钟一次，
这样新登录的账号最快 10 分钟内自动进入签到、网关与面板。

> 首次运行前，请把示例配置复制为真实配置并按你的环境填写：
> `config/workbuddy-gateway.example.json` → `config/workbuddy-gateway.json`，然后跑一次
> `python hub.py sync`：它会从本机客户端登录态（含新版 `$wbEncrypted` 加密凭据，自动
> 借 WorkBuddy runtime 解密）生成账号池与签到快照。这些运行时数据都在 `runtime/pool/`、
> `signin/accounts/`，属于隐私数据，不会也不应提交到仓库；缺少 `signin/accounts/` 时
> 账号昵称退化为 uin 前缀，不影响功能，跑一次 `sync` 即自动生成。

> 自检说明：`python hub.py selftest` 默认做联网冒烟（含一次极小 chat 请求，消耗可忽略）；
> 用 `python hub.py selftest --offline` 可跳过联网部分，完全无副作用。各检查项只验证
> 链路可用性，不校验账号数量（不同用户机器上的账号数本来就不同）。

## 客户端接入

把 Claude Desktop / cc-switch 的 `ANTHROPIC_BASE_URL` 指向：

```
http://127.0.0.1:8899
```

模型名两种写法：
- `claude-sonnet-1` 等别名 —— 走 WorkBuddy 原生协议（别名映射见配置 `models.map`）
- `xiaohuanxiong/sn-glm-5-3` —— 走指定平台的指定模型

## 支持的模型（以实测为准）

遵循统一的模型标准：DeepSeek 仅 V4.1-Flash（1017 优先）、GLM 仅 5.3、Kimi 3.1 Pro（无则 K3）。

| 平台 | DeepSeek | GLM | Kimi |
|---|---|---|---|
| Qoder 国际版 | ✓ | ✓ | ✓ |
| QwenWork | ✓ | ✓ | ✓ |
| 小浣熊 | ✓ | ✓ | ✓ |
| TraeWork CN | ✓ | ✓ | K3（3.1 Pro 套餐受限）|
| Loomy | ✓ | ✓ | —（该客户端无 Kimi）|

具体上游内部 key 与签名实现见 `core/protocols.py` 的 `VERIFIED_MODELS`。客户端版本漂移可能导致协议失效，升级后请先跑 `probe_chat.py` 复测。

## 项目结构

```
hub.py                 CLI / 面板 / 网关入口
core/
  adapter.py           平台适配器接口与 registry
  service.py           账号/额度/签到/限流/备注聚合层
  governance.py        账号池治理（sticky/冷却/健康）
  protocols.py         5 家平台的登录态解密、签名、chat、签到
  workbuddy_native.py  WorkBuddy 原生协议层（Anthropic 翻译/工具调用/流）
  workbuddy_data.py    WorkBuddy 数据层（自包含，历史函数迁移）
adapters/              6 个平台适配器
config/                网关配置（*.example.json 为脱敏模板）
runtime/               【不提交】账号池、日志、签到登录态
data/                  【不提交】面板运行期快照与备注
```

## 隐私与安全

- 令牌只存在于本机登录态与 `runtime/pool/` 的明文池文件，**不会**经由 API 返回给面板或客户端，也不打印到日志。
- `.gitignore` 已排除 `runtime/`、`data/`、真实配置与所有日志。提交前请再次确认没有把账号池文件、token 或个人信息带进版本库。
- 面板备注、积分历史等属于个人数据，同样在 `data/` 下被忽略。

## 后台运行（Windows）

面板与网关是常驻服务，建议注册为开机自启的计划任务，而不是留在终端前台：

```powershell
# 以管理员身份运行，自动按脚本所在目录探测 python / hub.py 路径
powershell -ExecutionPolicy Bypass -File scripts\install-windows-tasks.ps1
```

这会注册 4 个任务：面板（登录时启动，8798）、网关（登录时启动，8899）、
全平台签到（每 4 小时）、账号纳管（每 10 分钟）。卸载执行 `scripts\uninstall-windows-tasks.ps1`。
脚本对已存在的同名任务会跳过，不会覆盖你的现有配置。

随时确认服务状态：

```bash
python hub.py status --service   # 显示面板/网关是否存活、PID、HTTP 状态，以及客户端版本漂移
```

`status --service` 末尾会并列显示各平台【协议适配版本 vs 本机实测版本】。版本从本机客户端
安装目录只读探测（Qoder/Trae/Loomy 可识别，其余显示"未知"）；如需覆盖可在
`config/versions.json`（从 `config/versions.example.json` 复制）里配置 `_install_dirs`。
注意：**版本漂移不等于协议失效**——声明性版本头服务端通常宽容，真正判据是 probe。

主动探测协议是否仍可用（登录态、签名、模型 key，消耗极小）：

```bash
python hub.py probe           # 探测所有已登录平台
python hub.py probe --platform loomy
```

失败时退出码为 1，可用于计划任务告警（安装脚本已含每周一次的 `MultiHubProbe` 任务模板）。
客户端升级后若 probe 失败，再按 `config/versions.json` 调整适配值或更新 `protocols.py` 签名参数。

直接执行 `serve`/`gateway` 时若端口已被占用，会给出占用 PID 与换端口提示，而不是抛底层异常。

## 已知限制

- 新平台（非 WorkBuddy）经 Anthropic 端点以**非流式聚合**返回，未实现逐 token SSE。
- COSY 系平台（Qoder / QwenWork）是否接受 OpenAI tools 字段未逐一实测；小浣熊 / Loomy 的工具调用已验证。
- 协议依赖具体客户端版本，客户端升级后可能需要更新 `protocols.py` 中的版本头与签名参数。
- 签到由 `hub.py checkin` 与计划任务调度，WorkBuddy 另有独立签到脚本；两者都幂等。
- 网关重启后账号池选号游标（`POOL.cursor`）与内存中的 token 刷新缓存会重置；冷却状态已落盘不受影响。
- **解密依赖**：新版 WorkBuddy 的 `$wbEncrypted` 凭据借客户端自带 runtime（node 子进程）解密，
  因此需要本机安装 WorkBuddy 桌面端且 runtime 路径可发现；客户端升级加密方案后需同步更新解密逻辑。
  解密失败时该账号会被跳过，`status` 会反映账号数减少。

## 免责声明

> ⚠️ 本项目仅限**个人非商业、本人合法拥有账号**的自用场景。登录态解密、账号池轮询与自动签到
> 属于对客户端协议的逆向对接，**可能违反平台服务条款、被识别为异常使用，企业/团队账号风险更高**。
> 作者不对账号封禁、额度损失或任何使用后果负责；请自行评估合规风险后再使用。
