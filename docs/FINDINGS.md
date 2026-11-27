# 实测结论（可复现）

这份文档记录"为什么代码长成这样"的硬事实。每条都标了验证方式；换部署请自行复测。

## 1. 二维码载荷的结构

```
data = 1794355176 + 427f6c357a1f0c0e6f2efcc5317ab7e0
       10 位秒级时间戳   32 位 hex（md5 长度）
```

- 4 张真实载荷全部 42 字符，无一例外。
- 同一次场次的相邻两张码，只有时间戳和签名变，`rollcallId` 不变。

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
⇒ 签名带服务端私有密钥，客户端算不出来。

复现脚本：`tools/probe_forge.py`。

## 3. 取码接口按权限分级

```
GET /api/course/{cid}/rollcall/{rid}/qr_code
```

普通账号会话恒 `403`（rid 不存在也是 403，权限判断先于资源判断）。
⇒ 想要"零人工"，唯一入口是持有一个高权限会话去轮询该接口
（本项目不内置这条路，逻辑见 `qr_api.TronClient.get_qr_token`）。

## 4. 成功返回体极薄，真凭据在记录里

成功提交的返回长这样：

```json
{"id": null, "status": "on_call"}
```

没有 rollcall 对象、没有时间戳、没有距离。所以**不能凭 200 判断"真的生效"**，
要回查记录：

```
GET /api/rollcall/{rid}/student_rollcalls
→ 我那一行: rollcall_status=on_call_fine, status=on_call, updated_at=<落库时间>
```

`rollcall_status=on_call_fine` 才是最终生效的那个值。

## 5. 一码多账号可行（同码 + 不同 deviceId）

同一张码、同一个 `data`，用不同账号各自的 `deviceId` 依次提交，各自都能成功。
所以"一张码提交多个账号"是服务端允许的，前提是每个账号一台"设备"（deviceId 不能复用）。

反例：两个账号用同一个 deviceId 会撞 `device_used` / `deviceAlreadyInUse`。

## 6. 单码有效期约十几秒

- 服务端约每 15 秒换一张码。
- 监测到的下发延迟分布很宽：有 10~11 秒的码能提交成功，15 秒的码已经 `qr_code_expired`。
- 结论：任何"先截图再上传"的链路都必须在十几秒内完成，否则必过期。

## 7. 糊图不可救（这是采集端的活，不是解码器的活）

对 30 张现场实拍糊图跑多路解码（zxing-cpp 六路）：**0 张**能解。
同一套解码器对清晰帧：759 张里解出 17 张。

⇒ 解码器只能救"轻微失焦/低对比"，救不了"根本看不清"。
所以页面把重点放在**让手机产出足够清楚的图**：高分辨率 + 连续对焦 + 原尺寸静态照片 +
系统相机入口，然后再交给服务端解码。

## 8. 鉴权双通道

`Cookie: session=...` 和请求头 `x-session-id: ...` 单独带都能通过，两个同时带也行。
本项目两样都带（cookie 来自 `cookies_<id>.json`，头取同一个值）。
