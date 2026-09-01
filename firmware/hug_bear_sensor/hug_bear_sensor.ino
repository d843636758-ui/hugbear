#include <Arduino.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <WebServer.h>
#include <WiFi.h>

namespace {
constexpr char kFirmwareVersion[] = "2026-09-01-v1";
constexpr char kDeviceId[] = "hug-bear-01";
constexpr char kSetupSsid[] = "HugBear-Setup";
constexpr char kSetupPassword[] = "hugbear88";

constexpr int kFsrPin = 4;
constexpr int kTouchThreshold = 300;
constexpr int kReleaseThreshold = 180;
constexpr int kTightHugThreshold = 3500;
constexpr unsigned long kDebounceMs = 350;
constexpr unsigned long kHeartbeatMs = 60000;
constexpr unsigned long kReconnectMs = 15000;

Preferences prefs;
WebServer server(80);

String wifiSsid;
String wifiPassword;
String apiBase;
String deviceToken;

bool setupMode = false;
bool hugging = false;
unsigned long candidateStartedAt = 0;
unsigned long hugStartedAt = 0;
unsigned long lastHeartbeatAt = 0;
unsigned long lastReconnectAt = 0;
String sessionId;
int peakPressure = 0;
long pressureSum = 0;
unsigned long pressureSamples = 0;

String htmlEscape(const String& value) {
  String escaped = value;
  escaped.replace("&", "&amp;");
  escaped.replace("<", "&lt;");
  escaped.replace(">", "&gt;");
  escaped.replace("\"", "&quot;");
  return escaped;
}

String normalizeBaseUrl(String value) {
  value.trim();
  while (value.endsWith("/")) {
    value.remove(value.length() - 1);
  }
  return value;
}

int readSmoothedPressure() {
  long total = 0;
  for (int i = 0; i < 10; ++i) {
    total += analogRead(kFsrPin);
    delay(4);
  }
  return static_cast<int>(total / 10);
}

String levelName(int pressure) {
  if (pressure < kTouchThreshold) return "没有触碰";
  if (pressure < 2400) return "轻轻抱住";
  if (pressure < kTightHugThreshold) return "正常拥抱";
  return "用力抱紧";
}

String makeSessionId() {
  const uint64_t chip = ESP.getEfuseMac();
  char buffer[80];
  snprintf(
      buffer,
      sizeof(buffer),
      "hugbear_%08lx%08lx_%lu_%lu",
      static_cast<unsigned long>(chip >> 32),
      static_cast<unsigned long>(chip),
      millis(),
      static_cast<unsigned long>(esp_random()));
  return String(buffer);
}

bool postJson(const String& path, const String& json, String* response = nullptr) {
  if (WiFi.status() != WL_CONNECTED || apiBase.isEmpty() || deviceToken.isEmpty()) {
    return false;
  }

  HTTPClient http;
  http.setConnectTimeout(8000);
  http.setTimeout(10000);
  http.begin(apiBase + path);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Token", deviceToken);

  const int status = http.POST(json);
  const String body = http.getString();
  http.end();

  if (response != nullptr) {
    *response = body;
  }

  Serial.printf("POST %s -> %d\n", path.c_str(), status);
  if (!body.isEmpty()) {
    Serial.println(body);
  }
  return status >= 200 && status < 300;
}

void beginHug(int pressure) {
  sessionId = makeSessionId();
  hugging = true;
  hugStartedAt = millis();
  lastHeartbeatAt = millis();
  peakPressure = pressure;
  pressureSum = pressure;
  pressureSamples = 1;

  const String json =
      "{\"device_id\":\"" + String(kDeviceId) +
      "\",\"session_id\":\"" + sessionId +
      "\",\"source\":\"esp32_fsr\",\"peak\":" + String(pressure) +
      ",\"average\":" + String(pressure) + "}";

  Serial.printf("拥抱开始：%s，力度 %d\n", levelName(pressure).c_str(), pressure);
  postJson("/api/hug/start", json);
}

void sendHeartbeat(int pressure) {
  peakPressure = max(peakPressure, pressure);
  pressureSum += pressure;
  ++pressureSamples;
  const int average = static_cast<int>(pressureSum / max(1UL, pressureSamples));

  const String json =
      "{\"device_id\":\"" + String(kDeviceId) +
      "\",\"session_id\":\"" + sessionId +
      "\",\"peak\":" + String(peakPressure) +
      ",\"average\":" + String(average) + "}";

  if (postJson("/api/hug/heartbeat", json)) {
    lastHeartbeatAt = millis();
  }
}

void endHug() {
  const float seconds = (millis() - hugStartedAt) / 1000.0f;
  const String endingSession = sessionId;
  const String json = "{\"session_id\":\"" + endingSession + "\"}";

  Serial.printf("拥抱结束：持续 %.1f 秒，峰值 %d\n", seconds, peakPressure);
  postJson("/api/hug/end", json);

  hugging = false;
  sessionId = "";
  peakPressure = 0;
  pressureSum = 0;
  pressureSamples = 0;
  candidateStartedAt = 0;
}

String configPage(const String& message = "") {
  return String(F(
      "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
      "<meta name='viewport' content='width=device-width,initial-scale=1'>"
      "<title>抱抱小熊配网</title><style>"
      "body{font-family:system-ui;background:#fff8f2;color:#3b2b2b;margin:0;padding:24px}"
      "main{max-width:520px;margin:auto;background:white;padding:24px;border-radius:22px;"
      "box-shadow:0 10px 35px #d7b9a955}"
      "h1{margin-top:0}label{display:block;margin:15px 0 6px;font-weight:650}"
      "input{box-sizing:border-box;width:100%;padding:12px;border:1px solid #d9c8bd;"
      "border-radius:12px;font:inherit}button{width:100%;margin-top:20px;padding:13px;"
      "border:0;border-radius:14px;background:#8f6652;color:white;font:inherit;font-weight:700}"
      ".note{font-size:14px;color:#745f54}.msg{padding:10px;background:#eef8ed;border-radius:10px}"
      "</style></head><body><main><h1>🧸 抱抱小熊</h1>")) +
      (message.isEmpty() ? "" : "<p class='msg'>" + htmlEscape(message) + "</p>") +
      "<form method='post' action='/save'>"
      "<label>Wi‑Fi 名称</label><input name='ssid' required value='" +
      htmlEscape(wifiSsid) + "'>"
      "<label>Wi‑Fi 密码</label><input name='password' type='password' value=''>"
      "<label>Zeabur 服务地址</label><input name='api' type='url' required "
      "placeholder='https://example.zeabur.app' value='" + htmlEscape(apiBase) + "'>"
      "<label>DEVICE_TOKEN</label><input name='token' type='password' required value=''>"
      "<button type='submit'>保存并连接</button></form>"
      "<p class='note'>配置只保存在小熊内部，不会写进公开仓库。"
      "热点密码：" + String(kSetupPassword) + "</p>"
      "<p class='note'>固件 " + String(kFirmwareVersion) + "</p>"
      "</main></body></html>";
}

void startSetupPortal(const String& reason) {
  setupMode = true;
  WiFi.mode(WIFI_AP);
  WiFi.softAP(kSetupSsid, kSetupPassword);

  server.on("/", HTTP_GET, [reason]() {
    server.send(200, "text/html; charset=utf-8", configPage(reason));
  });

  server.on("/save", HTTP_POST, []() {
    const String newSsid = server.arg("ssid");
    const String newPassword = server.arg("password");
    const String newApi = normalizeBaseUrl(server.arg("api"));
    const String newToken = server.arg("token");

    if (newSsid.isEmpty() || newApi.isEmpty() || newToken.isEmpty()) {
      server.send(400, "text/html; charset=utf-8", configPage("请把必填项填写完整。"));
      return;
    }

    prefs.begin("hugbear", false);
    prefs.putString("ssid", newSsid);
    prefs.putString("password", newPassword);
    prefs.putString("api", newApi);
    prefs.putString("token", newToken);
    prefs.end();

    server.send(
        200,
        "text/html; charset=utf-8",
        "<!doctype html><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'><body style='font-family:system-ui;"
        "padding:30px'><h1>保存成功 🧸</h1><p>小熊正在重新启动并连接 Wi‑Fi。</p></body>");
    delay(1200);
    ESP.restart();
  });

  server.begin();
  Serial.printf("配网热点：%s\n", kSetupSsid);
  Serial.println("浏览器打开：http://192.168.4.1");
}

bool connectSavedWifi() {
  prefs.begin("hugbear", true);
  wifiSsid = prefs.getString("ssid", "");
  wifiPassword = prefs.getString("password", "");
  apiBase = normalizeBaseUrl(prefs.getString("api", ""));
  deviceToken = prefs.getString("token", "");
  prefs.end();

  if (wifiSsid.isEmpty() || apiBase.isEmpty() || deviceToken.isEmpty()) {
    return false;
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(wifiSsid.c_str(), wifiPassword.c_str());
  Serial.printf("正在连接 Wi‑Fi：%s", wifiSsid.c_str());

  const unsigned long deadline = millis() + 20000;
  while (WiFi.status() != WL_CONNECTED && millis() < deadline) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  Serial.print("Wi‑Fi 已连接，IP：");
  Serial.println(WiFi.localIP());
  return true;
}

void maintainWifi() {
  if (WiFi.status() == WL_CONNECTED || millis() - lastReconnectAt < kReconnectMs) {
    return;
  }
  lastReconnectAt = millis();
  WiFi.disconnect();
  WiFi.begin(wifiSsid.c_str(), wifiPassword.c_str());
}

void processSensor() {
  const int pressure = readSmoothedPressure();

  if (!hugging) {
    if (pressure >= kTouchThreshold) {
      if (candidateStartedAt == 0) {
        candidateStartedAt = millis();
      } else if (millis() - candidateStartedAt >= kDebounceMs) {
        beginHug(pressure);
      }
    } else {
      candidateStartedAt = 0;
    }
  } else {
    peakPressure = max(peakPressure, pressure);
    pressureSum += pressure;
    ++pressureSamples;

    if (pressure <= kReleaseThreshold) {
      endHug();
    } else if (millis() - lastHeartbeatAt >= kHeartbeatMs) {
      sendHeartbeat(pressure);
    }
  }

  static unsigned long lastPrintAt = 0;
  if (millis() - lastPrintAt >= 500) {
    lastPrintAt = millis();
    Serial.printf("力度：%d｜状态：%s\n", pressure, levelName(pressure).c_str());
  }
}
}  // namespace

void setup() {
  Serial.begin(115200);
  analogReadResolution(12);
  pinMode(kFsrPin, INPUT);
  delay(800);

  Serial.printf("\n抱抱小熊固件 %s\n", kFirmwareVersion);
  if (!connectSavedWifi()) {
    startSetupPortal("首次使用或 Wi‑Fi 连接失败，请完成配置。");
  }
}

void loop() {
  if (setupMode) {
    server.handleClient();
    delay(2);
    return;
  }

  maintainWifi();
  processSensor();
  delay(30);
}
