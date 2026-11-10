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
| 代理连通 | ✅ 成功 | 抓包机在同一虚拟局域网内，手机可直连其 8080 端口 |
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

### 4.2.1 接收方向：同样没有 HTTP 接口

"能不能发"和"能不能收"是两件事，这里单独查了一遍。

**逐类清点 184 个 GET 接口**（GET 通常是查询/拉取语义），按分类看：

| 分类 | 数量 | 与消息有关？ |
|---|---|---|
| Chat Messaging | 1 | 只有 `GET /weichat/emoji`（取表情列表） |
| Group Chat | 3 | 群列表、群成员、群文件下载 —— **没有"取群消息"** |
| Notifications | 3 | 系统通知/更新提示，不是聊天消息 |
| Social | 1 | `content/query` 是内容推荐流，不是对话 |
| 其余 176 个 | — | 定位、健康、通讯录、闹钟、上课禁用、OTA、支付等 |

另外清点了 7 个"POST 但语义是查询"的接口，全部是令牌获取与校验类
（图片/视频传输令牌、短信码校验、密码强度校验），无消息相关。

**结论：接收消息同样没有 HTTP 接口可调。** 收和发走的是同一条长连接，
不存在"只能发不能收"或"只能收不能发"的单向 HTTP 通道 —— 两个方向都没有 HTTP 入口。

**一条实测记录（供后来者省事）**：不能用"状态码差异"来枚举端点。
网关**先校验参数、再决定是否路由**，所以真实路径与虚构路径返回完全一样：

```
POST /weichat/emoji                    -> 000007 request parameter is invalid.
POST /notice/wear/123456               -> 000007 request parameter is invalid.
POST /group-chat/familyGroup/groups    -> 000007 request parameter is invalid.
POST /totally-bogus-path-xyz           -> 000007 request parameter is invalid.
```

因此判断"有没有某接口"只能依据端点清单本身，不能靠试探响应码。

### 4.2.2 修正：首帧的触发条件不是"发任意数据"

第 4.3 节早先写作"发送任意数据后服务器立即回一段固定结构"，这次复核发现
**触发条件比那更窄**，且指向一个不同的结论。

逐字节试验的结果：

| 发送内容 | 长度 | 是否回 412 字节首帧 |
|---|---|---|
| `16` | 1 | 否 |
| `16 03` | 2 | 否 |
| `16 03 01` | 3 | 否 |
| `16 03 01 00` | 4 | 否 |
| `16 03 01 00 50`（TLS 头） | 5 | **是** |
| `AAAAA` | 5 | 否 |
| `37 99 03 02 02 00 c8`（自家帧头） | 7 | 否 |
| 单字节 `00` | 1 | 否 |
| MQTT CONNECT 报文 | 10 | 否 |
| 连接后静默 | 0 | 否 |

也就是说：**只有发送"长度 ≥5 且以 `16 03` 开头"的数据**才会得到首帧，
这正好是 TLS ClientHello 的前缀。服务器在这个端口上做了**协议嗅探**：
认 TLS 握手头，然后回一段**不是 TLS** 的自定义帧。

用同样的判据复核了 `im3.okii.com:8002`（App 实际连的那个）：发 TLS 头、
发自家帧头、发 MQTT CONNECT、静默，**四种都不回任何数据**。

这说明 `8000` 与 `8002` 是两套不同的入口：`8000` 有一个对外的协议探测层，
`8002` 则完全沉默、只认真正的设备首包。所以第 4.3 节里那条 412 字节首帧
**只能用来确认"这个端口是自定义二进制协议"，不能用来推进握手** ——
它更像是给非设备客户端的一个统一应答，与真实会话无关。

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

#### 用 imoo-client 的编码器重试（2026-11-10 第二轮）

拿到 `imoo-client` 的 `tlv.py` / `transport.py` 后，用它的编码器重新解析首帧，
纠正了第一轮的三个误判。

**纠正 1：tag 11 不是"神秘数据块"，就是公钥的 modulus**

第一轮看到 tag 11 是 128 字节、每次相同，以为用途不明。实测比对：

```
PEM 公钥的 n == tag 11 的内容   -> True
PEM 公钥的 e == tag 12 的内容   -> True
```

服务器把公钥拆成三份下发（PEM 给人看，n/e 给程序直接用），
省掉客户端解析 PEM 的麻烦。这也解释了为什么它恒定不变 —— 它是公钥本身。

**纠正 2：首帧结构是"7 字节头 + 纯 TLV 序列"**

逐字节对齐后确认：

```
37 99 | 03 02 | 02 00 | c8 | <tag10: 公钥 PEM> <tag11: n> <tag12: e>
 ↑魔数   ↑版本 3.2      ↑标志
```

验证方式：从偏移 7 开始连续解三个 TLV，总长正好 405，
加上 7 字节头 = 412，与首帧长度**完全吻合**（`412 == 412`）。

那个 `c8` 不是 TLV 标签 —— 它被解码器读成 `tag=10, frame=0x40`，
但紧跟着的 `0a 8a 02` 才是真正的 `tag=10, len=266`。所以 `c8` 是帧标志字节。

**纠正 3：`transport.py` 的 tag32 封装不适用于这个端口**

`imoo-client` 的 `wrap_encrypted()` 生成 `tag32 构造帧{ tag10 = XOR(inner) }`。
照它发出去，服务器直接断开（`CLOSED`）。

#### 服务器行为规律（已确认）

穷举了帧结构、标签、消息类型字节后，规律非常稳定：

| 发送内容 | 服务器反应 |
|---|---|
| 不带 7 字节头 | **立即断开**（CLOSED） |
| 带 7 字节头 + 任意内容 | **保持连接、不回应**（TIMEOUT） |

扫描了 tag 1~64、消息类型字节 `0x00~0xf8`、有无前置密钥帧、
单帧与连发 —— **全部落在上述两类里，没有第三种反应**。

也就是说：服务器认帧头，但从不回应我发的任何业务帧。
它在等一个它认识的首包，而那个首包必须带设备身份。

#### 还缺什么（结论比第一轮更明确）

不是"帧格式不对"，而是**缺身份**。真实首包里应该包含：

1. `machineId` / `nDeviceId` / `authId` 之类的设备标识
2. 登录后的 `token`
3. 可能的 App 签名校验值（`App-Checksum`）

没有这些，服务器不会进入可交互状态 —— 它连错误码都不给，直接静默。

**这也意味着：即使把帧格式完全猜对，没有账号凭据仍然连不上。**
换句话说，这条路的前置条件是"先有 token"，而不是"先猜对协议"。

#### 一条没能走完的验证路

本想让 App 自己连一次、抓真实首包，但遇到两个障碍：

- App 实际连的是 `39.105.13.185:8002`（不是 `gw.im.okii.com:8000`），
  该端口对任何主动探测都直接断开，只认 App 的完整握手
- 手机直连该服务器，流量不经过抓包机，本机 `tcpdump` 抓不到；
  手机上也没有 `tcpdump`，且未 root

要用 PCAPdroid（VPN 层抓包）才能拿到那份首包。

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
