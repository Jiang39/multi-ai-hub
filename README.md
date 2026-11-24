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

## 已知限制

- 新平台（非 WorkBuddy）经 Anthropic 端点以**非流式聚合**返回，未实现逐 token SSE。
- COSY 系平台（Qoder / QwenWork）是否接受 OpenAI tools 字段未逐一实测；小浣熊 / Loomy 的工具调用已验证。
- 协议依赖具体客户端版本，客户端升级后可能需要更新 `protocols.py` 中的版本头与签名参数。
- 签到由外部调度器/计划任务负责，本项目只读取签到状态与收益。

## 免责声明

本项目仅用于**你本人合法拥有账号**的个人自动化场景，可能违反相关平台的服务条款。作者不对账号封禁、额度损失或任何使用后果负责。请自行评估合规风险后再使用。
