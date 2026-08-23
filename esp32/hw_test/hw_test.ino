// Bench test for the NEW glove hardware: button, status LED and INMP441 mic.
// Flash this on its own — it does not touch the IMU or BLE — to prove the
// wiring before any of it goes into the real firmware.
//
// Wiring
//   Button : GPIO18 -> button -> GND            (internal pull-up, no resistor)
//   LED    : GPIO19 -> 220R -> LED anode, LED cathode -> GND
//
// GPIO16 is NOT usable on this board: measured, it stays LOW with the internal
// pull-up enabled and nothing connected, because this is a WROVER module where
// GPIO16 belongs to the PSRAM. GPIO18 idles HIGH correctly.
//   INMP441: VDD->3V3, GND->GND, L/R->GND, SCK->GPIO33, WS->GPIO25, SD->GPIO32
//
// With a single LED the state is carried by the blink pattern:
//   off        idle, ready
//   solid on   recording - hold the button, release to stop
//   slow blink busy (this is where the BLE transfer will go)
//   fast blink error - the microphone did not start

#include <ESP_I2S.h>

#define BTN_PIN  18
#define LED_PIN  19
#define I2S_SCK  33
#define I2S_WS   25
#define I2S_SD   32

#define SAMPLE_RATE 16000
#define CHUNK 256  // samples read per loop

enum LedMode { LED_IDLE, LED_REC, LED_BUSY, LED_ERROR };

I2SClass i2s;
LedMode ledMode = LED_IDLE;
bool micOK = false;
bool recording = false;
uint32_t recStartMs = 0, samplesRead = 0, lastMeterMs = 0;
int32_t peakAbs = 0;

// Non-blocking: the pattern is derived from the clock, so nothing stalls.
void updateLed() {
  uint32_t t = millis();
  bool on = false;
  switch (ledMode) {
    case LED_IDLE:  on = false;             break;
    case LED_REC:   on = true;              break;
    case LED_BUSY:  on = (t / 400) % 2;     break;
    case LED_ERROR: on = (t / 120) % 2;     break;
  }
  digitalWrite(LED_PIN, on);
}

// Debounced "is the button being held right now?" (active low).
// Hold-to-record: you cannot walk away leaving it recording, and the gesture
// matches what your hand is doing - raise, hold, speak, release.
bool buttonHeld() {
  static bool stable = false, lastRaw = false;
  static uint32_t lastChange = 0;
  bool raw = digitalRead(BTN_PIN) == LOW;
  if (raw != lastRaw) {
    lastRaw = raw;
    lastChange = millis();
  }
  if (millis() - lastChange > 30) stable = raw;
  return stable;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n=== GLOVE HARDWARE TEST: button + LED + mic ===");

  pinMode(LED_PIN, OUTPUT);
  pinMode(BTN_PIN, INPUT_PULLUP);

  // Three blinks so you can confirm the LED is wired the right way round.
  Serial.println("LED check: three blinks...");
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_PIN, HIGH); delay(180);
    digitalWrite(LED_PIN, LOW);  delay(180);
  }

  // INMP441 is 24-bit data in 32-bit slots, left channel (L/R tied low).
  i2s.setPins(I2S_SCK, I2S_WS, -1, I2S_SD);
  micOK = i2s.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_32BIT,
                    I2S_SLOT_MODE_MONO);
  if (!micOK) {
    Serial.println("I2S init FAILED - check SCK=33, WS=25, SD=32.");
    ledMode = LED_ERROR;
  } else {
    Serial.println("I2S ready. HOLD the button to record.");
    ledMode = LED_IDLE;
  }
}

void loop() {
  updateLed();

  bool held = micOK && buttonHeld();
  if (held != recording) {
    recording = held;
    if (recording) {
      Serial.println("\n>>> RECORDING (hold the button and speak) <<<");
      ledMode = LED_REC;      // solid
      recStartMs = millis();
      samplesRead = 0;
      peakAbs = 0;
    } else {
      ledMode = LED_BUSY;     // slow blink: stands in for the BLE transfer
      float secs = (millis() - recStartMs) / 1000.0f;
      Serial.printf("\n>>> STOPPED: %.1fs, %lu samples, peak %.3f full-scale\n",
                    secs, (unsigned long)samplesRead, peakAbs / 8388608.0f);
      Serial.println(peakAbs < 20000
                     ? "  Very quiet - is SD wired to GPIO32 and L/R to GND?"
                     : "  Mic is picking up sound correctly.");
      uint32_t until = millis() + 1500;
      while (millis() < until) updateLed();   // show the busy pattern
      ledMode = LED_IDLE;
    }
  }

  if (!recording || !micOK) {
    delay(5);
    return;
  }

  static int32_t buf[CHUNK];
  size_t got = i2s.readBytes((char *)buf, sizeof(buf));
  size_t n = got / sizeof(int32_t);
  if (n == 0) return;

  // INMP441 data is left-justified in the 32-bit slot: >>8 gives 24-bit.
  double sum = 0;
  for (size_t i = 0; i < n; i++) {
    int32_t s = buf[i] >> 8;
    int32_t a = s < 0 ? -s : s;
    if (a > peakAbs) peakAbs = a;
    sum += (double)s * s;
  }
  samplesRead += n;

  if (millis() - lastMeterMs > 200) {
    lastMeterMs = millis();
    double rms = sqrt(sum / n) / 8388608.0;      // 0..1 of full scale
    int bars = (int)(rms * 300);
    if (bars > 40) bars = 40;
    Serial.printf("level %5.3f |", rms);
    for (int i = 0; i < bars; i++) Serial.print('#');
    Serial.println();
  }
}
