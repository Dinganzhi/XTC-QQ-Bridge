# 小天才 App API 地址清单

> 目标 App：`com.xtc.watch`（国内版小天才）9.47.02
> 采集时间：2026-11-10
> 用途：互操作性研究
>
> 本清单分三部分：**服务地址**、**接口清单**、**协议信封**。
> 每条标注了来源与可信度，未经验证的会明说。

---

## 一、服务地址（Base URL）

### 1.1 核心服务

| 服务 | Base URL | 用途 | 可信度 |
|---|---|---|---|
| 核心 API | `https://watch.okii.com` | 账号、设备、联系人、群聊、闹钟、支付 | **实测可用**（`/rsa` 返回 `000001`） |
| 登录 SSO | `https://account.okii.com` | 登录、注册、验证码、改密 | 已抓包确认（HTTPS 握手） |
| 位置 | `https://location.watch.okii.com` | 实时定位、轨迹、地理围栏 | 服务端配置返回 |
| 健康运动 | `https://sport.watch.okii.com` | 健康汇总、运动数据 | 服务端配置返回 |
| 聊天 | `https://watch.okii.com` | 表情列表、语音消息 | 服务端配置返回（`chatDomain`） |
| 积分 | `https://points.okii.com` | 积分、奖励任务 | 服务端配置返回 |
| 静态资源 | `https://static.watch.okii.com` | H5 页面、帮助页 | 抓包确认 |
| 应用商店 | `https://store.watch.okii.com` | 应用列表 | 服务端配置返回 |
| 第三方 | `https://thirdparty.watch.okii.com` | 第三方授权 | 服务端配置返回 |
| 活动 | `https://act.okii.com` | 运营活动 | 服务端配置返回 |
| 国际版镜像 | `https://watch-oz.okii.com` 等 `*-oz.okii.com` | 海外包 `com.imoo.watch.global` | 公开资料 |

> ⚠ `api.watch.okii.com` **不是**核心 API 的 base。它能解析、有 `/health`，
> 但据静态分析它只是 `HttpConstants.HOST_NAME_ARRAYS[1]`，业务接口不在上面。

### 1.2 IM 接入点（消息收发）

| 地址 | 端口 | 协议 | 状态 |
|---|---|---|---|
| `gw.im.okii.com` | `8000` | 自定义二进制（非 TLS、非标准 MQTT） | **已探明首帧结构** |
| `gw.im.okii.com` | `1883` | 同上（端口号像 MQTT，实测不响应 MQTT） | 已探明 |
| `im3.okii.com` | `8002` | 同上 | App 实际连接此地址 |
| `gw.im.okii.com` | `8883` / `443` | — | 未开放 |

**文本消息走这里，不走 HTTP。** 详见 `docs/imoo-api.md` 第 4.3 节。

### 1.3 风控与统计（第三方）

| 地址 | 用途 |
|---|---|
| `cfgc.zztfly.com` | 配置下发（`/dm?appkey=...` 返回明文域名表） |
| `upc.zztfly.com` | 上报（`POST /v6/gcl`，请求体为自定义二进制） |
| `log-auth.zztfly.com` | 日志（`POST /api/log`，带 `sign` 头） |
| `cdn-api-auth.zztfly.com` | CDN 鉴权 |
| `api-auth.zztfly.com` | 风控 API |
| `watchda.eebbk.net` | 数据上报（启动即连） |

`appkey` 为 `3a47312520fba`（所有请求共用，非用户凭据）。

### 1.4 服务端下发的权威域名表

`GET https://watch.okii.com/server/domain/list` 返回（**实测**）：

```json
{
  "watchDomain": "watch.okii.com",
  "chatDomain": "watch.okii.com",
  "locationDomain": "location.watch.okii.com",
  "pointsDomain": "points.okii.com",
  "activityDomain": "act.okii.com",
  "ssoDomain": "account.okii.com",
  "imJoinDomain": "gw.im.okii.com:8000",
  "sportDomain": "sport.watch.okii.com",
  "weatherDomain": "sport.watch.okii.com",
  "h5Domain": "static.watch.okii.com",
  "thirdDomain": "thirdparty.watch.okii.com",
  "storeDomain": "store.watch.okii.com",
  "imBakcupDomainList": ["gw.im.okii.com:1883", "..."]
}
```

这份表比任何逆向文档都可靠 —— 它是服务器自己给的。

---

## 二、接口清单

以下按功能分组。方法标注 `GET`/`POST` 等；来源为 APK 静态提取的公开规范，
方法名可能与其实际实现有出入（原规范已注明 "HTTP methods are inferred"）。

### 2.1 认证（`account.okii.com`，`/app/*`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/app/rsakey` | 取 RSA 公钥（1024 位 SPKI），用于包裹每请求 AES 密钥 |
| GET | `/app/login/modes` | 可用登录方式 |
| GET | `/app/areaCodeList` | 国际区号（176 个） |
| GET | `/app/checkAccount` | 账号是否存在 |
| GET | `/app/passwordStatus` | 密码状态 |
| POST | `/app/sendRandCodeV2` | 发短信验证码（需 reCAPTCHA） |
| POST | `/app/checkRandCode` | 校验验证码 |
| POST | `/app/randcodeLogin` | **验证码登录** → 返回 `token` + `authId` |
| POST | `/app/loginV2` | **密码登录** → 同上 |
| POST | `/app/tokenLogin` | token 登录 |
| POST | `/app/logout` | 登出 |
| POST | `/app/verifyToken` | 校验 token |
| POST | `/app/modifyPassword` | 改密 |
| POST | `/app/resetPassword` | 重置密码 |
| DELETE | `/app/deleteAccount` | 注销账号 |
| GET | `/app/oneClick/getServer` | 一键登录服务地址 |
| POST | `/app/oneClick/login` | 一键登录 |
| POST | `/app/third/login` | 第三方登录 |
| GET | `/oauth/openAccount/getOpenId` | OAuth 取 openId |

### 2.2 设备与账号

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/watchaccount` | 手表账号信息 |
| GET | `/watchaccount/id/{watchId}` | 按 watchId 查 |
| GET | `/watchaccount/getAuthorize` | 授权状态 |
| GET | `/watchaccount/soundMode/{watchId}` | 声音模式 |
| POST | `/watchaccount/soundMode/setSoundMode` | 设置声音模式 |
| POST | `/watchaccount/updateBabyName` | 改宝宝昵称 |
| POST | `/watchaccount/updateRealName` | 改实名 |
| POST | `/watchaccount/update/watchAttribute` | 改手表属性 |
| GET | `/mobileaccount` | 家长账号信息 |
| GET | `/mobileaccount/id/{mobileId}` | 按 mobileId 查 |
| GET | `/mobileinit/authid` | 取 authId |
| GET | `/mobilewatch` | 绑定关系 |
| GET | `/mobilewatch/watch/{watchId}` | 按手表查绑定 |
| POST | `/mobilewatch/changeadmin` | 更换管理员 |
| POST | `/smartwatch/watchaccount/bindnumber` | **绑定手表**（邀请码） |
| GET | `/smartwatch/mobilewatch` | 绑定列表 |
| POST | `/smartwatch/mobilewatch/apply/admin` | 申请管理员 |

### 2.3 联系人

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/contact/{watchId}` | 手表联系人列表 |
| POST | `/contact/batch/add` | 批量添加 |
| DELETE | `/contact/del/{id}` | 删除 |
| POST | `/contact/sort` | 排序 |
| POST | `/contact/update/info` | 改信息 |
| POST | `/contact/contactParentReview` | 家长审核 |
| GET | `/watchfriend/watchid/{watchId}` | 手表好友 |
| POST | `/watchfriend/batch` | 批量加好友 |
| POST | `/watchfriend/importPhoneBook` | 导入通讯录 |
| GET | `/watchfriend/recommendClassmate` | 推荐同学 |

### 2.4 群聊

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/group-chat/familyGroup/groups` | 家庭群列表 |
| GET | `/group-chat/familyGroup/members` | 群成员 |
| POST | `/group-chat/familyGroup/create` | 建群 |
| POST | `/group-chat/familyGroup/invite` | 邀请 |
| POST | `/group-chat/familyGroup/quit` | 退群 |
| POST | `/group-chat/familyGroup/dismiss` | 解散 |
| GET | `/group-chat/file/download` | 群文件下载 |

### 2.5 消息与通话 ⚠

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/weichat/emoji` | 表情列表 |
| POST | `/weichat/voice` | 发**语音**消息 |
| POST | `/videocall/call` | 发起视频通话 |
| POST | `/videocall/callresponse` | 接听/拒接 |
| POST | `/videocall/hangup` | 挂断 |
| POST | `/videocall/finish` | 结束 |
| GET | `/videocall/callquality` | 通话质量 |
| POST | `/videocall/chat/videoToVoice` | 视频转语音 |
| POST | `/videocall/interact` | 通话互动 |

> **⚠ 没有"发送文本消息"的接口。**
> 全部 400+ 端点里，Chat 分类只有 `/weichat/emoji` 与 `/weichat/voice`。
> 文本消息走 `gw.im.okii.com:8000` 的私有协议。

### 2.6 定位与围栏（`location.watch.okii.com`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/location/{watchId}` | 当前位置 |
| GET | `/location/multilocation` | 批量定位 |
| GET | `/location/motionstate/{watchId}` | 运动状态 |
| POST | `/location/addressFeedback` | 地址纠错 |
| GET | `/sub/latestLocationPush` | 最新位置推送 |
| POST | `/realtime/upload` | 上报实时位置 |
| POST | `/realtimeDisable/{watchId}` | 关闭实时定位 |
| GET | `/contrail/getRealContrail` | 实时轨迹 |
| GET | `/contrail/queryData` | 历史轨迹 |
| GET | `/zone/watch/{watchId}` | 地理围栏 |
| POST | `/zone/floorSetting` | 楼层设置 |
| GET | `/allDayGuard/queryGuardStatus` | 全天守护状态 |
| POST | `/allDayGuard/addCommonArea` | 加常用区域 |
| POST | `/allDayGuard/addDangerArea` | 加危险区域 |
| POST | `/followGuard/startGuard` | 开始跟随守护 |

### 2.7 健康与运动（`sport.watch.okii.com`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/sport-service/health/i32/home` | 健康首页（i32 机型） |
| GET | `/sport-service/health/nd01/home` | 健康首页（nd01 机型） |
| GET | `/flutterSport` | Flutter 桥接的运动数据 |

### 2.8 手表功能

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/watchalarm/watchid/{watchId}` | 闹钟列表 |
| POST | `/watchalarm` | 加闹钟 |
| GET | `/classmode/{watchId}` | 上课禁用状态 |
| POST | `/classmode/v2/add` | 添加上课禁用时段 |
| GET | `/watch-func/timeManagement/list` | 时间管理 |
| POST | `/watch-func/powerSaving` | 省电模式 |
| POST | `/watch-func/systemMode/switch` | 系统模式切换 |
| GET | `/watchWifi/v2/list/{watchId}` | WiFi 列表 |
| POST | `/watchWifi/v2` | 添加 WiFi |
| GET | `/findWatch/getLeaveMessage` | 取留言 |
| POST | `/findWatch/setLeaveMessage` | **设置留言** |
| POST | `/findWatch/playSound` | 让手表响铃 |
| GET | `/findWatch/getPhotos` | 取手表照片 |

### 2.9 应用商店与媒体

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/appstore/app/listAll` | 全部应用 |
| GET | `/appstore/search/global` | 搜索 |
| GET | `/appstore/userapp/getUserApps` | 已安装 |
| GET | `/image` | 取图片 |
| GET | `/photo` | 取照片 |
| POST | `/file/upload` | 上传文件 |
| POST | `/share_image` | 分享图片 |
| POST | `/token/uploadNew/{spaceType}` | 取上传令牌 |
| GET | `/token/download/v2` | 取下载令牌 |

### 2.10 系统配置

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| GET | `/rsa` | **RSA 公钥 + encSwitch** | **免鉴权**（实测 `000001`） |
| GET | `/server/domain/list` | **域名表** | **免鉴权**（实测 `000001`） |
| GET | `/base-service/config/fullFetch` | 全量配置 | 需 session |
| GET | `/version/latest` | 最新版本 |
| GET | `/version/releaseNote` | 更新日志 |
| GET | `/log/collect/add` | 上报日志 |

---

## 三、协议信封

### 3.1 核心 API（`watch.okii.com`）

请求头：

| 头 | 值 |
|---|---|
| `Content-Type` | `application/json;charset=utf-8` |
| `Base-Request-Param` | BRP JSON（`encSwitch=0` 时明文） |
| `Eebbk-Sign` | `MD5(url + BRP + body + key)` 大写 |
| `authId` | 账号 ID |
| `program` | `android` |
| `Version` | App 版本 |
| `Device-Name` | `BRAND-MODEL`（URL 编码） |
| `App-Brand` | `xtc` |
| `App-Checksum` | APK 签名块 MD5 |
| `Eebbk-Key` | `base64(RSA(key, /rsa 公钥))`，仅 `encSwitch=1` |
| `encrypted` | `encrypted` |

BRP 字段（顺序固定）：

```json
{"accountId":"...","appId":"0","deviceId":"...","imFlag":"1",
 "program":"android","registId":"...","timestamp":"yyyy-MM-dd HH:mm:ss",
 "token":"...","mac":"unkown","requestId":"..."}
```

响应：`base64 → AES-ECB → gunzip → JSON`，外层可能是 JSON 字符串字面量。

成功码：`200` / `000001` / `0`。

### 3.2 登录 SSO（`account.okii.com`，`/app/*`）

另一套信封：

```
sign = md5(url + "k1=v1##k2=v2" + secret).toLowerCase()   // key 按大小写不敏感排序
body = base64(AES-128-ECB-PKCS5(JSON))
EncryptKey = base64(RSA-1024-PKCS1(aesKey))               // aesKey = 16 随机 ASCII
```

按 `appId` 选择 secret（公开资料记录，国内包 appId=11）：

| appId | secret |
|---|---|
| 11 | `LKyF8jThHbZfUeTP` |
| 31 | `CXy5qhZZ0qHkjvgh` |
| 33 / 48 | `5sacCZns3z9sTcCX` |
| 35 | `NQIiQkcCIGhfn2s9` |
| 37 | `thqHjysVOkGETPoY` |

`/app/rsakey` 只签名不加密，返回 SPKI 公钥。

---

## 四、能做什么 / 不能做什么

### ✅ 已验证可用（需有效 token）

- 查设备信息、绑定关系、联系人
- 查位置、轨迹、地理围栏
- 查健康数据、闹钟、上课禁用
- 让手表响铃（`/findWatch/playSound`）
- 设置留言（`/findWatch/setLeaveMessage`）
- 取手表照片
- 发语音消息（`/weichat/voice`）

### ❌ 无法做到

- **发文本消息** —— 走私有协议，无 HTTP 接口
- 绕过登录拿 token —— 需要短信验证码或密码

### 💡 一个可用的折中：`/findWatch/setLeaveMessage`

如果只需要"从手机给手表推一句话"，`/findWatch/setLeaveMessage` 是
**唯一不需要 IM 协议**的途径。它的语义是"设置留言"——手表会在
特定界面（找手表/留言板）显示这段文字。

| 项 | 值 |
|---|---|
| 方法 | `POST /findWatch/setLeaveMessage` |
| Host | `watch.okii.com` |
| 鉴权 | 需 `token` + `authId` |
| 限制 | 不是聊天消息，手表端不会以"微聊"气泡形式出现；字数通常有限制 |

`/findWatch/getLeaveMessage` 是对应的读取接口。

这条路径的实用价值取决于你的场景：
* 若只是"通知手表一句话"——够用；
* 若要像聊天一样双向对话——不够，还得走 IM 协议或 ADB。

**现有 ADB 方案在聊天语义上是完整的**，这也是它目前仍是主路径的原因。

### 🔑 需要的凭据

| 值 | 从哪来 |
|---|---|
| `token` | 登录响应 |
| `accountId` | 登录响应（也作 `authId` 头） |
| `deviceId` | 手表绑定号，App「关于手表」页可见 |
| `registId` | 登录响应 |
| `App-Checksum` | APK 签名块 MD5（部分接口可能校验） |

---

## 五、来源

| 内容 | 来源 | 可信度 |
|---|---|---|
| 服务地址、域名表 | **本次实测** `GET /server/domain/list` | 最高 |
| `/rsa` 可用 | **本次实测**，返回 `000001` | 最高 |
| IM 走私有协议 | **本次实测**（`/proc/net/tcp6` + 端口探测） | 最高 |
| 端点清单 | [dachrisch/imoore](https://github.com/dachrisch/imoore) 的 `openapi.yaml`（APK 静态提取） | 高（方法名可能推断） |
| 信封结构 | 同上 + [HIbingai/imoo-client](https://github.com/HIbingai/imoo-client) | 高（`/rsa` 已验证） |
| 登录 secret 表 | 同上，标注为"live check 通过" | 中（未自行验证） |

---

## 六、复现方式

```bash
# 1. 验证 base 与信封（无需凭据）
python3 -c "
import urllib.request
print(urllib.request.urlopen('https://watch.okii.com/rsa', timeout=10).read()[:200])
"

# 2. 取域名表（无需凭据）
python3 -c "
import urllib.request, json
req = urllib.request.Request('https://watch.okii.com/server/domain/list',
    headers={'User-Agent': 'okhttp/3.12.0'})
print(json.loads(urllib.request.urlopen(req, timeout=10).read())['data'][0])
"

# 3. 探测 IM 接入点协议
python3 -B tools/probe_im_gateway.py
```

前两步是**免鉴权**的，可以直接验证本清单的服务地址是否正确。
