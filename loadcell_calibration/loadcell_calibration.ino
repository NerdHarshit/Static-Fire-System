// =============================================================================
//  LOAD CELL CALIBRATION SKETCH
//  DJS Impulse — for use with static_fire_firmware_v3_1
//
//  WIRING: same as v3.1 firmware
//    HX711 DOUT → GP2
//    HX711 SCK  → GP3
//
//  HOW TO USE:
//    1. Flash this sketch
//    2. Open Serial Monitor at 115200 baud
//    3. Follow the on-screen instructions
//    4. Copy the printed HX_CALIBRATION value into v3.1 firmware
// =============================================================================

#include "HX711.h"

#define HX_DOUT  2
#define HX_SCK   3

HX711 scale;

// ---- helper: block until user sends anything over Serial ----
void waitForEnter(const char* prompt) {
    Serial.println(prompt);
    Serial.println(F("  >>> Press Enter (send any character) to continue..."));
    while (!Serial.available()) {}
    while (Serial.available()) Serial.read();  // flush
}

// ---- helper: read a float from Serial ----
float readFloat() {
    while (!Serial.available()) {}
    String s = Serial.readStringUntil('\n');
    s.trim();
    return s.toFloat();
}

void setup() {
    Serial.begin(115200);
    while (!Serial && millis() < 3000) {}

    Serial.println(F("\n================================================"));
    Serial.println(F("  LOAD CELL CALIBRATION — DJS Impulse"));
    Serial.println(F("================================================\n"));

    scale.begin(HX_DOUT, HX_SCK);

    // Wait for HX711 to be ready
    Serial.print(F("Waiting for HX711..."));
    while (!scale.is_ready()) {
        Serial.print('.');
        delay(200);
    }
    Serial.println(F(" OK\n"));

    // ------------------------------------------------------------------
    //  STEP 1 — Tare
    // ------------------------------------------------------------------
    waitForEnter("STEP 1: Make sure NOTHING is on the load cell.");

    Serial.print(F("Taring (20 samples)... "));
    scale.set_scale();   // no factor yet — raw counts
    scale.tare(20);
    Serial.println(F("Done.\n"));

    // Show zero reading to confirm tare worked
    float zeroCheck = scale.get_units(10);
    Serial.print(F("Zero check (should be ~0): "));
    Serial.println(zeroCheck, 2);
    Serial.println();

    // ------------------------------------------------------------------
    //  STEP 2 — Place known mass
    // ------------------------------------------------------------------
    waitForEnter("STEP 2: Place your known calibration mass on the load cell.");

    Serial.print(F("Reading raw value (20 samples)... "));
    float rawReading = scale.get_units(20);
    Serial.println(F("Done.\n"));

    Serial.print(F("Raw ADC count: "));
    Serial.println(rawReading, 4);
    Serial.println();

    // ------------------------------------------------------------------
    //  STEP 3 — Enter known mass in GRAMS
    // ------------------------------------------------------------------
    Serial.println(F("STEP 3: Enter the mass of your calibration weight in GRAMS."));
    Serial.println(F("  Examples: 1 kg = 1000 | 2 kg = 2000 | 500 g = 500"));
    Serial.print(F("  Mass in grams: "));
    float knownMassGrams = readFloat();
    Serial.println(knownMassGrams);
    Serial.println();

    // ------------------------------------------------------------------
    //  COMPUTE FACTOR
    // ------------------------------------------------------------------
    if (knownMassGrams == 0.0f) {
        Serial.println(F("ERROR: Mass cannot be zero. Reset and try again."));
        while (true) {}
    }

    float factor = rawReading / knownMassGrams;

    // ------------------------------------------------------------------
    //  VERIFY
    // ------------------------------------------------------------------
    scale.set_scale(factor);

    Serial.println(F("Verifying — keep the mass on the cell..."));
    float verifyGrams   = scale.get_units(20);
    float verifyNewtons = verifyGrams * 0.00980665f;
    float expectedN     = knownMassGrams * 0.00980665f;
    float errorPct      = ((verifyNewtons - expectedN) / expectedN) * 100.0f;

    Serial.println(F("\n================================================"));
    Serial.println(F("  CALIBRATION RESULT"));
    Serial.println(F("================================================"));
    Serial.print(F("  Raw ADC count      : ")); Serial.println(rawReading, 4);
    Serial.print(F("  Known mass (g)     : ")); Serial.println(knownMassGrams, 2);
    Serial.print(F("  Calibration factor : ")); Serial.println(factor, 4);
    Serial.print(F("  Verification (g)   : ")); Serial.println(verifyGrams, 2);
    Serial.print(F("  Verification (N)   : ")); Serial.println(verifyNewtons, 4);
    Serial.print(F("  Expected    (N)    : ")); Serial.println(expectedN, 4);
    Serial.print(F("  Error              : ")); Serial.print(errorPct, 2); Serial.println(F(" %"));

    Serial.println(F("\n================================================"));
    Serial.println(F("  PASTE THIS INTO static_fire_firmware_v3_1.ino"));
    Serial.println(F("================================================"));
    Serial.print(F("  static const float HX_CALIBRATION = "));
    Serial.print(factor, 4);
    Serial.println(F("f;"));
    Serial.println(F("================================================\n"));

    if (abs(errorPct) < 2.0f) {
        Serial.println(F("STATUS: GOOD — error < 2%, calibration is solid."));
    } else if (abs(errorPct) < 5.0f) {
        Serial.println(F("STATUS: ACCEPTABLE — error < 5%. Consider re-calibrating with a more precise mass."));
    } else {
        Serial.println(F("STATUS: WARNING — error > 5%. Check your known mass, retry tare, or check wiring."));
    }

    Serial.println(F("\nDone. Flash v3.1 firmware with the value above."));
}

void loop() {
    // Optional live monitoring after calibration
    // Remove comment block below if you want to watch live readings
    if (scale.is_ready()) {
    float g = scale.get_units(1);
    float n = g * 0.00980665f;
    Serial.print(g, 2); Serial.print(F(" g  |  "));
    Serial.print(n, 4); Serial.println(F(" N"));
    }
 delay(100);

}
