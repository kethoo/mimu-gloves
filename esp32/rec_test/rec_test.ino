// Proof that the glove can actually RECORD, not just measure loudness.
//
// Hold the button -> records to RAM (LED solid).
// Release        -> sends the take to the laptop over serial (LED slow blink).
// record_test.py on the laptop turns it back into sound you can hear.
//
// This is a dry run of the real pipeline with serial standing in for BLE:
// buffer on the ESP32, transfer after the fact, play on the laptop. Latency
// does not matter for a recording, which is why this works where live
// streaming would not.
//
// Wiring: button GPIO18->GND, LED GPIO19 via 220R, INMP441 SCK=33 WS=25 SD=32

#include <ESP_I2S.h>

#define BTN_PIN  18
#define LED_PIN  19
#define I2S_SCK  33
#define I2S_WS   25
#define I2S_SD   32

#define SAMPLE_RATE  16000
#define MAX_SECONDS  3
#define MAX_SAMPLES  (SAMPLE_RATE * MAX_SECONDS)
#define CHUNK        256

I2SClass i2s;
int16_t audio[MAX_SAMPLES];     // ~96 KB
size_t  nSamples = 0;
bool    micOK = false, recording = false;
uint32_t ledUntil = 0;
int      ledMode = 0;           // 0 idle, 1 recording, 2 busy, 3 error

void updateLed() {
  uint32_t t = millis();
  bool on = false;
  if (ledMode == 1) on = true;
  else if (ledMode == 2) on = (t / 400) % 2;
  else if (ledMode == 3) on = (t / 120) % 2;
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

static const char B64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

// Streamed so we never allocate a second copy of the audio.
void dumpBase64(const uint8_t *data, size_t len) {
  char line[81];
  int li = 0;
  for (size_t i = 0; i < len; i += 3) {
    uint32_t v = (uint32_t)data[i] << 16;
    if (i + 1 < len) v |= (uint32_t)data[i + 1] << 8;
    if (i + 2 < len) v |= data[i + 2];
    line[li++] = B64[(v >> 18) & 63];
    line[li++] = B64[(v >> 12) & 63];
    line[li++] = (i + 1 < len) ? B64[(v >> 6) & 63] : '=';
    line[li++] = (i + 2 < len) ? B64[v & 63] : '=';
    if (li >= 76) { line[li] = 0; Serial.println(line); li = 0; }
  }
  if (li) { line[li] = 0; Serial.println(line); }
}

void setup() {
  Serial.begin(115200);
  delay(200);
  pinMode(BTN_PIN, INPUT_PULLUP);
  pinMode(LED_PIN, OUTPUT);
  Serial.println("\n=== GLOVE RECORD TEST ===");

  i2s.setPins(I2S_SCK, I2S_WS, -1, I2S_SD);
  micOK = i2s.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_32BIT,
                    I2S_SLOT_MODE_MONO);
  if (!micOK) {
    Serial.println("I2S init FAILED");
    ledMode = 3;
  } else {
    Serial.printf("Ready. HOLD the button and speak (max %d s).\n", MAX_SECONDS);
    ledMode = 0;
  }
}

void loop() {
  updateLed();
  bool held = micOK && buttonHeld();

  if (held && !recording) {
    recording = true;
    nSamples = 0;
    ledMode = 1;
    Serial.println("\n>>> RECORDING <<<");
  } else if (!held && recording) {
    recording = false;
    ledMode = 2;
    float secs = (float)nSamples / SAMPLE_RATE;
    Serial.printf(">>> STOPPED: %.2f s, %u samples\n", secs, (unsigned)nSamples);
    Serial.printf("AUDIO_BEGIN %u %u\n", (unsigned)nSamples, SAMPLE_RATE);
    dumpBase64((const uint8_t *)audio, nSamples * sizeof(int16_t));
    Serial.println("AUDIO_END");
    ledMode = 0;
  }

  if (!recording) { delay(2); return; }

  static int32_t raw[CHUNK];
  size_t got = i2s.readBytes((char *)raw, sizeof(raw));
  size_t n = got / sizeof(int32_t);
  for (size_t i = 0; i < n && nSamples < MAX_SAMPLES; i++) {
    // 24-bit left-justified in a 32-bit slot -> take the top 16 bits.
    audio[nSamples++] = (int16_t)(raw[i] >> 16);
  }
  if (nSamples >= MAX_SAMPLES) {
    Serial.println("(buffer full)");
    // Force the stop path on the next loop by pretending the button went up.
    recording = false;
    ledMode = 2;
    Serial.printf("AUDIO_BEGIN %u %u\n", (unsigned)nSamples, SAMPLE_RATE);
    dumpBase64((const uint8_t *)audio, nSamples * sizeof(int16_t));
    Serial.println("AUDIO_END");
    ledMode = 0;
    while (buttonHeld()) { updateLed(); delay(5); }  // wait for release
  }
}
