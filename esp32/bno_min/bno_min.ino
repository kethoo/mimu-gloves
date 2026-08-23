// MINIMAL diagnostic: closest thing to the user's original working sketch.
// No BLE, no watchdog, no I2C bus-recovery. Just: reset, init, stream.
#include <Wire.h>
#include <Adafruit_BNO08x.h>

Adafruit_BNO08x bno08x;
sh2_SensorValue_t v;

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("=== MINIMAL BNO08X TEST ===");

  pinMode(4, OUTPUT);
  digitalWrite(4, LOW);  delay(10);
  digitalWrite(4, HIGH); delay(500);

  Wire.begin(21, 22);
  delay(100);

  if (!bno08x.begin_I2C(0x4B, &Wire)) {
    Serial.println("begin_I2C FAILED");
    while (1) delay(1000);
  }
  Serial.println("begin_I2C OK");

  if (!bno08x.enableReport(SH2_ROTATION_VECTOR))
    Serial.println("enableReport FAILED");
  else
    Serial.println("enableReport OK");
}

uint32_t reports = 0, lastPrint = 0;

void loop() {
  if (bno08x.wasReset()) {
    Serial.println("(sensor reset -> re-enabling)");
    bno08x.enableReport(SH2_ROTATION_VECTOR);
  }
  if (bno08x.getSensorEvent(&v)) {
    if (v.sensorId == SH2_ROTATION_VECTOR) reports++;
  }
  if (millis() - lastPrint > 1000) {
    lastPrint = millis();
    Serial.printf("reports/s=%lu  quat r=%.3f i=%.3f j=%.3f k=%.3f\n",
                  (unsigned long)reports, v.un.rotationVector.real,
                  v.un.rotationVector.i, v.un.rotationVector.j,
                  v.un.rotationVector.k);
    reports = 0;
  }
  delay(2);
}
