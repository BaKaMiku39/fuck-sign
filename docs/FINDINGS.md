# 实测结论（可复现）

这份文档记录"为什么代码长成这样"的硬事实。每条都标了验证方式，换学校/换部署请自己复测。

## 1. 二维码载荷的结构

```
data = 1794355176 + 427f6c357a1f0c0e6f2efcc5317ab7e0
       10 位秒级时间戳   32 位 hex（md5 长度）
```

- 4 张真实载荷全部 42 字符，无一例外。
- 同一次点名的相邻两张码，只有时间戳和签名变，`rollcallId` 不变。

## 2. 服务端严格校验签名（伪造走不通）

用三种伪造 `data` 打同一个进行中的 rid：

| 变体 | 返回 |
|---|---|
| 当前毫秒时间戳 + 32 位随机 hex | `400 invalid_create_time_hash` |
| 只有时间戳 | `400 invalid_create_time_hash` |
| 随便一个字符串 `test` | `400 invalid_qr_create_time` |

两种错误码的差别说明它是**两层校验**：先解析时间戳（解析不了 → `invalid_qr_create_time`），
再验签名（格式对但验不过 → `invalid_create_time_hash`）。

进一步：拿 4 张真码，对 `ts / rid / courseId / activityId` 做 21 种明文拼接 ×
`md5 / sha1[:32] / sha256[:32]` 共 63 组穷举，**0 命中**。同 rid 相邻两帧的签名也没有线性关系。
⇒ 签名带服务端私有密钥，客户端侧算不出来。

复现脚本：`tools/probe_forge.py`。

## 3. 取码接口只对教师/助教开放

```
GET /api/course/{cid}/rollcall/{rid}/qr_code
```

学生会话恒 `403`（rid 不存在也是 403，权限判断先于资源判断）。
⇒ 想要"零人工"，唯一入口是拿一个有教师/助教权限的会话去轮询这个接口
（本项目不内置这条路，逻辑见 `qr_api.TronClient.get_qr_token`）。

## 4. 提交成功返回体极薄，真凭据在名单里

成功的提交返回长这样：

```json
{"id": null, "status": "on_call"}
```

没有 rollcall 对象、没有时间戳、没有距离。所以**不能凭 200 判断"真的签上了"**，
要回查名单：

```
GET /api/rollcall/{rid}/student_rollcalls
→ 我那一行: rollcall_status=on_call_fine, status=on_call, updated_at=<落库时间>
```

`rollcall_status=on_call_fine` 才是老师端认的那个值。

## 5. 一码多签可行（同码 + 不同 deviceId）

同一张码、同一个 `data`，用不同账号各自的 `deviceId` 依次提交，各自都能签上。
所以"一张码签多个人"是服务端允许的，前提是每人一台"设备"（deviceId 不能复用）。

反例：两个账号用同一个 deviceId 会撞 `device_used` / `deviceAlreadyInUse`。

## 6. 单码有效期约十几秒，帧延迟决定成败

- 老师页面约每 15 秒换一张码。
- 监测到的帧延迟分布很宽：有 10~11 秒的帧能签上，15 秒的帧已经 `qr_code_expired`。
- 结论：任何"先截图再上传"的链路都必须在十几秒内完成，否则必过期。
  实测同一条链路里，码龄 15s → `qr_code_expired`，码龄 11s → 成功。

## 7. 糊图不可救（这是采集端的活，不是解码器的活）

对 30 张教室实拍糊图跑多路解码（zxing-cpp 六路）：**0 张**能解。
同一套解码器对清晰的投屏帧：759 张里解出 17 张。

⇒ 解码器只能救"轻微失焦/低对比"，救不了"根本看不清"。
所以页面把重点放在**让手机产出足够清楚的图**：高分辨率 + 连续对焦 + 原尺寸静态照片 +
系统相机入口，然后再交给服务端解码。

## 8. 鉴权双通道

`Cookie: session=...` 和请求头 `x-session-id: ...` 单独带都能通过，两个同时带也行。
本项目两样都带（cookie 来自 `cookies_<学号>.json`，头取同一个值）。
