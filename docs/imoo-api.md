# 小天才（imoo）API 逆向记录

> 实验性调研笔记。记录**已证实的事实**、**未能证实的推测**，以及每条结论的验证方式，
> 便于后续接手的人判断可信度、避免重复劳动。
>
> 时间：2026-11-10 · 设备：Android 12 真机（未 root）· App：`com.xtc.watch` 9.47.02

---

## 一、结论摘要

| # | 结论 | 可信度 | 验证方式 |
|---|---|---|---|
| 1 | 手机 App 与手表的**消息通道**走 `im3.okii.com:8002` 裸 TCP，不经 HTTP 代理 | **已证实** | `/proc/net/tcp6` 中该 UID 存在到 `123.56.164.38:8002` 的 ESTABLISHED 连接，而 mitmproxy 无对应记录 |
| 2 | App 的 HTTPS 流量**确实经过系统代理** | **已证实** | 设置 `http_proxy` 后 mitmproxy 记录到 `account.okii.com`、`watchda.eebbk.net` 等握手 |
| 3 | 未 root 时**无法解密**其 HTTPS | **已证实** | 握手失败于 `tls alert certificate unknown`；`network_security_config` 只含 `system` 信任锚，`targetSdk=36` |
| 4 | 请求信封为 `Base-Request-Param` + `Eebbk-Sign`(MD5) + AES-ECB 请求体 | **有公开实现佐证** | 见 [imoo-client](https://github.com/HIbingai/imoo-client)；本仓库 `api/crypto.py` 按其重写并通过标准向量 |
| 5 | 消息收发对应的具体 HTTP 路径 | **未证实** | 网关为精确路由，枚举 20+ 常见路径全部 404 |
| 6 | 是否存在可直接调用的"发消息"HTTP 接口 | **存疑** | 见下文「四、为什么可能没有 HTTP 发消息接口」 |

---

## 二、网络拓扑与抓包条件

### 2.1 三个不同的通道

```
┌──────────┐   ADB(5555)    ┌──────────┐
│  PC      │◄──────────────►│ 手机     │
└──────────┘                └────┬─────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
        HTTP(S) 走代理      裸 TCP 不走代理      局域网直连
              │                  │                  │
      account.okii.com    im3.okii.com:8002    192.168.1.x
      watchda.eebbk.net   (IM 长连接)          (同网段设备)
```

关键点：**系统 `http_proxy` 只影响 HTTP(S) 客户端**（OkHttp/HttpURLConnection 等），
对裸 TCP socket 无效。所以 mitmproxy 能看到账号接口，看不到消息接口。

### 2.2 抓包为什么失败

| 环节 | 结果 | 原因 |
|---|---|---|
| 代理连通 | ✅ 成功 | `192.168.192.210:8080` 手机可达 |
| HTTP 明文 | ✅ 抓到 | `cfgc.zztfly.com/dm` 等返回明文 JSON |
| HTTPS 解密 | ❌ 失败 | 客户端报 `certificate unknown` |
| 安装用户 CA | ❌ 被阻断 | 系统要求**指纹**验证，ADB 无法完成 |
| 即使装上 | ❌ 仍无效 | `targetSdk=36` + 配置只信任 `system` |

`res/8GD.xml`（network security config）解析出的字符串只有
`system`、`127.0.0.1`、`base-config`、`trust-anchors`，**没有 `user`**。
这是 Android 7+ 的默认行为，也意味着"装证书"这条路对 `targetSdk>=24` 的应用本来就不通。

### 2.3 其它被排除的路径

| 方案 | 结论 |
|---|---|
| `run-as` 读 App 数据 | ❌ `package not debuggable` |
| 直接读 `/data/data/com.xtc.watch` | ❌ 权限拒绝 |
| `cmd android.security.authorization` 装证书 | ❌ `Failed transaction`（需系统权限） |
| 写 `/system` 分区 | ❌ `erofs (ro)` 只读 |
| 静态提取 DEX 字符串 | ❌ DexHelper 加固（`libDexHelper.so`），DEX 加密 |
| 手机侧 `tcpdump` | ❌ 未安装 |
| 手机侧 `nc` | ❌ toybox 无此命令 |

---

## 三、已抓到的域名清单

### 3.1 小天才自有（`okii.com`）

| 域名 | 用途（推断依据） |
|---|---|
| `api.watch.okii.com` | **API 网关**。实测 `/health` → `{"status":"UP"}`，其余精确路由 404 |
| `im3.okii.com` | **IM 长连接**，端口 `8002`，裸 TCP |
| `im.okii.com` / `chat.okii.com` | 备用/分地域 IM 入口 |
| `account.okii.com` | 账号登录 |
| `watch.okii.com` / `static.watch.okii.com` | H5 页面与静态资源 |
| `watchda.eebbk.net` | 数据上报（启动即连，12 次/次启动） |
| `watchcdn.okii.com` | CDN |
| `api-module` / `store-module` / `location-module` 等 `*-module.okii.com` | 微服务分域 |

### 3.2 第三方风控与统计（`zztfly.com` / `flysleep.cn`）

| 域名 | 用途 |
|---|---|
| `cfgc.zztfly.com` | **配置下发**。`/dm?appkey=...` 返回明文域名映射表 |
| `upc.zztfly.com` | 上报，请求体为自定义二进制 |
| `log-auth.zztfly.com` | 日志，带 `sign` 头 |
| `cdn-api-auth.zztfly.com` | CDN 鉴权 |
| `api-auth.zztfly.com` | 风控 API |

`appkey` 为 `3a47312520fba`（所有请求共用，非用户凭据）。

### 3.3 网关特征

`api.watch.okii.com` 的 404 响应体是：

```json
{"error_msg":"404 Route Not Found"}
```

这是 **Kong / APISIX** 一类网关的典型输出，说明路由表是**精确匹配**，
不存在"猜一个前缀就能命中"的可能。`/actuator*` 直接断连（被 WAF 拦）。

---

## 四、为什么可能没有 HTTP 发消息接口

这是本次调研**最重要也最需要后续验证**的判断。

### 4.1 观察到的证据

1. App 启动后立刻建立到 `im3.okii.com:8002` 的**长连接**并保持。
2. 该连接**不经过 HTTP 代理**，说明它不是 HTTP(S) 协议栈。
3. `imoo-client` 项目提供了 `transport.py`：`ENCRYPT_WAPPER(32)` 帧、XOR 会话密钥、
   RSA-PKCS1 包裹密钥 —— 这是**自定义二进制协议**的典型结构，不是 REST。
4. 网关枚举 20+ 条"发消息"风格路径，全部 404。

### 4.2 推论

手表与手机之间的消息**很可能**走 `8002` 上的私有长连接（TLV 帧 + XOR + RSA 协商），
而 `api.watch.okii.com` 只承载账号、配置、设备管理等**非实时**操作。

如果这个推论成立，那么"抓 App 的 HTTP API 来发消息"这条路**本身就不存在**，
需要改为实现 `8002` 的私有协议 —— 难度和工作量都高一个数量级
（需要完整还原握手、鉴权、帧格式、心跳、序列号等，且服务端可能校验设备指纹）。

### 4.3 如何验证（留给后续）

| 方法 | 说明 | 难度 |
|---|---|---|
| PCAPdroid | 手机端免 root 抓包，走 VPN 层，能抓裸 TCP | 低 |
| `adb shell` + `strace` | 需 root | 高 |
| 端口转发 + 本机监听 | `adb forward` 只能转手机本地端口，不能拦出站 | — |
| 分析 `libDexHelper` 解密后的 DEX | 需脱壳 | 高 |
| 直接对着 `8002` 发 TLV 帧试探 | 需要先拿到握手公钥 | 中 |

---

## 五、协议信封（有公开实现佐证）

以下结构来自 [imoo-client](https://github.com/HIbingai/imoo-client)（MIT），
本仓库 `api/crypto.py` 按其重写，并通过 FIPS-197 标准向量自测。

### 5.1 `Base-Request-Param`（BRP）

字段顺序**固定**，服务端按同一份明文重算签名：

```json
{"imFag":"1","appId":"2","program":"watch","timestamp":"2026-11-10 01:00:00",
 "registId":"12345","deviceId":"<绑定号>","token":"<chipId>",
 "accountId":"<账号>","mac":"unkown","requestId":"<uuid hex>"}
```

注意 `"mac":"unkown"` 的拼写（官方如此，不是 `unknown`）。

### 5.2 `Eebbk-Sign`

```
Eebbk-Sign = UPPER(MD5( uri + BRP明文 + body明文 + aes_key ))
```

对**明文**计算，不是密文。`uri` 是完整 URL（含 scheme 与 host）。

### 5.3 请求体加密（v3）

```
body → gzip → AES-128-ECB(PKCS7) → base64
```

配套头：

```
Eebbk-Key-Id: <keyId>
Eebbk-Key:    <eebbkKey>
encrypted:    encrypted
content-encoding: gzip
```

`encrypt_key` 形如 `keyId:0123456789abcdef:eebbkKey`（中间必须 16 字节）。
为空时走 **v0 明文模式**（早期固件）。

### 5.4 响应

响应体同样加密（v3）：`base64 → AES-ECB → gunzip → JSON`。

```json
{"code":"200","desc":"","data":{...}}
```

成功码：`200`（okii 域）、`000001`（tiancaixing 域）、`0`。

---

## 六、本仓库的实现状态

| 文件 | 内容 | 状态 |
|---|---|---|
| `src/xtc_qq_bridge/api/crypto.py` | BRP / 签名 / AES-ECB / 纯 Python AES 兜底 | ✅ 自测通过 |
| `src/xtc_qq_bridge/api/client.py` | 签名 GET/POST、响应解密、结构化错误 | ✅ 自测通过 |
| `src/xtc_qq_bridge/api/__init__.py` | 包导出与来源声明 | ✅ |
| `tests/test_api_client.py` | 协议层单测 | ✅ |

**默认不启用**：`api.enabled` 缺省为 `false`，此时任何调用立即返回
`kind="disabled"`，不发起网络请求。现有 ADB 链路完全不受影响。

---

## 七、凭据获取（未完成）

调用真实接口需要四个值：`bind_number`（绑定号）、`chip_id`、`account_id`、`encrypt_key`。

| 值 | 可能来源 | 本次结果 |
|---|---|---|
| `bind_number` | App「我的 → 关于手表」页显示 | ❌ 界面被指纹弹窗阻塞 |
| `chip_id` | App 私有存储 / 登录响应 | ❌ 无 root 不可读 |
| `account_id` | 登录响应 | ❌ 同上 |
| `encrypt_key` | 登录响应下发的 `encryptKey` 字段 | ❌ 同上 |

**结论：在无 root、且系统级指纹弹窗无法用 ADB 通过的前提下，无法取得凭据。**
后续若要做真实联调，需要：
1. 用户在手机上手动完成一次指纹验证（安装证书），或
2. 用户手动从 App 界面抄下绑定号，并抓一次登录响应（需已装好证书）。

---

## 八、给后续接手者的建议

1. **先确认"有没有 HTTP 发消息接口"**，再决定投入方向。用 PCAPdroid 抓一次完整
   "手机发消息给手表"的过程，看是否有明文 HTTP(S) 请求。这是最高性价比的一步。
2. 若确认只有 `8002` 私有协议，评估是否值得做 —— 它比 ADB 界面自动化复杂得多，
   且服务端可能校验设备指纹，稳定性风险高。
3. 现有 ADB 方案虽然慢（一次发送约 3 秒），但**稳定可用**，不建议为新方案牺牲它。
4. 本仓库的 `api/` 包可以保留：即使最终不走 HTTP，签名层代码在调试其它 okii 接口时仍有用。

---

## 九、许可与声明

* 协议结构参考 [HIbingai/imoo-client](https://github.com/HIbingai/imoo-client)（MIT）。
* 协议本身归小天才（imoo / 步步高）所有，本项目仅做**互操作性研究**。
* 本文档不含任何真实凭据、设备标识或个人数据。
* 抓包仅针对**本人设备与本人账号**，用于学习目的。
