/*
  Hug Bear Touch · ESP32-S3 单 FSR 骨架

  现在先不用烧。硬件到货后，我们会根据你那块开发板的实物：
  1) 确认 ADC GPIO
  2) 测真实空闲/按压值
  3) 再填写 SERVER_URL / DEVICE_TOKEN

  功能：
  - 已保存 Wi-Fi 自动尝试连接
  - 全部失败时开启 HugBear-Setup 热点，手机浏览器访问 192.168.4.1 配网
  - FSR 触发后在本地聚合成一次事件，松手后只上传一次，省流量
*/

#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <HTTPClient.h>

// ===== 硬件到货后确认 =====
const int FSR_PIN = 4;              // 占位：必须按实际 ESP32-S3 板子确认
const int TRIGGER_THRESHOLD = 200;  // 占位：按真实 FSR 数据校准
const int RELEASE_THRESHOLD = 120;  // 占位：应低于触发阈值，防抖

// ===== Zeabur 部署后填写 =====
const char* SERVER_URL = "https://YOUR-DOMAIN/api/touch";
const char* DEVICE_TOKEN = "PASTE-YOUR-DEVICE-TOKEN-HERE";
const char* DEVICE_ID = "hug-bear-01";

Preferences prefs;
WebServer portal(80);

struct Net { String ssid; String pass; };
Net nets[3];

bool touching = false;
unsigned long touchStart = 0;
unsigned long lastAbove = 0;
long sumValue = 0;
unsigned long sampleCount = 0;
int peakValue = 0;

void loadNetworks() {
  prefs.begin("hugwifi", false);
  for (int i = 0; i < 3; i++) {
    nets[i].ssid = prefs.getString(("ssid" + String(i)).c_str(), "");
    nets[i].pass = prefs.getString(("pass" + String(i)).c_str(), "");
  }
}

void saveNetwork(const String& ssid, const String& pass) {
  int slot = 0;
  for (int i = 0; i < 3; i++) {
    if (nets[i].ssid == ssid) { slot = i; goto save; }
    if (nets[i].ssid.length() == 0) { slot = i; goto save; }
  }
  slot = prefs.getInt("nextslot", 0) % 3;
  prefs.putInt("nextslot", (slot + 1) % 3);
save:
  nets[slot].ssid = ssid;
  nets[slot].pass = pass;
  prefs.putString(("ssid" + String(slot)).c_str(), ssid);
  prefs.putString(("pass" + String(slot)).c_str(), pass);
}

bool tryKnownNetworks() {
  WiFi.mode(WIFI_STA);
  for (int i = 0; i < 3; i++) {
    if (nets[i].ssid.length() == 0) continue;
    Serial.printf("Trying Wi-Fi: %s\n", nets[i].ssid.c_str());
    WiFi.begin(nets[i].ssid.c_str(), nets[i].pass.c_str());
    unsigned long start = millis();
    while (millis() - start < 9000) {
      if (WiFi.status() == WL_CONNECTED) {
        Serial.print("Connected, IP: ");
        Serial.println(WiFi.localIP());
        return true;
      }
      delay(250);
    }
    WiFi.disconnect(true);
    delay(200);
  }
  return false;
}

String pageHtml() {
  return R"HTML(
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HugBear 配网</title></head><body style="font-family:system-ui;max-width:560px;margin:30px auto;padding:0 18px">
<h2>🧸 HugBear 配网</h2><p>输入当前位置的 Wi-Fi。保存后小熊会重启并尝试连接。</p>
<form method="POST" action="/save"><p>Wi-Fi 名称<br><input name="ssid" style="width:100%;padding:10px"></p>
<p>密码<br><input name="pass" type="password" style="width:100%;padding:10px"></p>
<button style="padding:10px 18px">保存并重启</button></form></body></html>
)HTML";
}

void startPortal() {
  WiFi.mode(WIFI_AP);
  WiFi.softAP("HugBear-Setup");
  Serial.println("Config AP: HugBear-Setup");
  Serial.println("Open http://192.168.4.1");

  portal.on("/", HTTP_GET, []() { portal.send(200, "text/html; charset=utf-8", pageHtml()); });
  portal.on("/save", HTTP_POST, []() {
    String ssid = portal.arg("ssid");
    String pass = portal.arg("pass");
    if (ssid.length() == 0) {
      portal.send(400, "text/plain", "SSID required");
      return;
    }
    saveNetwork(ssid, pass);
    portal.send(200, "text/html; charset=utf-8", "<h3>保存成功，小熊正在重启…</h3>");
    delay(1200);
    ESP.restart();
  });
  portal.begin();

  while (true) {
    portal.handleClient();
    delay(10);
  }
}

bool sendEvent(int peak, int average, unsigned long durationMs) {
  if (WiFi.status() != WL_CONNECTED && !tryKnownNetworks()) return false;

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Token", DEVICE_TOKEN);

  String body = "{\"device_id\":\"" + String(DEVICE_ID) +
                "\",\"peak\":" + String(peak) +
                ",\"average\":" + String(average) +
                ",\"duration_ms\":" + String(durationMs) + "}";

  int code = http.POST(body);
  String response = http.getString();
  Serial.printf("POST %d %s\n", code, response.c_str());
  http.end();
  return code >= 200 && code < 300;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  analogReadResolution(12);
  loadNetworks();

  if (!tryKnownNetworks()) startPortal();
}

void loop() {
  int v = analogRead(FSR_PIN);
  unsigned long now = millis();

  if (!touching && v >= TRIGGER_THRESHOLD) {
    touching = true;
    touchStart = now;
    lastAbove = now;
    sumValue = v;
    sampleCount = 1;
    peakValue = v;
  } else if (touching) {
    sumValue += v;
    sampleCount++;
    if (v > peakValue) peakValue = v;
    if (v >= RELEASE_THRESHOLD) lastAbove = now;

    // 连续约 180ms 低于释放阈值，视为松开
    if (now - lastAbove > 180) {
      unsigned long durationMs = lastAbove - touchStart;
      int average = sampleCount ? (int)(sumValue / sampleCount) : peakValue;
      Serial.printf("Touch end: peak=%d avg=%d duration=%lums\n", peakValue, average, durationMs);
      if (durationMs >= 20) sendEvent(peakValue, average, durationMs);
      touching = false;
    }
  }

  delay(60);
}
