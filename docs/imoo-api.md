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

#### 4.3.0 先拿到服务端拓扑（`GET /server/domain/list`）

这是本次最省事的发现：**`/server/domain/list` 用 GET、不需要 token 就能调通**，
直接返回三套环境的完整域名表。方法对了，一次就通；之前用 POST 一直报 `000003`。

```http
GET /server/domain/list
Base-Request-Param: {"accountId":"...","appId":"0","deviceId":"<绑定号>",...}
Eebbk-Sign: <MD5>
-> {"code":"000001","desc":"success","data":[{...},{...},{...}]}
```

三套环境（`id` 1 正式 / 2 生产 / 4 模块测试）的关键域名：

| 字段 | 正式环境 | 说明 |
|---|---|---|
| `watchDomain` | `watch.okii.com` | 主 API |
| `chatDomain` | `watch.okii.com` | 聊天（与主 API 同域） |
| `locationDomain` | `location.watch.okii.com` | 定位 |
| `ssoDomain` | `account.okii.com` | 登录 |
| `gatewayDomain` | `api.watch.okii.com` | 网关 |
| **`imJoinDomain`** | **`gw.im.okii.com:8000`** | **IM 接入点** |
| **`imBakcupDomainList`** | **`['gw.im.okii.com:1883', 'gw.im.okii.com:8000']`** | 备用 |
| `imJoinGreyDomain` | `gw.beta.im.okii.com:8000` | 灰度 |

模块测试环境用 `gw.module.im.okii.com:8001`、`account-alpha.okii.com`、
`watch-module.okii.com` —— 这些可以做无副作用的联调。

#### 4.3.1 `1883` 与 `8000` 是同一个服务

`imBakcupDomainList` 里出现了 `1883`（MQTT 标准端口），一度以为协议就是 MQTT。
实测两个端口**行为完全一致**，解析到同一个 IP：

| 端口 | 解析 IP | 对 MQTT CONNECT | 对 TLS 头 | 对文本/静默 |
|---|---|---|---|---|
| `8000` | `8.141.31.235` | 回 `240402021068` | 回 412 字节首帧 | 无响应 |
| `1883` | `8.141.31.235` | 回 `240402021068` | 回 412 字节首帧 | 无响应 |
| `8883` | — | 超时 | — | — |
| `8001`（模块测试） | `123.57.136.4` | 开放 | — | — |

**结论：`1883` 只是端口号借用了 MQTT 的习惯，协议本身不是 MQTT。**

#### 4.3.2 对 MQTT CONNECT 的应答是固定拒绝

发标准 MQTT 3.1 / 3.1.1 / 5.0 的 CONNECT，无论 clientId 是空串、`test`、
绑定号、accountId、32 位随机串还是 50 字符长串，**返回的都是同一段 6 字节**：

```
24 04 02 02 10 68
```

换成标准 CONNACK 的视角看，首字节应为 `0x20`，这里是 `0x24`（低 4 位不为 0），
所以它**不是合法 CONNACK**。而且返回码不随任何输入变化 —— 这说明它是一层
**固定的协议拒绝应答**，与"clientId 不对"无关。

对照第 4.2.2 节的结论，`8000`/`1883` 上存在一个**协议嗅探层**：
认 TLS 头就给 412 字节自定义帧，认 MQTT 头就给这 6 字节，其余一律静默。

#### 4.3.4 报文结构（脱壳后从代码读出）

IM 的消息体是**带命令号的 TLV**，每个字段用注解标出 tag 号，整体是 varint 编码。
这是脱壳后`com/xtc/im/core/common/request/entity/` 下的真实定义，可直接照抄：

**发消息（命令号 9）**

```java
@CommandValue(9)
public class MessageRequestEntity extends RequestEntity {
    @TagValue(1)  int    RID;            // 请求序号
    @TagValue(2)  int    needResponse;   // 是否需要回执
    @TagValue(10) long   dialogId;       // 会话 ID
    @TagValue(11) long   imAccountId;    // 本机 IM 账号 ID
    @TagValue(12) long   registId;
    @TagValue(13) int    msgType;        // 文本/图片/语音…
    @TagValue(14) byte[] msg;            // 消息体
    @TagValue(15) String msgId;
    @TagValue(18) int    contentType;
    @TagValue(20) int    noSensitivity;
}
```

**登录（命令号 3）**

```java
@CommandValue(3)
public class LoginRequestEntity extends RequestEntity {
    @TagValue(1)  int    RID;
    @TagValue(10) long   registId;
    @TagValue(11) String registToken;        // 注意：又一个独立凭据
    @TagValue(12) long   sdkVersion;
    @TagValue(17) String deviceToken;
    @TagValue(21) long   timestamp;
    @TagValue(24) String baseRequestParam;   // 就是 §7.7 那个 BRP JSON
    @TagValue(26) String deviceId;
    @TagValue(18) String imSdkVersionName;
    @TagValue(19) int    pushType;
    @TagValue(20) int    platform;
}
```

其它命令号：`22` 取服务端公钥、`33` 设置加密参数、以及若干同步/回执类。

`dialogId` 与 `imAccountId` 存在本地库里：
`databases/app_database_v5.db` 的 `MobileAccount` 表里有一个 `imAccountInfo` 字段，
是 JSON，包含 `imAccountId` 和 `singlePushDialogId` —— 后者就是要发给手表的那条会话。

**编码格式**：TLV 的长度与 tag 都用 varint（7 位一组、低位在前），
见 `com/xtc/im/core/common/tlv/Georgia.java` 的 `Gibraltar()` / `Guyana()`：

```java
int i = bArr[0] & 0x7f;
for (int k = 1; k < len; k++) i |= (bArr[k] & 0x7f) << (k * 7);
```

**但字节级的帧头、组包与加解密都在 native 层**（`TCPConnection` 及各
`Entity.toByteArray()` 全部是 `JniLib.cX()` 空壳，方法体被加固进 so）。
也就是说：报文语义已经清楚，但要自己实现一个能连上的客户端，
还差帧头格式与那层加解密 —— 这两块只能靠动态调试或逆向 so，工作量不小。

#### 4.3.5 IM 通道的真面目：TLS 1.3 + 自定义帧

之前一直把 `gw.im.okii.com:8000` 当成 IM 入口，其实错了。
真机的 IM 连接是 **`im3.okii.com:8002`，标准 TLS 1.3**。抓包一看就清楚：

```
c2s: 16 03 01 00 f9 01 00 00 f5 03 03 ...     ← TLS ClientHello
     SNI = im3.okii.com
s2c: 16 03 03 ... 13 01                        ← ServerHello, TLS_AES_128_GCM_SHA256
```

`8000` 上那个 `37 99` 自定义帧是**另一个服务**（或者是对非客户端流量的探测应答），
和 App 实际用的通道无关。这一点走错方向浪费了不少时间。

**怎么看到明文的**：设备有 root，所以做了一次临时 MITM：

1. 生成 CA + `im3.okii.com` 的证书；
2. 用 `mount --bind` 把带 CA 的目录盖到 `/system/etc/security/cacerts`
   （`/system` 是只读 ext4，改不了，但 bind mount 不需要写）；
3. `adb reverse tcp:8443 tcp:8443` 把设备的本地端口通到 Linux；
4. `iptables -t nat -A OUTPUT -p tcp --dport 8002 -j DNAT --to-destination 127.0.0.1:8443`
   —— 注意要按**端口**匹配，不能按 IP：`im3.okii.com` 解析到一组会变的阿里云 NLB 地址；
5. 在 Linux 上跑一个最小 TLS 代理（`tools/tls_tap.py`），两侧都记录解密后的字节。

证书被接受了，说明 App **没有做证书锁定**，只有普通的系统信任链校验。

**帧格式**（这是最关键的收获）：

```
帧 = a0 | 命令号(1B) | varint(载荷长度) | 载荷
载荷 = 若干 TLV
TLV = tag(varint) | len(varint) | value
```

实测到的命令号：

| 命令 | 实体 | 说明 |
|---|---|---|
| `0x20`(32) | — | 数据帧，真正内容在 `tag10` 里 |
| `0x21`(33) | `EncryptSetRequestEntity` | 协商会话密钥：`t1`=RID、`t11`=128 字节 RSA 密文、`t10`=1 |
| `0x22`(34) | 同上应答 | `t1`=RID、`t2`=200（成功） |

真实抓到的几帧：

```
c2s 142B: a0 21 8a 01 | 01 02 03 e9  0b 80 01 <128B RSA 密文>  0a 01 01
s2c  11B: a0 22 08    | 01 02 03 e9  02 02 00 c8
c2s  59B: a0 20 38    | 0a 36 <54B 密文>
s2c  15B: a0 20 0c    | 0a 0a <10B 密文>
```

解读：`a0` 是帧头魔数，`0x21`=33 是 EncryptSet，`0x8a 0x01` 是 varint 长度 138，
载荷里 `01 02 03 e9` = tag1/len2/value 1001（RID），
`0b 80 01` = tag11/len128（EncryptSetRequestEntity 的 `encryptKey` 字段，正好 128 字节）。

**会话密钥的建立方式**：客户端随机生成 AES 密钥 → 用服务端的 RSA 公钥加密成 128 字节
→ 放进 `t11` 发过去 → 之后所有 `0x20` 数据帧的 `tag10` 都用这把 AES 密钥加密。

**到这里还差什么**：内层载荷的加密方式（AES 的 mode/padding）没有最终确认。
本次抓到的样本里，`tag10` 的密文在明文位置出现了 `35 36 33`（ASCII "563"）这样的片段，
既不像纯 AES 也测不出短周期 XOR，可能是「AES 之后又做了某种变换」或者
「结构是 protobuf、只有部分是密文」。要定论还需要：
把自己伪造的 RSA 公钥塞进服务端应答（拦截命令 22），
让客户端改用**我们的**公钥加密会话密钥，就能拿回密钥直接解。

不过**帧格式、命令号、TLV 编码这三样已经确定**，
自己实现客户端的骨架是清楚的，剩下是那把会话密钥怎么拿到。

#### 4.3.6 已经能脱离客户端：自建握手成功

这一步是整条线上最关键的确认：**不需要 App 也能建立 IM 会话。**

服务端在**无认证**的情况下就把 RSA-1024 公钥给出来。直接 `cmd 22` 即可：

```
发送: a0 16 04 | 01 02 03 e9           (cmd=22, tag1=RID=1001)
接收: 37 9d 03 | 01 02 03 e9           tag1 = RID 回显
              | 02 02 00 c8            tag2 = 200 成功
              | 0a 8a 02 <266B PEM>    tag10 = -----BEGIN PUBLIC KEY-----
              | 0b 80 01 <128B>        tag11 = 模数
              | 0c 03 010001           tag12 = 指数 65537
```

拿到公钥后，客户端可以**完全自己**完成会话协商：

1. 自己生成 16 字节 AES 密钥；
2. 用服务端公钥 RSA(PKCS#1 v1.5) 加密成 128 字节；
3. `cmd 33`（EncryptSet）上报：`t1`=RID、`t11`=密文、`t10`=1；
4. 服务端回 `t2=200`。

实测已经跑通，服务端 **接受了我们自己的密钥**：

```
[>] cmd33 EncryptSet 142B: a0 21 8a 01 | 01 02 03 ea 0b 80 01 11398bb2...
[<] 应答 11B: a0 22 08 | 01 02 03 ea 02 02 00 c8
[✓] t2=200
```

会话密钥只有我们知道，所以后续帧的加密、解密都能自己完成。
**这就是"脱离客户端"的判据**——不需要 App 参与握手，也不需要偷它的密钥。

心跳也验证通过：

```
[>] a0 07 04 | 01 02 29 23            (cmd=7, RID=3001)
[<] a0 20 04 | 0a 02 29 23            (cmd=32, tag10=10531)
```

发心跳有正常回应，说明帧格式、会话、编码三者都对。

#### 4.3.7 命令号总表（脱壳代码里的 `@CommandValue`）

| 命令 | 实体 | 用途 |
|---|---|---|
| 1 / 2 | RegistRequest / Response | 设备注册 |
| 3 / 4 | LoginRequest / Response | 登录（拿 registId、registToken） |
| 7 / 8 | HeartBeat | 心跳 |
| 9 / 10 | MessageRequest / Response | 发群消息 |
| 12 / 13 | SyncRequest / Response | 同步 |
| 22 / 23 | PublicKey | 取服务端 RSA 公钥 |
| 33 / 34 | EncryptSet | 协商会话 AES 密钥 |
| 40 / 41 | SingleMessage | 发单聊消息 |
| 32 | （不在表里） | 传输层数据帧，真内容在 `tag10` |

注意 `32` 不在 `@CommandValue` 表里，但双向都在用——它是传输层包装，不是业务命令。
所以业务帧实际是「`cmd=32` + `tag10` = 加密后的 [业务命令 + 业务 TLV]」。

#### 4.3.8 IM 层的 RSA 公钥是硬编码的，无法中间人替换

曾经想用「替换服务端公钥」的办法把客户端会话密钥骗到手，这条路**已确认走不通**。

排查过程：

| 动作 | 结果 |
|---|---|
| 删 `com.xtc.im.core.app.xml` | App 照常连，**仍不发 `cmd 22`** |
| 再删 `com.xtc.data.http.xml` | 同上，直接发 `cmd 33` |
| 在 prefs / files / databases 里搜 PEM | 没有 |
| 全目录搜 `MIGf` / `MIGJAoGB`（1024 位 SPKI/PKCS#1 前缀） | **只在 `.cache/classes.jar`（App 代码）里命中** |

也就是说 **IM 用的那把 RSA-1024 公钥是编译进代码的常量**，
不是从服务端取、也不是缓存——所以没有可替换的时机。

顺带确认了 HTTP 层的 `/rsa` 是另一把钥匙：

```json
{"encSwitch": 0, "publicKey": "MFwwDQYJKoZIhvcNAQEB..."}
```

`MFww` 是 **RSA-512** 的 SPKI 前缀，与 IM 的 1024 位不是同一把；
`encSwitch: 0` 也解释了 HTTP 侧为什么走 v0 明文信封。

**推论**：既然公钥写死在客户端里，服务端的私钥就只在服务端，
任何人都无法解出客户端 `EncryptSet` 里那 128 字节的会话密钥。
所以「抓一次 App 流量看明文」这条路对 IM 层是关闭的。

#### 4.3.9 固定消息已解到「能读出字段名」的程度

对那条 386 字节的应答做了系统排查，结论如下。

**异或密钥确认无误。** 逐字节验算：密文 `22 e9 20 31 40 55 67 e7 …`
配 `key=00112233445566778899aabbccddeeff` 得到 `22 f8 02 02 04 00 01 90 67 25 …`，
每一字节都对得上（`0x22^0x00=0x22`、`0xf8^0x11=0xe9`、`0x02^0x22=0x20` …）。

**换起始偏移扫描**：只有偏移 0 能解出英文单词（7 个），其余 15 个偏移都是 0 个。
所以对齐也是对的。

**明文结构**：`22 f8 02` = tag34 / len376，正好 3+376=379 吻合。

**明文里能读出的英文片段**（按出现顺序）：

```
does   form   null   stion   baseb   umber   usiness   Id: 1, c   imS   MA1
```

补全后是这些字段名：**`basebandVersion`**（baseb…）、**`modelNumber`**（…umber）、
**`businessId`**（…usiness…）、`platform`（…form）、`imSdkVersionName`（imS…）、`mac`。
**这些全是 `RegistRequestEntity` 的字段**，不是 `LoginRequestEntity` 的。

**排除了几种可能**：

| 假设 | 结果 |
|---|---|
| 是压缩流（zlib/deflate/gzip） | ❌ 各偏移都解不开 |
| 是 UTF-8 / GBK / GB18030 / Big5 / UTF-16 | ❌ 只有 UTF-16BE 把 ASCII 抬进 CJK 区，不是真解 |
| 是握手残留 | ❌ 排空后仍收到，且静默 8 秒无推送 |
| 是对请求的回显 | ❌ 填不同 `registToken`/`deviceToken`、发注册报文、只发 RID，响应**逐字节相同** |

**目前的解读**：这是一条**固定消息**，内容围绕「设备注册」的字段。
它更像服务端在说「你这条不是有效业务帧，请先注册」，而不是逐字段校验结果。

**由此得到的行动线索**：`cmd=32` 的载荷被当成**注册实体**解析了。
原因是登录与注册的标签号大量重叠（tag17 imei/deviceToken、tag18 imsi/imSdkVersionName、
tag19 mac/pushType、tag20 modelNumber/platform、tag21 basebandVersion/timestamp、
tag24 businessId/baseRequestParam）。既然服务端按注册解释，
那么**正确顺序应是先 `cmd 1` 注册拿到 `registId`/`registToken`，再 `cmd 3` 登录**，
而不是跳过注册直接登录。

---
#### 4.3.10 更正：业务命令放在帧头第二字节，不是塞进 tag10

上一节把 `cmd=32 + tag10` 当成业务通道，这个判断是错的。实测直接发业务命令号有效：

| 发送方式 | 响应 | 结论 |
|---|---|---|
| `frame(cmd=1, 注册TLV)` | **631 B** | ✅ 被当成注册请求处理 |
| `frame(cmd=32, tag10=XOR(...))` | 386 B | ❌ 通用通道，固定消息 |
| `frame(cmd=3, 登录TLV)` | 15 B | 命令被识别，返回错误码 |

**判据**：注册帧的响应长度**随请求字段变化** ——
`businessId` 从 `"1"` 改成 `"999"`，响应从 631 B 变成 633 B（正好多两字节）。
这说明服务端真的在解析并回显我们发的字段，不是固定消息。

注册响应里能读出这些字段名（按出现顺序）：

```
platform  imSdkVersionName  imsi  basebandVersion  modelNumber
registToken  appId  email  maxAge  deviceId  sdkVersion  brandDesc
```

还有一段像 JSON 的片段：`"appId":…`、`"email":""`、`"maxAge":-1`、`"deviceId":…`、
`9.47","us…`。这正是我们发上去的 `accountToken`（SSO 票据本身是 JSON 串）被回显。

**所以命令号的正确位置是帧头第二字节**，`tag10` 那层是另一套东西。

#### 4.3.11 登录仍被拒：固定返回码 30542

`cmd=3` 的响应固定为 10 字节：

```
tag36 len8 { tag1 = RID（我们发的，会原样回显）, tag2 = 0x774e = 30542 }
```

**载荷怎么改都不变**：完整登录帧、只发 RID、RID+BRP、RID+registId+BRP，
四种写法的 code 都是 30542。先发 `cmd=1` 注册再发 `cmd=3` 也一样。

对照 `EncryptSet` 的成功应答（`tag2 = 200`），可以确认 `tag2` 就是状态码，
30542 是一个业务错误。结合上一节的发现，最可能是
**"registToken 无效或缺失"** —— 因为登录帧的 `tag11` 我们一直填的空串。

**当前卡点**：`registToken` 只在注册响应里，而那条 631 字节响应的解码还不完整
（明文里文本与二进制交织，按 TLV 拆也不闭合）。
拿到 `registToken` 之后登录才有希望。

---
#### 4.3.12 登录帧的发送方式仍未确定

把 `registToken` / `deviceToken` 分别填空、填票据、都填票据，三次请求的响应**完全一致**
（386 字节），说明服务端返回的是一条**固定消息**，不是针对本次请求的回显。

把 `tag10` 按会话密钥异或解出来，能看到零散的可读片段：

```
...does n...form: nu.../e: null,...ion: nul...stion: n...ull, mac..., base...
...umber: n...usinessId: 1, c..., imS...MA1...
```

拼起来像是某个对象的 `toString()`，多数字段为 `null`。
但因为是固定消息，不能据此判断「哪些字段缺失」。

另外，`cmd=3` 直发时服务端返回的是 `cmd=32` 的数据帧而不是 `cmd=4`（登录应答），
说明登录命令没有被当成登录处理。

**登录帧（`cmd 3`）还没打通。** 已按 `LoginRequestEntity` 的字段全部填好并发出：

```
t1=RID, t10=registId(0), t11=registToken(""), t12=sdkVersion,
t17=deviceToken(""), t18=imSdkVersionName, t19=pushType, t20=platform,
t21=timestamp, t24=baseRequestParam(BRP JSON), t26=deviceId
```

服务端回的是 `a0 20 0c | 0a 0a <10B>`——一条 `cmd=32` 的数据帧，
不是 `cmd=4`（LoginResponse）。说明登录没被当成登录处理，或者被拒绝了。

可能的原因（按可能性排序）：

1. **登录帧也要包在 `cmd=32` 里**，由 `tag10` 承载，而不是直接发 `cmd=3`；
2. `registToken` / `deviceToken` 不能空——这两个值来自设备注册（`cmd 1`）和推送 SDK；
3. `registId` 不能是 0，需要先走注册或从上一次登录的 `PushInfo` 里取。

下一步最省事的验证是**试第 1 条**：把同样的登录载荷加密后塞进 `cmd=32` 的 `tag10` 再发一次。

另外，`tag10` 那层 AES 的模式（ECB / CBC）还没有实证确认——
因为服务端对心跳的应答看起来是明文，没有足够样本比对。
拿到一次成功的加密业务帧就能一次性确定。

---

脱壳后把全部 Retrofit 注解抽出来，共 **323 个接口**，逐个看过：

- 没有任何「发送聊天消息」的接口；
- 聊天相关只有群组的增删改查、文件下载、位置共享；
- `/findWatch/setLeaveMessage` 是**留言**（手表关机时的留言），不是聊天消息。

**结论：发消息没有 HTTP 接口，只能走 IM 长连接。** 这条结论现在是代码级确认，
不再是靠试探推断。

#### 4.3.13 端口状态

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

## 七、凭据获取（三个已拿到，一个卡住）

调用真实接口需要四个值：`bind_number`（绑定号）、`chip_id`、`account_id`、`encrypt_key`。

| 值 | 状态 | 怎么拿到的 |
|---|---|---|
| `account_id` | ✅ 已取得 | 用账号密码调 `/app/loginV2`，响应 `data.accountId` |
| `bind_number` | ✅ 已取得 | App「更多 → 宝贝个人中心 → 手表绑定号」页直接显示（16 位小写字母） |
| `encrypt_key` | ⚠️ 部分 | 结构是 `keyId:aesKey:eebbkKey`，三段式能被服务端识别，但真实值不明 |
| `chip_id` | ❌ **卡住** | App 私有 SharedPreferences，无 root 读不到 |

### 7.1 登录已经完全打通（不需要手机）

这是本次最实用的结论：**账号密码登录整条链路可以在 Linux 上跑通，不需要设备、不需要 root、不需要证书。**

```
POST /app/rsakey   -> 取 RSA-1024 公钥（000001）
POST /app/loginV2  -> AES-128-ECB 加密请求体 + RSA 包裹 aesKey
                   -> 返回 data.accountId / sign / deviceId
```

实现见 `src/xtc_qq_bridge/api/sso.py`。真实账号实测返回 `000001 success`，
拿到 `accountId`、`sign`（32 位）、`deviceId`（16 位）。

**注意登录响应里的 `deviceId` 与绑定号不是同一个东西**：前者是手机端会话标识（16 位十六进制），
后者是手表绑定号（16 位小写字母）。BRP 里的 `deviceId` 要填**绑定号**。

### 7.2 `chip_id` 是硬性必需项（已用对照实验证实）

这一点之前只是从文档推测，这次做了完整的对照实验。结论很干净：

| BRP 里的 `token` | 服务端返回 | 含义 |
|---|---|---|
| 空 | `000003` | 参数校验不通过（缺字段） |
| 任意非空值 | `100500` | 参数结构过了，网关拒绝（值无效） |

换 `registId`（0 / accountId / timestamp）、`model`（空 / XTC_Z10 / Z8A）、
`dataCenterCode`（CN_BJ / CN / 空）**都不改变结果** —— 唯一决定因素是 `token` 是否为空。

所以 `chip_id` 不能省略、不能伪造。

#### 7.2.1 只有 `/server/domain/list` 不需要 token

把一批 GET 接口逐个试过，只有域名列表放行，其余全部要求 token：

| 接口 | 无 token 时 |
|---|---|
| `GET /server/domain/list` | ✅ `000001 success`（返回三套环境域名表） |
| `GET /mobileinit/authid` | ❌ `010403` The token is invalid |
| `GET /weichat/emoji` | ❌ `000003` |
| `GET /watchaccount/query` | ❌ `000003` |
| `GET /group-chat/familyGroup/groups` | ❌ `000003` |
| `GET /contact/<绑定号>` | ❌ `000003` |
| `GET /app/areaCodeList` | ❌ `000003` |
| `GET /app/login/modes` | ❌ `000003` |

**方法（GET/POST）比参数更关键**：`/server/domain/list` 用 POST 一直报 `000003`，
换成 GET 立刻就通了。调不通某个接口时，先换方法再怀疑参数。

#### 7.2.2 `010403` 与 `000002` 的区别

`/mobileinit/authid` 会返回内层错误码，把两者区分得很清楚：

| token 取值 | 外层 | 内层 | 含义 |
|---|---|---|---|
| 空串 | `000003` | `010403` | **没给** token |
| 任意非空（1/16/32/64/128 位、登录 sign） | `000003` | `000002` | **给了但值不对** |

这说明服务端先看"有没有"，再看"对不对"。所以 `010403` 不是"格式错"，
而是"缺失"；`000002` 才是"值无效"。两者都不能靠猜绕过。

### 7.3 `encrypt_key` 的实测行为

`ImooCrypto` 按 `keyId:aesKey:eebbkKey` 三段式识别 v3 加密。实测：

| 传入值 | 服务端返回 |
|---|---|
| 空 | `000003`（走 v0 明文） |
| `1:16字符:x` | `000016`（结构被识别，但内容校验不过） |
| `1:16字符:`（第三段空） | `000003`（退回 v0） |
| 只有 aesKey | `000003`（退回 v0） |

`000016` 是**独立于 token 的一层校验**：无论 token 填什么、`eebbkKey` 填什么，
只要结构是 `1:16字符:非空`，就固定返回 `000016`。说明服务端在这个阶段还没解我的密文，
而是先校验密钥标识本身。真实 `keyId` / `aesKey` / `eebbkKey` 三值都不明。

### 7.4 为什么拿不到 `chip_id`（三条路都试过）

| 方案 | 结果 |
|---|---|
| `run-as` | ❌ `package not debuggable` |
| `adb backup` | ❌ 只得到 47 字节空备份；`dumpsys package` 的 flags 里没有 `ALLOW_BACKUP`（`allowBackup=false`），App 直接拒绝，手机也不弹确认框 |
| Shizuku / FV / MiXplorer | ❌ **本质上等同于 `adb shell`**（同样是 uid=2000 shell）。`/data/data/com.xtc.watch` 权限是 0700 且属主是应用 uid，shell 进不去。它只是把同样的权限包装成图形界面，没有提权 |
| 读 App 日志 | ❌ 日志是腾讯 mars xlog 格式（XOR 加密），需要 App 内的密钥才能解 |
| `/sdcard/Android/data/com.xtc.watch/` | ✅ 可读，但里面只有日志、表情缓存、崩溃记录，**没有凭据** |

**结论：不 root 就拿不到 `chip_id`。**

### 7.5 脱壳：拿到全部业务代码

真正解开问题的是**把加密的 dex 从内存里 dump 出来**，而不是继续猜接口。

加固方式是「壳 dex + 尾部加密载荷」：`classes.dex` 只有 5 个类、19 KB 是有效内容，
剩下 106 MB 是 SecNeo 壳的加密载荷（头 20 字节为 `dexdata` 标记 + 长度 + 8 字节元数据）。
静态解不开（单字节 XOR 爆破、AES 各模式 × 已知密钥全部失败），
但**壳会在运行时把载荷原地解密到自己的内存映射里**：

```
1778 行 /proc/<pid>/maps 里有一条：
  105.5 MB  rw-p  /data/data/com.xtc.watch/.cache/classes.jar
```

这条映射的 md5 与磁盘上的 `classes.jar` 不同 —— 证明已被原地解密。
按映射地址 `dd` 出来，里面能找到 **15 个 `dex\n035` 魔数**，
逐个按 dex 头声明的 `file_size` 切出来（并修正 sha1 签名与 adler32 校验和），
就得到 14 个真实 dex，合计约 **7 万个类**。用 jadx 反编译即可读全部业务逻辑。

> 这一步是本次的关键转折：没有它，前面所有结论都只能靠试探；
> 有了它，`Token 从哪来`、`BRP 有哪些字段` 都是读代码读出来的。

### 7.6 `token` 的真相：它就是登录响应 `data` 的原始 JSON 串

反编译后追到的链条：

```java
// com/xtc/account/service/impl/Gibraltar.java:193-194（登录成功回调）
SSOLoginResult r = (SSOLoginResult) Uganda.Gabon(loginResponse.data, SSOLoginResult.class);
r.setTicket(loginResponse.data);          // ← ticket = data 的原始字符串，不是子字段

// com/xtc/account/service/Gibraltar.java:22-24（网络模型转本地模型）
MobileAccount m = new MobileAccount();
m.setToken(r.getTicket());                // ← token 就是 ticket

// com/xtc/h5/Gambia.java:237（构造 BRP）
baseRequestParam.setToken(Vietnam(mobileAccount));   // = mobileAccount.getToken()
```

**所以 `token` 不是某个 ID，而是整个 `data` JSON 文本。** 登录时我一直只取
`sign` 之类的子字段去试，方向从一开始就错了。

实测替换后，`/mobileinit/authid` 的返回从 `000003 / 010403（token 无效）`
变成 `010013`，其余业务接口从 `000003` 变成 `000016` —— **token 这一关过去了**。

### 7.7 `BaseRequestParam` 的完整字段（含四个此前不知道的）

```java
// com/xtc/http/bean/BaseRequestParam.java
accountId  appId  deviceId  imFlag  mac
machineId  nDeviceId  program  registId  timestamp  token
```

赋值逻辑在 `com/xtc/h5/Gambia.java:233-249`：

| 字段 | 取值 | 说明 |
|---|---|---|
| `appId` | 固定 `"0"` | 不是登录用的 11 |
| `token` | `mobileAccount.getToken()` | 见 §7.6 |
| `timestamp` | `yyyy-MM-dd HH:mm:ss` | 本地时间 |
| `machineId` | `HttpDataTool.Venezuela()` | 原生方法，取不到静态值 |
| `mac` | `HttpDataTool.Hungary(context)` | **不是 `"unkown"`** |
| `accountId` | `mobileAccount.getMobileId()` | **不是登录响应的 accountId** |
| `deviceId` | `HttpDataTool.Gibraltar(mobileId)` | 由 mobileId 推导 |
| `program` | `"android"` | |
| `imFlag` | `"1"` | |
| `registId` | `ImPhoneApi.getRegistId()` | 长期为 0 时服务端不报错 |

两个最容易踩的坑：`accountId` 要填 **mobileId**（形如 `wac…` 的 32 位串），
以及 `mac` 不是占位串。

### 7.8 `eebbkKey` 不需要"找"：它是客户端随机生成的

这一点颠覆了我之前的所有尝试：

```java
// com/xtc/watch/util/HongKong.java  （UUIDUtil）
public static String Hawaii() {
    String s = UUID.randomUUID().toString();
    return s.substring(0,8) + s.substring(9,13) + ... ;   // 去掉横线，32 位
}

// com/xtc/http/Georgia.java:17
private static final String Germany = HongKong.Hawaii().substring(0, 16);  // ← 前 16 位

// com/xtc/http/okhttp/Guatemala.java:257-259 + Gambia.java:285
map.put("Eebbk-Key", str);          // str = RSA(aesKey)，encSwitch 开启时
map.put("encrypted", "encrypted");
```

也就是说：AES 密钥 = **随机 16 位十六进制**，随请求通过 `Eebbk-Key` 头自报，
签名也用它（`MD5(url + BRP + body + aesKey)`）。**它不是从服务端取来的固定值**，
所以之前那 5 个 appId 密钥根本不该用于核心 API。

`encSwitch` 有两条路径（`com/xtc/http/Gabon.java:225` 读的是 `ENC_SWITCH` 偏好，默认 0）：

| 模式 | `Base-Request-Param` | `Eebbk-Key` |
|---|---|---|
| v0（encSwitch=0） | BRP 明文 | 明文 aesKey |
| v3（encSwitch=1） | base64(AES-ECB(BRP)) | base64(RSA(aesKey))，另加 `encrypted: encrypted` |

### 7.9 错误码对照（实测）

| 码 | 含义 | 触发条件 |
|---|---|---|
| `000001` | 成功 | |
| `000003` | 参数无效（细码见 `data.code`） | |
| `000016` | 参数无效 | 业务接口缺各自参数；`desc` 固定为 request parameter is invalid |
| `010013` | **APP 版本过低**（`tips` 会明说） | 只出现在 `/mobileinit/authid`，与 `Version` 头无关 |
| `010401` | 签名无效 | BRP 里 `mac` / `machineId` 留空时出现 |
| `010403` | 未带 token | BRP 里 `token` 为空 |
| `000002` | token 值不对 | `token` 非空但不是合法 ticket |
| `100500` | 网关错误 | appId 与路径不匹配（如 appId=0 打 SSO 路径） |

`000003` 的细码很有用：`010403` = 没给 token，`000002` = 给了但不对，
`010401` = 签名不对。三者区分清楚了，排错就不会乱猜。

### 7.10 剩下的可行路径

1. **临时 root**（Magisk / KernelSU）：本机 WSA 已装 Magisk，`/debug_ramdisk/su` 可用。
2. **把证书装进系统区**：需要 root 或可写 `/system`（本机 `/system` 是 `erofs (ro)`，不可写）。
3. **用户手动提供**：如果用户能从别处拿到 `chip_id`，直接填入配置即可。
4. **放弃 HTTP 路线**，继续用 ADB 界面自动化（现状）。

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
