# Hug Bear MCP 🧸

单压力传感器抱抱小熊：ESP32-S3 识别拥抱力度与持续时间，通过 Zeabur 保存记录，并向 ChatGPT 暴露只读 MCP 工具。

## 已验证硬件

- ESP32-S3 N16R8
- FSR402 薄膜压力传感器（1 个）
- 10kΩ 分压电阻
- 传感器信号接 GPIO4
- Arduino ESP32 core `3.3.10-cn`
- 板型 `ESP32S3 Dev Module`
- USB CDC On Boot: Enabled

实机校准（2026-09-01）：

| 状态 | ADC 范围 |
|---|---:|
| 松开 | 约 0 |
| 轻轻抱住 | 1300–2400 |
| 正常拥抱 | 2400–3500 |
| 用力抱紧 | 3500–4095 |

## 固件

Arduino 草图位于：

`firmware/hug_bear_sensor/hug_bear_sensor.ino`

首次启动时，小熊会创建 Wi‑Fi 热点：

- 名称：`HugBear-Setup`
- 密码：`hugbear88`
- 配网页面：`http://192.168.4.1`

配网页面用于填写 Wi‑Fi、Zeabur 服务地址和 `DEVICE_TOKEN`。这些信息只保存在 ESP32 的 Preferences/NVS 中，不进入仓库。

## Zeabur

服务需要持久卷挂载到 `/data`，并配置：

- `DEVICE_TOKEN`：ESP32 写入接口鉴权
- `MCP_TOKEN`：MCP URL 鉴权，24–128 位，仅字母、数字、`_`、`-`
- `LOCAL_TIMEZONE=Asia/Shanghai`

健康检查：`/health`

MCP 地址：

`https://<your-domain>/mcp/<MCP_TOKEN>`

## 设备接口

- `POST /api/hug/start`
- `POST /api/hug/heartbeat`
- `POST /api/hug/end`

设备请求头：

`X-Device-Token: <DEVICE_TOKEN>`

## MCP 工具

- `latest_touch`
- `recent_touches`
- `hug_summary`
- `was_hugged_recently`
- `current_hug_state`

不要把 Wi‑Fi 密码、`DEVICE_TOKEN` 或 `MCP_TOKEN` 提交到 GitHub。
