// Flex-sensor isolation test. Flash this ON ITS OWN when both flex channels
// read zero and you need to know whether the ADC works at all.
//
// Deliberately minimal: no BLE, no I2S, no IMU. The full firmware runs the
// INMP441 on GPIO32/33, which are ADC1 channels 4 and 5 — the same ADC1 block
// as the flex pins on 34/35. That should not disturb analogRead, but this
// sketch removes the question entirely.
//
// Wiring under test: 3V3 -> flex -> GPIO34 -> 15k -> GND   (same for 35)
//
// Reading it:
//   both ~0, steady         no voltage arriving. The 3V3 leg or the sensor
//                           is open — GPIO34-39 have no internal pull-up, so
//                           a steady 0 means the 15k pull-down is winning
//                           with nothing feeding the top of the divider.
//   both ~4095              the pin is tied to 3V3 — pull-down missing.
//   drifting noise, no 15k  pin genuinely floating.
//   moves when you bend     everything works; the fault was elsewhere.
//
// Jumper 3V3 straight to GPIO34 to prove the pin and ADC in isolation:
// it should read close to 4095.

#define FLEX_PIN  34
#define FLEX2_PIN 35

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("flex_test: analogRead on GPIO34 / GPIO35, nothing else running.");
  Serial.println("Bend each finger. Then jumper 3V3 -> GPIO34; expect ~4095.");
}

void loop() {
  // Median of 5 rejects the ESP32 ADC's occasional wild sample without
  // hiding a real signal the way heavy averaging would.
  int a[5], b[5];
  for (int i = 0; i < 5; i++) {
    a[i] = analogRead(FLEX_PIN);
    b[i] = analogRead(FLEX2_PIN);
    delay(2);
  }
  for (int i = 0; i < 5; i++)
    for (int j = i + 1; j < 5; j++) {
      if (a[j] < a[i]) { int t = a[i]; a[i] = a[j]; a[j] = t; }
      if (b[j] < b[i]) { int t = b[i]; b[i] = b[j]; b[j] = t; }
    }

  static int lo1 = 4095, hi1 = 0, lo2 = 4095, hi2 = 0;
  int v1 = a[2], v2 = b[2];
  if (v1 < lo1) lo1 = v1;
  if (v1 > hi1) hi1 = v1;
  if (v2 < lo2) lo2 = v2;
  if (v2 > hi2) hi2 = v2;

  // Volts assume the default 11 dB attenuation, i.e. full scale ~3.3 V.
  Serial.printf("flex1 %4d (seen %4d..%4d, swing %4d, %.2fV)   "
                "flex2 %4d (seen %4d..%4d, swing %4d, %.2fV)\n",
                v1, lo1, hi1, hi1 - lo1, v1 * 3.3 / 4095.0,
                v2, lo2, hi2, hi2 - lo2, v2 * 3.3 / 4095.0);
  delay(200);
}
