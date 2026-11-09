# 小天才 API 调研总结

> 本次任务的结论汇总。详细记录见 [`imoo-api.md`](imoo-api.md)，
> 地址清单见 [`imoo-api-list.md`](imoo-api-list.md)。
>
> 时间：2026-11-10

---

## 一句话结论

**想通过 HTTP API 让手机给手表发文字消息 —— 这条路不存在。**

文本消息走的是私有二进制协议（`gw.im.okii.com:8000`），不是 REST。
现有的 ADB 界面自动化方案仍是唯一可行的文本发送途径。

---

## 为什么抓包没抓到消息

这是本次最重要的发现，也是理解整件事的关键。

### 三个通道，只有一个是 HTTP

```
手机 App
   │
   ├── HTTP(S)  →  account.okii.com / watch.okii.com / zztfly.com
   │               ✓ 走系统代理，但 ✗ 无法解密（见下）
   │
   ├── 私有 TCP →  im3.okii.com:8002 / gw.im.okii.com:8000
   │               ✗ 不走 HTTP 代理（裸 socket 不受 http_proxy 影响）
   │
   └── 局域网   →  192.168.1.x
```

消息走第二条。所以 mitmproxy 里永远看不到消息内容 —— 不是配置错了，
是它根本不在 HTTP 层。

### 为什么 HTTPS 也解不开

即使只想看账号接口，也解不开：

| 尝试 | 结果 |
|---|---|
| 设 `http_proxy` 指向 mitmproxy | ✅ 手机流量确实过来了 |
| 安装 mitmproxy CA 证书 | ❌ 系统要求**指纹**验证，ADB 无法完成 |
| 假设装上了 | ❌ 仍然无效：App 的 `network_security_config` 只信任 `system` 证书，`targetSdk=36` |

第二条是硬限制：Android 7+ 的应用默认不信任用户安装的证书，
除非它自己声明了 `trust-anchors` 包含 `user`。这个 App 没有。

### 其它被排除的路

| 方案 | 为什么不行 |
|---|---|
| `run-as` 读 App 数据 | `package not debuggable` |
| 直接读 `/data/data` | 权限拒绝（无 root） |
| 命令行装证书 | `android.security.authorization` 需系统权限 |
| 写 `/system` | `erofs (ro)` 只读 |
| 从 APK 提取字符串 | DexHelper 加固，DEX 加密 |
| 手机侧 tcpdump | 未安装 |
| `api.watch.okii.com` 猜路径 | 网关是精确路由，400+ 端点里没有发消息的 |

---

## 实际拿到了什么

### 1. 完整的服务地址表

服务器自己给的（`GET /server/domain/list`，免鉴权）：

```json
{"watchDomain": "watch.okii.com", "chatDomain": "watch.okii.com",
 "ssoDomain": "account.okii.com", "imJoinDomain": "gw.im.okii.com:8000",
 "locationDomain": "location.watch.okii.com", "pointsDomain": "points.okii.com",
 "imBakcupDomainList": ["gw.im.okii.com:1883", ...]}
```

这份表比任何逆向文档都可靠。

### 2. 签名信封，已用真实服务器验证

`GET https://watch.okii.com/rsa` 返回 `code=000001` + RSA-1024 公钥，
证明请求格式正确。信封结构：

```
Base-Request-Param: {"accountId","appId":"0","deviceId","imFlag":"1",
                     "program":"android","registId","timestamp","token",...}
Eebbk-Sign:         UPPER(MD5(url + BRP + body + key))
body:               base64(AES-128-ECB(gzip(JSON)))
```

本仓库 `src/xtc_qq_bridge/api/` 已实现，157 项测试通过。

### 3. IM 接入点的协议结构（部分）

`gw.im.okii.com:8000` 不是 TLS、不是标准 MQTT，而是自定义协议。
已探明它下发的首帧：

```
37 99 | 03 02 | 02 00 | c8 0a
TLV tag=10 len=266  RSA-1024 公钥
TLV tag=11 len=128  固定数据块
TLV tag=12 len=3    01 00 01（指数 65537）
```

试过往回发加密的会话密钥，收到固定错误 `24 04 02 02 10 68`；
加上帧头后服务器转为等待（说明帧头对了，缺后续步骤）。

**还缺**：客户端首帧格式、密钥派生方式、鉴权时机、topic 结构。
这些必须抓一次真实连接才能确定。

---

## 现在的建议

### 继续用 ADB 方案

| 维度 | ADB | MQTT 逆向 |
|---|---|---|
| 现状 | 可用 | 未知步骤还很多 |
| 速度 | ~3 秒/条 | 可能更快 |
| 稳定性 | 高（界面不变就能用） | 协议变更即失效 |
| 风险 | 无 | 设备指纹校验、账号风控 |
| 维护 | 界面改版要适配 | 协议改版要重新逆向 |

**结论：不值得投入 MQTT 路线。**

### 如果只是想"推一句话给手表"

`POST /findWatch/setLeaveMessage` 是唯一不需要 IM 协议的途径。
但它不是聊天消息 —— 手表端不会以微聊气泡显示。

### 如果将来仍想推进

性价比最高的一步是：**用 PCAPdroid 抓一次真实发消息**。
它走 VPN 层，能抓裸 TCP，免 root。有了那份流量，
上面"还缺什么"的四项就都能确定。

---

## 交付物

| 文件 | 内容 |
|---|---|
| `docs/imoo-api-list.md` | API 地址清单（服务地址、400+ 端点、信封、能做什么） |
| `docs/imoo-api.md` | 完整调研记录（每条结论的验证方式） |
| `src/xtc_qq_bridge/api/` | 协议层实现（默认关闭） |
| `tests/test_api_client.py` | 157 项测试 |
| `tools/probe_im_gateway.py` | IM 接入点探测脚本（只读） |

全部在 `dev` 分支，未合入 `rc/1.0.0` 与 `main`。

---

## 隐私说明

本文档与代码中**不含**任何真实凭据、设备标识或账号信息。
抓包过程中的设备 ID、token 等敏感字段均未落盘。

`api.watch.okii.com` 的 `/health` 与 `watch.okii.com` 的 `/rsa`、
`/server/domain/list` 是公开端点，不含用户数据。

---

## 附：本次遇到的一个环境问题

调试过程中发现一个**与 API 无关、但会影响部署**的问题，记录在此以免重复排查。

### 现象

尝试向手表发中文时，注入失败。排查发现：

| 检查项 | 结果 |
|---|---|
| 设备上是否装了 ADBKeyboard | ❌ 没有 |
| 当前输入法 | 讯飞（`com.iflytek.inputmethod`） |
| `cmd clipboard` 是否可用 | ❌ `No shell command implementation` |
| `service call clipboard` | ❌ 用法不支持 |

也就是说：**这台机器上，中文注入只有 ADBKeyboard 一条路，而它没装。**

### 为什么没装上

项目自带 `assets/keyboardservice-debug.apk`（包名 `com.android.adbkeyboard`），
但荣耀 MagicOS 的安装流程有两道拦截：

1. 「来历不明的应用」→ 点「继续」
2. 「未经荣耀安全审核」安全页 → 点「安装」

第二步的「安装」按钮**点击无效**（ADB tap 到坐标正确，但页面不响应）。
多次尝试均停在原页，`adb install` 最终以
`INSTALL_FAILED_ABORTED: User rejected permissions` 结束。

这是系统的安全策略，属于**需要用户手动确认**的决策，ADB 无法绕过。

> 顺带澄清：设备上原本就有一个 `com.didjdk.adbhelper`（装于 2026-11-02），
> 那是另一个工具，不是 ADBKeyboard，不能替代。

### 影响与建议

| 影响 | 说明 |
|---|---|
| 中文发送 | 不可用（无 ADBKeyboard、无剪贴板通道） |
| 英文/数字发送 | 可用（`input text` 直接注入） |
| 读取与转发 | 不受影响 |

**建议**：在手机上手动完成一次 ADBKeyboard 安装（设置 → 安全 → 允许安装未知来源，
或临时关闭「纯净模式」）。装好后 `ime enable` + `ime set` 即可，
之后 ADB 中文注入就通了。

现有代码对此的处理是正确的：`_adbkeyboard_ready()` 探测不到就返回 False，
`input_text()` 会依次尝试 ADBKeyboard → 剪贴板 → keyevent，
全部失败时**如实上报**而不是假装成功。
