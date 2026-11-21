#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <ArduinoJson.h>  // ArduinoJson 7
#include <esp_system.h>
#include <stdint.h>
#include <string.h>
#include <ctype.h>

#if __has_include("scanner_config.local.h")
#include "scanner_config.local.h"
#else
#error "Copy scanner_config.example.h to scanner_config.local.h and configure privately."
#endif

#define SS_PIN 5
#define RST_PIN 4
#define SCK_PIN 18
#define MISO_PIN 19
#define MOSI_PIN 23
#define GREEN_LED 25
#define RED_LED 26
#define BUZZER 27
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
#define OLED_ADDRESS 0x3C

static const uint32_t SCAN_COOLDOWN = 2000;
static const uint32_t WIFI_CONNECT_MS = 15000;
static const uint32_t WIFI_RECONNECT_MS = 10000;
static const uint32_t HTTP_CONNECT_MS = 3000;
static const uint32_t HTTP_READ_MS = 4000;
static const uint32_t MAX_PENDING_MS = 90000;
static const uint32_t MAX_RETRY_MS = 60000;
static const unsigned MAX_ATTEMPTS = 3;
static const uint64_t MAX_SEQUENCE = 0x7fffffffffffffffULL;
static const size_t RESPONSE_LIMIT = 1024;

MFRC522 rfid(SS_PIN, RST_PIN);
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
Preferences sequenceStore;
bool oledReady = false, operational = false, wifiConnecting = false;
bool previouslyConnected = false, wifiAttempted = false;
uint32_t wifiStarted = 0, wifiLastAttempt = 0, lastScanTime = 0, readyAt = 0;
uint32_t serverNotBefore = 0;
bool serverPaused = false;
String lastUID;
uint64_t highWater = 0;

struct PendingScan {
  bool active = false;
  String id, session, uid, body;
  uint64_t sequence = 0;
  unsigned attempts = 0;
  uint32_t created = 0, nextAttempt = 0;
} pending;

bool due(uint32_t now, uint32_t target) {
  return static_cast<int32_t>(now - target) >= 0;
}

// Only fixed diagnostics: never print config, URLs, UIDs, headers or responses.
void showMessage(const char *line1, const char *line2 = "") {
  Serial.println(line1);
  if (!oledReady) return;
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 10); display.println(line1);
  display.setCursor(0, 30); display.println(line2);
  display.display();
}
void beep(unsigned duration) {
  digitalWrite(BUZZER, HIGH); delay(duration); digitalWrite(BUZZER, LOW);
}
void successSignal() {
  digitalWrite(GREEN_LED, HIGH); beep(150); delay(350); digitalWrite(GREEN_LED, LOW);
}
void errorSignal() {
  digitalWrite(RED_LED, HIGH); beep(400); delay(200); digitalWrite(RED_LED, LOW);
}

bool validUuid(const char *s) {
  if (strlen(s) != 36) return false;
  for (unsigned i = 0; i < 36; ++i) {
    if (i == 8 || i == 13 || i == 18 || i == 23) {
      if (s[i] != '-') return false;
    } else if (!isxdigit(static_cast<unsigned char>(s[i]))) return false;
  }
  return strcmp(s, "00000000-0000-0000-0000-000000000000") != 0;
}
bool validSecret(const char *s) {
  if (strlen(s) != 43) return false;
  for (unsigned i = 0; i < 43; ++i) {
    char c = s[i];
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_')) return false;
  }
  // The last Base64 character has two zero pad bits for a 32-byte key.
  return strchr("AEIMQUYcgkosw048", s[42]) != nullptr;
}
bool validConfiguration() {
  if (!ALLOW_TRUSTED_LAN_HTTP || !WIFI_SSID[0] || !WIFI_PASSWORD[0] ||
      strstr(WIFI_SSID, "REPLACE_") || strstr(WIFI_PASSWORD, "REPLACE_") ||
      !validUuid(SCANNER_UUID) || !validUuid(CAPTURE_SESSION_UUID) ||
      !validSecret(SCANNER_DEVICE_SECRET)) return false;
  String url(FASTAPI_BASE_URL);
  if (!url.startsWith("http://")) return false;
  String authority = url.substring(7);
  int colon = authority.indexOf(':');
  String host = colon < 0 ? authority : authority.substring(0, colon);
  IPAddress ip;
  if (!ip.fromString(host) || ip.toString() != host) return false;
  bool privateIp = ip[0] == 10 || (ip[0] == 172 && ip[1] >= 16 && ip[1] <= 31) ||
                   (ip[0] == 192 && ip[1] == 168);
  if (!privateIp) return false;
  if (colon >= 0) {
    String port = authority.substring(colon + 1);
    if (!port.length() || port.length() > 5) return false;
    for (unsigned i = 0; i < port.length(); ++i) if (!isDigit(port[i])) return false;
    if (port.toInt() < 1 || port.toInt() > 65535) return false;
  }
  return true;
}
String newScanUuid() {
  uint8_t bytes[16];
  esp_fill_random(bytes, sizeof(bytes)); // Wi-Fi is enabled before reading scans.
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  char out[37];
  snprintf(out, sizeof(out),
           "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
           bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
           bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]);
  return String(out);
}
bool openSequenceStore() {
  if (!sequenceStore.begin("equip-scan", false)) return false;
  bool hasId = sequenceStore.isKey("scanner"), hasSequence = sequenceStore.isKey("sequence");
  if (hasId != hasSequence) return false;
  if (!hasId) {
    if (sequenceStore.putString("scanner", SCANNER_UUID) == 0 ||
        sequenceStore.putULong64("sequence", 0) != sizeof(uint64_t)) return false;
  }
  if (sequenceStore.getString("scanner", "") != String(SCANNER_UUID)) return false;
  highWater = sequenceStore.getULong64("sequence", UINT64_MAX);
  return highWater <= MAX_SEQUENCE;
}
bool reserveSequence(uint64_t &reserved) {
  if (highWater >= MAX_SEQUENCE) return false;
  uint64_t next = highWater + 1;
  // Persist BEFORE network I/O. Failures burn numbers; never clear or decrement.
  if (sequenceStore.putULong64("sequence", next) != sizeof(uint64_t) ||
      sequenceStore.getULong64("sequence", UINT64_MAX) != next) return false;
  highWater = next; reserved = next; return true;
}

class BoundedResponse : public Stream {
 public:
  char data[RESPONSE_LIMIT + 1] = {};
  size_t used = 0;
  bool overflow = false;
  uint32_t deadline = millis() + 8000;
  size_t write(uint8_t c) override { return write(&c, 1); }
  size_t write(const uint8_t *buffer, size_t size) override {
    if (due(millis(), deadline) || size > RESPONSE_LIMIT - used) { overflow = true; return 0; }
    memcpy(data + used, buffer, size); used += size; data[used] = '\0'; return size;
  }
  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }
  void flush() override {}
};
void finishFailure(const char *message, const char *detail = "Scan not confirmed") {
  pending = PendingScan();
  showMessage(message, detail); errorSignal(); readyAt = millis() + 2000;
}
// Numeric Retry-After only. With no trusted clock, HTTP dates/long delays cause
// abandonment instead of retrying earlier than the server requested.
bool retryDelay(const String &header, uint32_t &wait) {
  if (!header.length()) return true;
  if (header.length() > 5) return false;
  uint32_t seconds = 0;
  for (unsigned i = 0; i < header.length(); ++i) {
    if (!isDigit(header[i])) return false;
    seconds = seconds * 10 + (header[i] - '0');
  }
  if (seconds > MAX_RETRY_MS / 1000) return false;
  if (seconds * 1000 > wait) wait = seconds * 1000;
  return true;
}
void transientFailure(const String &retryAfter) {
  uint32_t wait = 1000UL << (pending.attempts ? pending.attempts - 1 : 0);
  uint32_t age = millis() - pending.created;
  if (!retryDelay(retryAfter, wait)) {
    // Do not immediately accept new cards and evade an unsupported Retry-After.
    operational = false;
    finishFailure("Server Backoff", "Operator required"); return;
  }
  if (retryAfter.length()) {
    serverNotBefore = millis() + wait; serverPaused = true;
  }
  if (pending.attempts >= MAX_ATTEMPTS ||
      age >= MAX_PENDING_MS || wait >= MAX_PENDING_MS - age) {
    finishFailure("Network Error", "Outcome uncertain"); return;
  }
  pending.nextAttempt = millis() + wait;
  showMessage(retryAfter.length() ? "Server Backoff" : "Network Error", "Retry scheduled"); errorSignal();
}
bool acceptedReceipt(const char *body, bool &duplicate) {
  JsonDocument receipt;
  if (deserializeJson(receipt, body)) return false;
  if (!receipt["scan_id"].is<const char *>() || pending.id != receipt["scan_id"].as<String>() ||
      !receipt["sequence"].is<uint64_t>() || receipt["sequence"].as<uint64_t>() != pending.sequence ||
      !receipt["uid"].is<const char *>() || pending.uid != receipt["uid"].as<String>() ||
      !receipt["duplicate"].is<bool>() || !receipt["asset_id"].is<const char *>() ||
      !validUuid(receipt["asset_id"].as<const char *>())) return false;
  duplicate = receipt["duplicate"].as<bool>(); return true;
}
void submitPending() {
  ++pending.attempts;
  showMessage("Sending Scan");
  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(HTTP_CONNECT_MS); http.setTimeout(HTTP_READ_MS);
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
  http.useHTTP10(true);
  if (!http.begin(client, String(FASTAPI_BASE_URL) + "/device-scans")) {
    http.end(); transientFailure(""); return;
  }
  const char *headers[] = {"Retry-After"};
  http.collectHeaders(headers, 1);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Scanner ") + SCANNER_UUID + "." + SCANNER_DEVICE_SECRET);
  int status = http.POST(pending.body);
  String retryAfter = http.header("Retry-After");
  if (status == 200) {
    BoundedResponse response;
    bool duplicate = false;
    bool valid = http.getSize() <= static_cast<int>(RESPONSE_LIMIT);
    if (valid) valid = http.writeToStream(&response) >= 0 && !response.overflow;
    valid = valid && acceptedReceipt(response.data, duplicate);
    http.end();
    if (!valid) { transientFailure(retryAfter); return; }
    pending = PendingScan();
    showMessage("Scan Accepted", duplicate ? "Already received" : "Equipment staged");
    successSignal(); readyAt = millis() + 2000; return;
  }
  bool expiredSession = false;
  if (status == 409) {
    BoundedResponse response;
    JsonDocument error;
    if (http.getSize() <= static_cast<int>(RESPONSE_LIMIT) &&
        http.writeToStream(&response) >= 0 && !response.overflow &&
        !deserializeJson(error, response.data)) {
      const char *detail = error["detail"] | "";
      expiredSession = strcmp(detail, "Scanner has no active capture session") == 0;
    }
  }
  // Never print arbitrary response bodies, URLs, headers or library diagnostics.
  http.end();
  if (status < 0 || status == 408 || status == 429 || (status >= 500 && status <= 599)) {
    if (status == 429 && !retryAfter.length()) retryAfter = "60";
    transientFailure(retryAfter);
  } else if (status == 401 || status == 403) {
    operational = false; finishFailure("Scanner Denied", "Check provisioning");
  } else if (status == 404) {
    finishFailure("Unknown Equipment");
  } else if (status == 409) {
    // This status also covers sequence conflicts and UID cooldown, not just expiry.
    finishFailure(expiredSession ? "Session Expired" : "Session / Conflict", "Check session/seq");
  } else if (status == 413 || status == 422 || status == 400) {
    finishFailure("Invalid Scan", "Check configuration");
  } else {
    finishFailure("Scan Rejected", "Check API setup");
  }
}
void maintainWifi() {
  uint32_t now = millis();
  if (WiFi.status() == WL_CONNECTED) {
    wifiConnecting = false;
    if (!previouslyConnected) {
      showMessage("Wi-Fi Connected", pending.active ? "Retry pending" : "Ready to Scan");
      previouslyConnected = true;
    }
    return;
  }
  if (previouslyConnected) {
    previouslyConnected = false; showMessage("Wi-Fi disconnected", "Reconnecting");
  }
  if (wifiConnecting && now - wifiStarted >= WIFI_CONNECT_MS) {
    WiFi.disconnect(false, false); wifiConnecting = false;
    showMessage("Network Error", "Wi-Fi unavailable");
  }
  if (!wifiConnecting && (!wifiAttempted || now - wifiLastAttempt >= WIFI_RECONNECT_MS)) {
    wifiAttempted = true; wifiConnecting = true; wifiStarted = wifiLastAttempt = now;
    showMessage("Connecting Wi-Fi");
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  }
}
String getUID() {
  String uid;
  for (byte i = 0; i < rfid.uid.size; ++i) {
    if (rfid.uid.uidByte[i] < 0x10) uid += "0";
    uid += String(rfid.uid.uidByte[i], HEX);
  }
  uid.toUpperCase(); return uid;
}
void setup() {
  Serial.begin(115200);
  pinMode(GREEN_LED, OUTPUT); pinMode(RED_LED, OUTPUT); pinMode(BUZZER, OUTPUT);
  digitalWrite(GREEN_LED, LOW); digitalWrite(RED_LED, LOW); digitalWrite(BUZZER, LOW);
  Wire.begin(21, 22);
  oledReady = display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS);
  showMessage("IoT Inventory", "Starting");
  SPI.begin(SCK_PIN, MISO_PIN, MOSI_PIN, SS_PIN);
  rfid.PCD_Init(); delay(500);
  byte version = rfid.PCD_ReadRegister(MFRC522::VersionReg);
  Serial.print("RC522 version: 0x"); Serial.println(version, HEX);
  if (version == 0x00 || version == 0xFF) {
    showMessage("RFID ERROR", "Check wiring"); errorSignal(); return;
  }
  if (!validConfiguration()) {
    showMessage("Configuration Error", "Check local header"); errorSignal(); return;
  }
  if (!openSequenceStore()) {
    showMessage("Sequence Error", "Operator required"); errorSignal(); return;
  }
  WiFi.persistent(false); WiFi.mode(WIFI_STA); WiFi.setAutoReconnect(false);
  operational = true; maintainWifi();
}
void loop() {
  if (!operational) { delay(20); return; }
  maintainWifi();
  uint32_t now = millis();
  if (serverPaused && due(now, serverNotBefore)) serverPaused = false;
  if (pending.active) {
    if (now - pending.created >= MAX_PENDING_MS) {
      finishFailure("Network Error", "Outcome uncertain");
    } else if (!serverPaused && WiFi.status() == WL_CONNECTED && due(now, pending.nextAttempt)) {
      submitPending();
    }
    delay(10); return;  // one event at a time; no unbounded offline queue
  }
  if (serverPaused || WiFi.status() != WL_CONNECTED || (readyAt && !due(now, readyAt))) {
    delay(10); return;
  }
  if (readyAt) { readyAt = 0; showMessage("Ready to Scan"); }
  // Halted cards do not answer REQA: do not use WUPA/reinitialize for held cards.
  if (!rfid.PICC_IsNewCardPresent()) { delay(10); return; }
  if (!rfid.PICC_ReadCardSerial()) {
    rfid.PICC_HaltA(); rfid.PCD_StopCrypto1();
    showMessage("Read Failed", "Try again"); errorSignal(); return;
  }
  bool supported = rfid.uid.size == 4 || rfid.uid.size == 7 || rfid.uid.size == 10;
  String uid = supported ? getUID() : String();
  rfid.PICC_HaltA(); rfid.PCD_StopCrypto1();  // cleanup before all network/error paths
  if (!supported) { finishFailure("Invalid RFID UID"); return; }
  if (uid == lastUID && now - lastScanTime < SCAN_COOLDOWN) return;
  lastUID = uid; lastScanTime = now;
  if (!reserveSequence(pending.sequence)) {
    operational = false; finishFailure("Sequence Error", "Operator required"); return;
  }
  pending.id = newScanUuid();
  pending.session = String(CAPTURE_SESSION_UUID); pending.session.toLowerCase();
  pending.uid = uid; pending.attempts = 0;
  JsonDocument request;
  request["scan_id"] = pending.id; request["capture_session_id"] = pending.session;
  request["sequence"] = pending.sequence; request["uid"] = pending.uid;
  serializeJson(request, pending.body);
  pending.created = pending.nextAttempt = millis(); pending.active = true;
}
