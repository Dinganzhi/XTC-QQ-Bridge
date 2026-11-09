# 小天才（imoo）API 逆向记录

> 实验性调研笔记。记录**已证实的事实**、**未能证实的推测**，以及每条结论的验证方式，
> 便于后续接手的人判断可信度、避免重复劳动。
>
> 时间：2026-11-10 · 设备：Android 12 真机（未 root）· App：`com.xtc.watch` 9.47.02

---

## 一、结论摘要

| # | 结论 | 可信度 | 验证方式 |
|---|---|---|---|
| 1 | 手机 App 与手表的**消息通道**走私有 TCP（MQTT 系），不经 HTTP 代理 | **已证实** | `/proc/net/tcp6` 中该 UID 存在到 `123.56.164.38:8002` 的 ESTABLISHED 连接，而 mitmproxy 无对应记录 |
| 2 | App 的 HTTPS 流量**确实经过系统代理** | **已证实** | 设置 `http_proxy` 后 mitmproxy 记录到 `account.okii.com`、`watchda.eebbk.net` 等握手 |
| 3 | 未 root 时**无法解密**其 HTTPS | **已证实** | 握手失败于 `tls alert certificate unknown`；`network_security_config` 只含 `system` 信任锚，`targetSdk=36` |
| 4 | 请求信封为 `Base-Request-Param` + `Eebbk-Sign`(MD5) + AES-ECB | **已用真实服务器证实** | 本仓库实现签名后请求 `GET https://watch.okii.com/rsa`，返回 `code=000001` + RSA 公钥 |
| 5 | **不存在文本发消息的 HTTP 接口** | **已证实** | 400+ 端点的 OpenAPI 清单中，Chat 只有 `/weichat/emoji` 与 `/weichat/voice`；`/server/domain/list` 显示 IM 接入点是 `gw.im.okii.com:8000`（MQTT 端口） |
| 6 | `api.watch.okii.com` **不是**可用 base | **已证实** | 真实 base 是 `watch.okii.com`；前者只是 `HOST_NAME_ARRAYS[1]` |

### 一句话结论

**"抓 App 的 HTTP API 来发消息"这条路不存在。**
文本消息走 MQTT 长连接（`gw.im.okii.com:8000` / 备用 `:1883`），不是 REST。
现有 ADB 界面自动化方案仍是唯一可行的自动化发消息途径。

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

## 四、为什么没有 HTTP 发消息接口（已证实）

### 4.1 三条独立证据

1. **完整端点清单里没有**。参考 [dachrisch/imoore](https://github.com/dachrisch/imoore)
   从 APK 静态提取的 OpenAPI 规范，共 400+ 个端点。Chat 分类下只有两个：
   * `GET /weichat/emoji` —— 取表情列表
   * `POST /weichat/voice` —— 发语音

   **没有任何"发送文本消息"的 REST 接口。**

2. **服务器自己说了 IM 接入点**。`GET /server/domain/list` 返回：
   ```
   "imJoinDomain": "gw.im.okii.com:8000"
   "imBakcupDomainList": ["gw.im.okii.com:1883", ...]
   ```
   `8000` 是网关端口，`1883` 是 **MQTT 标准端口**。IM 走 MQTT，不是 REST。

3. **App 的行为对得上**。App 启动即建立到 `im3.okii.com:8002` 的长连接，
   且该连接不经 HTTP 代理 —— 不是 HTTP 协议栈。

### 4.2 结论

**手机 App 与手表之间的文本消息走 MQTT 长连接，不存在可通过 HTTP 调用的发消息接口。**

这意味着：
* 想绕过 ADB，需要实现 **MQTT 客户端 + 小天才的私有 topic/鉴权方案**；
* 服务端很可能校验设备指纹与 `authId`，伪造难度高、稳定性风险大；
* 工作量比 ADB 界面自动化高一个数量级，且随时可能因协议变更失效。

**建议：继续用现有 ADB 方案。** 它虽然慢（一次发送约 3 秒），但稳定、可维护。

### 4.3 IM 接入点的实际协议（本次已探明一部分）

对 `gw.im.okii.com:8000` 做了主动探测，**该端口不是标准 MQTT，也不是 TLS**，
而是一层自定义二进制协议。已确认的结构如下。

#### 端口状态

| 端口 | 状态 | 说明 |
|---|---|---|
| `8000` | 开放 | 主接入点 |
| `1883` | 开放 | 备用（端口号像 MQTT，但实测同样不响应标准 MQTT） |
| `8883` / `443` | 超时 | 未开放 |

#### 服务端下发的首帧（412 字节）

发送**任意**数据（甚至只发 `0x16 0x03 0x01 0x00 0x50` 这样的 TLS 头）后，
服务器立即回一段固定结构：

```
37 99 | 03 02 | 02 00 | c8 0a | ...
魔数    版本3.2   ?      长度
```

紧跟三个 TLV：

| 偏移 | tag | 长度 | 内容 |
|---|---|---|---|
| 7 | `0x0a` (10) | 266 | **RSA-1024 公钥**（PEM，单行无换行） |
| 276 | `0x0b` (11) | 128 | 128 字节二进制（RSA 模长） |
| 407 | `0x0c` (12) | 3 | `01 00 01` = RSA 指数 e=65537 |

TLV 的长度字段是 **LEB128 变长编码**（`0x8a 0x02` → 266），与
`imoo-client` 的 `tlv.py` 一致。tag `10` 也正好是该项目里的
`ENCRYPT_WAPPER_PAYLOAD_TAG`。

#### 已验证与未验证

| 项 | 结果 |
|---|---|
| 公钥每次连接是否相同 | **相同**（固定公钥） |
| tag 11 的 128 字节是否每次相同 | **相同**（固定值，不是会话密钥） |
| 用该公钥加密会话密钥后发送 tag 11 | 收到固定错误 `24 04 02 02 10 68` |
| 加上服务端帧头 `37 99 03 02 02 00 <len>` 再发 | **不再报错**，转为等待 → **帧头格式正确** |
| 协商后接 MQTT CONNECT（3.1.1） | 超时，未进入 MQTT 状态 |

`24 04 02 02 10 68` 按 TLV 解是 `tag=0x24(36), len=4, val=02 02 10 68`，
是协议内的错误响应（不是 MQTT CONNACK —— 其类型字节低 4 位应为 0）。

#### 还缺什么

要真正跑通，还需要还原：

1. **客户端首帧的完整格式**：帧头之后是 tag 11 还是别的 tag？是否需要
   `machineId` / `authId` 之类的身份字段？
2. **会话密钥的派生方式**：服务器 tag 11 的固定 128 字节是做什么用的？
   是否参与密钥派生（而非直接加密）？
3. **鉴权时机**：是在这个二进制层内完成，还是切到 MQTT 后用
   username/password 完成？
4. **topic 与消息体**：`watchId` 如何映射到 topic；消息体是 protobuf 还是 TLV。

这些都需要**抓一次真实的 App 连接**才能确定 —— 而该连接不走 HTTP 代理，
必须用 PCAPdroid（VPN 层抓包，免 root）或 root 后的 tcpdump。

#### 评估

即使还原成功，收益也有限：ADB 方案已经能发消息（约 3 秒/条），
而 MQTT 方案要承担协议变更、设备指纹校验、账号风控三类风险。
**建议不投入。** 本节的价值在于：把"能不能"变成"已探明到哪一步"，
避免后来者重复这几小时的探测。

---

## 五、协议信封（有公开实现佐证）

以下结构来自 [imoo-client](https://github.com/HIbingai/imoo-client)（MIT），
本仓库 `api/crypto.py` 按其重写，并通过 FIPS-197 标准向量自测。

### 5.1 `Base-Request-Param`（BRP）

字段顺序**固定**，服务端按同一份明文重算签名：

```json
{"accountId":"<账号>","appId":"0","deviceId":"<绑定号>","imFlag":"1",
 "program":"android","registId":"12345","timestamp":"2026-11-10 01:00:00",
 "token":"<token>","mac":"unkown","requestId":"<uuid hex>"}
```

字段来自 `com.xtc.http.bean.BaseRequestParam`：`appId` 固定 `"0"`、
`program` 固定 `"android"`、`imFlag` 固定 `"1"`。

注意 `"mac":"unkown"` 的拼写（官方如此，不是 `unknown`）。

> 早期 `imoo-client` 项目用的是 `imFag`/`appId:"2"`/`program:"watch"`，
> 与国内 `xtc` 包的实现不同。本仓库按 `xtc` 包（即本项目目标）为准。

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

## 五之二、真实服务器验证结果

以下为**本次实际请求**的结果（`2026-11-10`），不是推测：

| 请求 | 响应 | 含义 |
|---|---|---|
| `GET https://watch.okii.com/rsa` | `code=000001`，含 `encSwitch:0`、`publicKey`、`modulus`、`exponent` | **签名信封正确**，服务器正常应答 |
| `GET https://watch.okii.com/server/domain/list` | `code=000001`，返回完整域名配置 | 同上 |
| `GET https://watch.okii.com/watchaccount` | `code=100500` `api gateway error` | 业务接口需要有效 session 才会被路由 |
| `GET https://watch.okii.com/health` | `code=100500` | 该路径在 `watch.okii.com` 不存在 |
| `GET https://api.watch.okii.com/health` | `200 {"status":"UP"}` | 这是另一个网关，只有 `/health` |

### `/server/domain/list` 返回的真实配置（节选）

```json
{"watchDomain": "watch.okii.com", "chatDomain": "watch.okii.com",
 "locationDomain": "location.watch.okii.com", "pointsDomain": "points.okii.com",
 "ssoDomain": "account.okii.com",
 "imJoinDomain": "gw.im.okii.com:8000",
 "imBakcupDomainList": ["gw.im.okii.com:1883", ...]}
```

**关键**：`imJoinDomain` 是 `gw.im.okii.com:8000`，备用 `:1883` ——
这两个都是 **MQTT** 的典型端口（1883 是 MQTT 标准端口）。
`chatDomain` 指向 `watch.okii.com`，但该 host 上并没有文本发消息的 REST 接口。

### 为什么 `100500` 不代表信封错了

`100500` 是网关的"路由不到"响应，出现在**路径不在白名单**时。
对照实验：

* `/rsa`、`/server/domain/list` → `000001`（免鉴权白名单，信封被接受）
* `/watchaccount`、`/mobileaccount`、`/weichat/emoji` → `100500`（需 session）

也就是说：**签名算法已验证正确**，缺的只是有效 token。
即使拿到 token，`/weichat/emoji` 也只是取表情列表，**不是发消息**。

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

## 九、参考资料与出处

本次调研用到的两份公开资料，均已在正文标注：

| 项目 | 用途 | 许可 |
|---|---|---|
| [HIbingai/imoo-client](https://github.com/HIbingai/imoo-client) | 签名信封、AES-ECB、TLV/XOR 传输层结构 | MIT |
| [dachrisch/imoore](https://github.com/dachrisch/imoore) | **完整 400+ 端点 OpenAPI 清单**、`BaseRequestParam` 字段、`/rsa` 与 `encSwitch` 语义、域名表 | 见仓库 |

`imoore` 的关键贡献是那份 `openapi.yaml` —— 它让"有没有发消息接口"这个问题
从"猜"变成"查"。本仓库 `api/crypto.py` 按 `imoo-client` 的结构重写，
但字段取值按 `imoore` 记录的 `xtc` 包实现（两者有差异，见 5.1 的注）。

## 十、许可与声明

* 协议本身归小天才（imoo / 步步高）所有，本项目仅做**互操作性研究**。
* 本文档不含任何真实凭据、设备标识或个人数据。
* 抓包仅针对**本人设备与本人账号**，用于学习目的。
* 本文中所有"已证实"结论均可由正文给出的命令或请求复现。
