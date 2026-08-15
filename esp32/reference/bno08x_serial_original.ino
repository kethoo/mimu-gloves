// User's original BNO08x sketch (serial output only, no BLE).
// Kept for reference — the sensor init, quaternion->Euler math, and
// calibration idea were merged into ../glove_ble/glove_ble.ino.

#include <Wire.h>
#include <math.h>
#include <Adafruit_BNO08x.h>

#define BNO08X_I2C_ADDR 0x4B
#define BNO_RST 4


Adafruit_BNO08x bno08x;
sh2_SensorValue_t sensorValue;

// ---------- Calibration ----------
float yawOffset = 0;
float pitchOffset = 0;
float rollOffset = 0;

bool calibrated = false;

// ---------- Filter ----------
float yawFiltered = 0;
float pitchFiltered = 0;
float rollFiltered = 0;

const float alpha = 0.85;   // Filter strength

// ---------- Timing ----------
unsigned long startTime;

void setReports() {
  if (!bno08x.enableReport(SH2_ROTATION_VECTOR)) {
    Serial.println("Could not enable Rotation Vector");
  }
}

void setup() {

  Serial.begin(115200);
  delay(100);

  Serial.println();
  Serial.println("========== MIMU GLOVE ==========");
  Serial.println("Initializing...");
  Serial.println();


  // ---------- Reset BNO08x ----------
  pinMode(BNO_RST, OUTPUT);

  digitalWrite(BNO_RST, LOW);
  delay(10);

  digitalWrite(BNO_RST, HIGH);
  delay(500);


  // ---------- Start I2C ----------
  Wire.begin(21, 22);
  delay(100);


  // ---------- Initialize BNO08x ----------
  if (!bno08x.begin_I2C(BNO08X_I2C_ADDR, &Wire)) {
    Serial.println("BNO08X not found!");
    while (1);
  }

  Serial.println("BNO08X Found!");

  setReports();

  startTime = millis();

  Serial.println();
  Serial.println("Hold the glove still...");
  Serial.println("Calibration starts in 5 seconds.");
  Serial.println();
}

void loop() {

  if (bno08x.wasReset()) {
    Serial.println("Sensor reset.");
    delay(100);
    setReports();
  }

  if (!bno08x.getSensorEvent(&sensorValue))
    return;

  if (sensorValue.sensorId != SH2_ROTATION_VECTOR)
    return;

  // ---------- Read Quaternion ----------
  float qr = sensorValue.un.rotationVector.real;
  float qi = sensorValue.un.rotationVector.i;
  float qj = sensorValue.un.rotationVector.j;
  float qk = sensorValue.un.rotationVector.k;

  // ---------- Quaternion -> Euler ----------
  float yaw = atan2(
      2.0f * (qr * qk + qi * qj),
      1.0f - 2.0f * (qj * qj + qk * qk));

  float pitch = asin(
      2.0f * (qr * qj - qk * qi));

  float roll = atan2(
      2.0f * (qr * qi + qj * qk),
      1.0f - 2.0f * (qi * qi + qj * qj));

  yaw *= 180.0 / PI;
  pitch *= 180.0 / PI;
  roll *= 180.0 / PI;

  // ---------- Automatic Calibration ----------
  if (!calibrated && millis() - startTime >= 5000) {

    yawOffset = yaw;
    pitchOffset = pitch;
    rollOffset = roll;

    yawFiltered = 0;
    pitchFiltered = 0;
    rollFiltered = 0;

    calibrated = true;

    Serial.println();
    Serial.println("========== CALIBRATED ==========");
    Serial.print("Yaw Offset: ");
    Serial.println(yawOffset, 2);

    Serial.print("Pitch Offset: ");
    Serial.println(pitchOffset, 2);

    Serial.print("Roll Offset: ");
    Serial.println(rollOffset, 2);

    Serial.println("===============================");
    Serial.println();
  }

  if (!calibrated)
    return;

  // ---------- Relative Orientation ----------
  yaw -= yawOffset;
  pitch -= pitchOffset;
  roll -= rollOffset;

  // ---------- Low-pass Filter ----------
  yawFiltered =
      alpha * yawFiltered +
      (1.0 - alpha) * yaw;

  pitchFiltered =
      alpha * pitchFiltered +
      (1.0 - alpha) * pitch;

  rollFiltered =
      alpha * rollFiltered +
      (1.0 - alpha) * roll;

  // ---------- Print ----------
  Serial.print("Yaw: ");
  Serial.print(yawFiltered, 1);

  Serial.print("°   Pitch: ");
  Serial.print(pitchFiltered, 1);

  Serial.print("°   Roll: ");
  Serial.println(rollFiltered, 1);

   // ---------- Gesture Detection ----------
  if (pitchFiltered < -30) {
    Serial.println("Gesture: Hand Up");
  }
  else if (pitchFiltered > 30) {
    Serial.println("Gesture: Hand Down");
  }
  else if (rollFiltered > 25) {
    Serial.println("Gesture: Tilt Right");
  }
  else if (rollFiltered < -25) {
    Serial.println("Gesture: Tilt Left");
  }

  delay(20);
}
