// MiMu-style glove — ESP32 + BNO08x firmware
//
// The BNO08x does sensor fusion on-chip, so we stream ready-made
// orientation (Euler angles) plus linear acceleration over BLE at ~100 Hz.
// Sensor init and quaternion->Euler math merged from the user's original
// serial sketch (see ../reference/bno08x_serial_original.ino).
//
// Wiring (GY-BNO08x -> ESP32):
//   VCC -> 3V3,  GND -> GND,  SDA -> GPIO21,  SCL -> GPIO22,  RST -> GPIO4
//
// This firmware NEVER fabricates motion. If the sensor is missing it keeps
// retrying init and reports status=0 so the laptop can say so plainly.
//
// Wiring (flex sensors): 3V3 -> flex -> GPIO34 -> 47k -> GND
//                        3V3 -> flex -> GPIO35 -> 47k -> GND
//
// Packet format (36 bytes, little-endian, must match ble_receiver.py):
//   float roll, pitch, yaw;  // degrees, absolute (laptop zeroes them)
//   float lax, lay, laz;     // linear acceleration, m/s^2 (gravity removed)
//   float status;            // 1 = live sensor, 0 = sensor not detected
//   float flex, flex2;       // raw ADC 0..4095 from the two flex dividers

#include <Adafruit_BNO08x.h>
#include <ESP_I2S.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <Wire.h>
#include <math.h>

#define DEVICE_NAME  "MIMU-GLOVE"
#define SERVICE_UUID "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define CHAR_UUID    "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
#define AUDIO_UUID   "6e400004-b5a3-f393-e0a9-e50e24dcca9e"

#define BNO08X_I2C_ADDR 0x4B
#define BNO_RST 4
// Flex sensor divider. GPIO34 is input-only and on ADC1, which (unlike ADC2)
// keeps working while the BLE radio is active.
#define FLEX_PIN  34
#define FLEX2_PIN 35   // second finger; ADC1, input-only
// Record button, status LED and the INMP441 microphone.
#define BTN_PIN     18
#define LED_PIN     19
#define I2S_SCK     33
#define I2S_WS      25
#define I2S_SD      32
#define AUDIO_RATE  16000
#define MAX_SECONDS 8
#define MAX_SAMPLES (AUDIO_RATE * MAX_SECONDS)
#define AUDIO_CHUNK 180   // bytes per BLE notification during a transfer
// 50 Hz is plenty for gesture control and leaves the I2C bus ~8x headroom;
// asking the BNO08x for 100 Hz on two reports wedges its SHTP transport.
#define SAMPLE_HZ 50
#define REPORT_US 20000  // 20 ms -> 50 Hz sensor reports

// No reset pin in the constructor: we drive it manually below, because the
// BNO08x needs a long (~500 ms) settle time after reset that the library's
// built-in pulse does not give it.
Adafruit_BNO08x bno08x;
sh2_SensorValue_t sensorValue;
BLECharacteristic *sensorChar;
BLECharacteristic *audioChar;
bool deviceConnected = false;
bool imuPresent = false;
uint32_t lastReportMs = 0;  // watchdog: when did a real sensor report arrive
uint32_t nextRetryMs = 0;   // rate-limit recovery attempts
int reEnableCount = 0;      // failed soft recoveries before a hard re-init

// Latest sensor readings, updated as reports arrive.
float roll = 0, pitch = 0, yaw = 0;
float lax = 0, lay = 0, laz = 0;
float flex = 0, flex2 = 0;  // smoothed raw ADC; the laptop calibrates these

// Microphone / recording state.
I2SClass i2s;
bool micOK = false, recording = false;
size_t maxSamples = 0;   // however many the allocation gave us
int16_t *audioBuf = nullptr;     // allocated in PSRAM at boot
size_t audioLen = 0;
int ledMode = 0;                 // 0 idle, 1 recording, 2 sending, 3 error

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *) override { deviceConnected = true; }
  void onDisconnect(BLEServer *srv) override {
    deviceConnected = false;
    srv->getAdvertising()->start();  // let the laptop reconnect
  }
};

void setReports() {
  if (!bno08x.enableReport(SH2_ROTATION_VECTOR, REPORT_US))
    Serial.println("Could not enable Rotation Vector");
  if (!bno08x.enableReport(SH2_LINEAR_ACCELERATION, REPORT_US))
    Serial.println("Could not enable Linear Acceleration");
  // NOTE: deliberately does not touch lastReportMs — only a real incoming
  // report may reset the watchdog, or it can never escalate to a re-init.
}

// Unstick the I2C bus. If the sensor locked up mid-transfer it can hold SDA
// low forever, which makes every device — including itself — invisible to a
// scan. Clocking SCL manually lets it finish that transfer and release SDA.
void recoverI2CBus() {
  Wire.end();
  pinMode(22, OUTPUT);      // SCL
  pinMode(21, INPUT_PULLUP);  // SDA
  for (int i = 0; i < 10 && digitalRead(21) == LOW; i++) {
    digitalWrite(22, LOW);
    delayMicroseconds(5);
    digitalWrite(22, HIGH);
    delayMicroseconds(5);
  }
  // Manual STOP condition so the bus is left idle.
  pinMode(21, OUTPUT);
  digitalWrite(21, LOW);
  delayMicroseconds(5);
  digitalWrite(22, HIGH);
  delayMicroseconds(5);
  digitalWrite(21, HIGH);
  delayMicroseconds(5);
  Wire.begin(21, 22);
  Wire.setClock(100000);  // slower bus: safer over long jumper wires
}

// Hard-reset the sensor the way the original sketch did: hold RST low,
// release, then give it half a second to boot before talking to it.
void hardResetSensor() {
  pinMode(BNO_RST, OUTPUT);
  digitalWrite(BNO_RST, LOW);
  delay(20);
  digitalWrite(BNO_RST, HIGH);
  delay(500);
}

void scanI2C() {
  Serial.print("I2C devices found:");
  bool any = false;
  for (uint8_t addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      Serial.printf(" 0x%02X", addr);
      any = true;
    }
  }
  Serial.println(any ? "" : " NONE (check wiring: VCC/GND/SDA=21/SCL=22)");
}

// Try both common BNO08x addresses, with retries — a cold sensor sometimes
// needs a second attempt after reset.
bool initSensor() {
  for (int attempt = 1; attempt <= 3; attempt++) {
    recoverI2CBus();
    hardResetSensor();
    if (attempt > 1) scanI2C();  // show whether it is even on the bus
    for (uint8_t addr : {(uint8_t)BNO08X_I2C_ADDR, (uint8_t)0x4A}) {
      if (bno08x.begin_I2C(addr, &Wire)) {
        Serial.printf("BNO08X found at 0x%02X (attempt %d)\n", addr, attempt);
        setReports();
        lastReportMs = millis();
        reEnableCount = 0;
        return true;
      }
    }
    Serial.printf("BNO08X init attempt %d failed\n", attempt);
  }
  return false;
}

// Status LED. Non-blocking so the pattern survives audio work.
void updateLed() {
  uint32_t t = millis();
  bool on = false;
  if (ledMode == 1) on = true;                 // recording: solid
  else if (ledMode == 2) on = (t / 400) % 2;   // sending: slow blink
  else if (ledMode == 3) on = (t / 120) % 2;   // error: fast blink
  digitalWrite(LED_PIN, on);
}

bool buttonHeld() {
  static bool stable = false, lastRaw = false;
  static uint32_t lastChange = 0;
  bool raw = digitalRead(BTN_PIN) == LOW;
  if (raw != lastRaw) { lastRaw = raw; lastChange = millis(); }
  if (millis() - lastChange > 30) stable = raw;
  return stable;
}

// Ship the take to the laptop: a 12-byte header, then raw little-endian
// int16 samples. Latency does not matter here - the recording is already
// finished - which is why this works where live streaming would not.
void sendAudio() {
  if (!deviceConnected || audioLen == 0) return;
  ledMode = 2;
  uint8_t hdr[12];
  memcpy(hdr, "AUD0", 4);
  uint32_t n = audioLen, r = AUDIO_RATE;
  memcpy(hdr + 4, &n, 4);
  memcpy(hdr + 8, &r, 4);
  audioChar->setValue(hdr, sizeof(hdr));
  audioChar->notify();
  delay(20);

  const uint8_t *data = (const uint8_t *)audioBuf;
  size_t total = audioLen * sizeof(int16_t), sent = 0;
  while (sent < total && deviceConnected) {
    size_t n2 = min((size_t)AUDIO_CHUNK, total - sent);
    audioChar->setValue((uint8_t *)(data + sent), n2);
    audioChar->notify();
    sent += n2;
    updateLed();
    delay(6);   // let the BLE stack drain; without this notifications are lost
  }
  Serial.printf("audio sent: %u samples (%.2f s)\n",
                (unsigned)audioLen, (float)audioLen / AUDIO_RATE);
  ledMode = 0;
  audioLen = 0;
}

void setup() {
  Serial.begin(115200);
  delay(100);
  Serial.println("========== MIMU GLOVE (BLE) ==========");

  Wire.begin(21, 22);
  // 100 kHz, NOT 400 kHz. Measured: at 400 kHz over dupont jumpers the
  // BNO08x wedged every ~15-20 s (it kept ACKing its address but would not
  // re-initialize); at 100 kHz it ran 90 s under BLE load with zero dropouts.
  Wire.setClock(100000);
  delay(100);

  pinMode(BTN_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);

  // The audio buffer lives in PSRAM: this is a WROVER module, and internal
  // RAM has no room for it beside the BLE stack. Falls back to a shorter
  // take in internal RAM if PSRAM is unavailable.
  audioBuf = (int16_t *)ps_malloc(MAX_SAMPLES * sizeof(int16_t));
  if (audioBuf) {
    maxSamples = MAX_SAMPLES;
    Serial.printf("Audio buffer: %d s in PSRAM.\n", MAX_SECONDS);
  } else {
    maxSamples = AUDIO_RATE * 2;
    audioBuf = (int16_t *)malloc(AUDIO_RATE * 2 * sizeof(int16_t));
    Serial.println("No PSRAM - falling back to a 2 s buffer in internal RAM.");
  }

  i2s.setPins(I2S_SCK, I2S_WS, -1, I2S_SD);
  micOK = i2s.begin(I2S_MODE_STD, AUDIO_RATE, I2S_DATA_BIT_WIDTH_32BIT,
                    I2S_SLOT_MODE_MONO);
  Serial.println(micOK ? "Microphone ready." : "Microphone FAILED to start.");

  hardResetSensor();
  scanI2C();

  imuPresent = initSensor();
  if (!imuPresent)
    Serial.println("BNO08X not found - will keep retrying (no fake data sent).");

  BLEDevice::init(DEVICE_NAME);
  BLEDevice::setMTU(517);   // big MTU so audio chunks fit in one packet
  BLEServer *server = BLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  BLEService *service = server->createService(SERVICE_UUID);
  sensorChar = service->createCharacteristic(
      CHAR_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  sensorChar->addDescriptor(new BLE2902());
  audioChar = service->createCharacteristic(
      AUDIO_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  audioChar->addDescriptor(new BLE2902());
  service->start();
  server->getAdvertising()->start();
  Serial.println("Advertising as MIMU-GLOVE");
}

void readSensor() {
  if (bno08x.wasReset()) {
    Serial.println("Sensor reset.");
    delay(100);
    setReports();
  }
  // Drain everything that has arrived since last loop.
  while (bno08x.getSensorEvent(&sensorValue)) {
    lastReportMs = millis();
    if (sensorValue.sensorId == SH2_ROTATION_VECTOR) {
      float qr = sensorValue.un.rotationVector.real;
      float qi = sensorValue.un.rotationVector.i;
      float qj = sensorValue.un.rotationVector.j;
      float qk = sensorValue.un.rotationVector.k;
      yaw = atan2(2.0f * (qr * qk + qi * qj),
                  1.0f - 2.0f * (qj * qj + qk * qk)) * 180.0f / PI;
      pitch = asin(constrain(2.0f * (qr * qj - qk * qi), -1.0f, 1.0f))
              * 180.0f / PI;
      roll = atan2(2.0f * (qr * qi + qj * qk),
                   1.0f - 2.0f * (qi * qi + qj * qj)) * 180.0f / PI;
    } else if (sensorValue.sensorId == SH2_LINEAR_ACCELERATION) {
      lax = sensorValue.un.linearAcceleration.x;
      lay = sensorValue.un.linearAcceleration.y;
      laz = sensorValue.un.linearAcceleration.z;
    }
  }

  // Watchdog: reports stop arriving if the SHTP transport wedges (a known
  // BNO08x quirk, provoked here by BLE traffic). Try re-enabling reports a
  // few times, then escalate to a full hardware reset + re-init.
  uint32_t now = millis();
  uint32_t since = now - lastReportMs;
  if (since > 1000 && now > nextRetryMs) {
    nextRetryMs = now + 1000;
    if (reEnableCount < 2) {
      reEnableCount++;
      Serial.printf("No reports for %lums - re-enabling (%d).\n",
                    (unsigned long)since, reEnableCount);
      setReports();
    } else {
      Serial.println("Soft recovery failed - hard resetting sensor.");
      imuPresent = initSensor();
      lastReportMs = millis();
      reEnableCount = 0;
      if (!imuPresent) {
        // The sensor still ACKs its address but refuses to initialize: it is
        // wedged in a state that re-init cannot clear. A full reboot is the
        // one recovery proven to work every time, so stop limping and reboot.
        Serial.println("Sensor unrecoverable in place - rebooting ESP32.");
        Serial.flush();
        delay(50);
        ESP.restart();
      }
    }
  }
}

void loop() {
  static uint32_t next = 0;
  uint32_t now = millis();

  updateLed();

  // Hold the button to record. Audio capture takes priority over the sensor
  // stream: a few dropped orientation frames cost nothing, a gap in the
  // recording is audible.
  bool held = micOK && buttonHeld();
  if (held && !recording) {
    recording = true;
    audioLen = 0;
    ledMode = 1;
    Serial.println(">>> RECORDING <<<");
  } else if (!held && recording) {
    recording = false;
    Serial.printf(">>> STOPPED: %.2f s\n", (float)audioLen / AUDIO_RATE);
    sendAudio();
  }

  if (recording) {
    static int32_t rawAudio[256];
    size_t got = i2s.readBytes((char *)rawAudio, sizeof(rawAudio));
    size_t n = got / sizeof(int32_t);
    for (size_t i = 0; i < n && audioLen < maxSamples; i++)
      audioBuf[audioLen++] = (int16_t)(rawAudio[i] >> 16);   // 24-bit -> 16-bit
    if (audioLen >= maxSamples) {
      Serial.println("(buffer full)");
      recording = false;
      sendAudio();
      while (buttonHeld()) { updateLed(); delay(5); }
    }
    return;   // skip sensor work while capturing
  }

  if (imuPresent) {
    readSensor();
  } else if (now > nextRetryMs) {
    // Never give up: a marginal connection often comes back, and a boot-time
    // failure must not doom the whole session. After a few in-place retries,
    // reboot — that path reliably brings the sensor back.
    nextRetryMs = now + 3000;
    static int missingRetries = 0;
    Serial.println("Retrying BNO08X...");
    imuPresent = initSensor();
    if (imuPresent) {
      missingRetries = 0;
    } else if (++missingRetries >= 2) {
      Serial.println("Still no sensor - rebooting ESP32.");
      Serial.flush();
      delay(50);
      ESP.restart();
    }
  }

  // Yield to the FreeRTOS scheduler. Without this the tight poll loop
  // starves the BLE stack, which is what wedged the sensor's I2C transport
  // as soon as a client connected.
  delay(2);

  if (now < next) return;
  next = now + 1000 / SAMPLE_HZ;

  // Flex sensor, lightly smoothed against ADC noise. Sent raw (0..4095) so
  // the laptop can calibrate it to the player's actual bend range. Seed the
  // filter from the first sample — ramping up from 0 produced a fake swing
  // that looked like a real bend to the laptop's auto-calibration.
  int flexRaw = analogRead(FLEX_PIN);
  int flex2Raw = analogRead(FLEX2_PIN);
  static bool flexInit = false;
  if (!flexInit) {
    flex = flexRaw;
    flex2 = flex2Raw;
    flexInit = true;
  } else {
    flex = 0.8f * flex + 0.2f * flexRaw;
    flex2 = 0.8f * flex2 + 0.2f * flex2Raw;
  }

  // Heartbeat first, so serial diagnostics work with no BLE client attached.
  static uint32_t nextLog = 0;
  if (now > nextLog) {
    nextLog = now + 1000;
    Serial.printf("%s roll %7.2f  pitch %7.2f  yaw %7.2f  |acc| %5.2f  flex %5.0f/%5.0f  ble=%d\n",
                  imuPresent ? "SENSOR " : "NO-IMU!", roll, pitch, yaw,
                  sqrtf(lax * lax + lay * lay + laz * laz), flex, flex2,
                  deviceConnected);
  }

  if (!deviceConnected) return;

  if (!imuPresent) roll = pitch = yaw = lax = lay = laz = 0;

  float pkt[9] = {roll,      pitch, yaw, lax, lay, laz,
                  imuPresent ? 1.0f : 0.0f, flex, flex2};
  sensorChar->setValue((uint8_t *)pkt, sizeof(pkt));
  sensorChar->notify();
}
