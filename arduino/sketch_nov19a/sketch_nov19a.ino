
#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// RC522 pins
#define SS_PIN 5
#define RST_PIN 4
#define SCK_PIN 18
#define MISO_PIN 19
#define MOSI_PIN 23

// Output pins
#define GREEN_LED 25
#define RED_LED 26
#define BUZZER 27

// OLED settings
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
#define OLED_ADDRESS 0x3C

MFRC522 rfid(SS_PIN, RST_PIN);

Adafruit_SSD1306 display(
  SCREEN_WIDTH,
  SCREEN_HEIGHT,
  &Wire,
  OLED_RESET
);

bool oledReady = false;
String lastUID = "";
unsigned long lastScanTime = 0;
const unsigned long SCAN_COOLDOWN = 2000;

void showMessage(String line1, String line2 = "") {
  Serial.println(line1 + " " + line2);

  if (!oledReady) return;

  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);

  display.setCursor(0, 10);
  display.println(line1);

  display.setCursor(0, 30);
  display.println(line2);

  display.display();
}

void beep(int duration) {
  digitalWrite(BUZZER, HIGH);
  delay(duration);
  digitalWrite(BUZZER, LOW);
}

void successSignal() {
  digitalWrite(GREEN_LED, HIGH);
  beep(150);
  delay(350);
  digitalWrite(GREEN_LED, LOW);
}

void errorSignal() {
  digitalWrite(RED_LED, HIGH);
  beep(400);
  delay(200);
  digitalWrite(RED_LED, LOW);
}

String getUID() {
  String uid = "";

  for (byte i = 0; i < rfid.uid.size; i++) {
    if (rfid.uid.uidByte[i] < 0x10) {
      uid += "0";
    }

    uid += String(rfid.uid.uidByte[i], HEX);
  }

  uid.toUpperCase();
  return uid;
}

void setup() {
  Serial.begin(115200);

  pinMode(GREEN_LED, OUTPUT);
  pinMode(RED_LED, OUTPUT);
  pinMode(BUZZER, OUTPUT);

  digitalWrite(GREEN_LED, LOW);
  digitalWrite(RED_LED, LOW);
  digitalWrite(BUZZER, LOW);

  // Initialize OLED
  Wire.begin(21, 22);

  oledReady = display.begin(
    SSD1306_SWITCHCAPVCC,
    OLED_ADDRESS
  );

  if (!oledReady) {
    Serial.println("OLED initialization failed!");
  }

  showMessage("IoT Inventory", "Starting...");

  // Initialize RFID reader
  SPI.begin(SCK_PIN, MISO_PIN, MOSI_PIN, SS_PIN);
  rfid.PCD_Init();

  delay(500);

  byte version = rfid.PCD_ReadRegister(
    MFRC522::VersionReg
  );

  Serial.print("RC522 version: 0x");
  Serial.println(version, HEX);

  if (version == 0x00 || version == 0xFF) {
    showMessage("RFID ERROR", "Check wiring!");
    errorSignal();
  } else {
    showMessage("System Ready", "Scan RFID Card");
    beep(100);
  }
}

void loop() {
  // Wait for a card
  if (!rfid.PICC_IsNewCardPresent()) {
    return;
  }

  // Read card UID
  if (!rfid.PICC_ReadCardSerial()) {
    showMessage("Read Failed", "Try Again");
    errorSignal();
    return;
  }

  String uid = getUID();

  // Ignore repeated scans during cooldown
  if (uid == lastUID &&
      millis() - lastScanTime < SCAN_COOLDOWN) {
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();
    return;
  }

  lastUID = uid;
  lastScanTime = millis();

  Serial.println("----------------");
  Serial.print("RFID UID: ");
  Serial.println(uid);

  showMessage("RFID Detected", uid);
  successSignal();

  // End communication with the card
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();

  delay(700);
  showMessage("System Ready", "Scan RFID Card");
}
