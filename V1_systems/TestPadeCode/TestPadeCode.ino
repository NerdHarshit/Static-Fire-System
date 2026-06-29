#include <SPI.h>
#include <LoRa.h>
#include <HX711.h>
#include <SdFat.h>

// Pin definitions
#define LORA_CS   17
#define LORA_RST  20
#define LORA_DIO0 21

#define SD_CS     13   // CS for SD
#define SD_SCK    10
#define SD_MISO   12
#define SD_MOSI   11

#define DT        2
#define CLK       3
#define BUZZER    9
#define PYRO1     4
#define PYRO2     5
#define LED_R     8
#define LED_G     7
#define LED_B     6

enum TestPadState { IDLE, INITIALISATION, ARM, IGNITE, ABORT };
TestPadState currentState = IDLE;

HX711 myScale;
SdFat SD;
SdSpiConfig sdConfig(SD_CS, SPI_FULL_SPEED, 10000000, &SPI1);
FsFile myfile;

void setup() {
  Serial.begin(115200);

  // LEDs & Buzzer
  pinMode(LED_R, OUTPUT);
  pinMode(LED_G, OUTPUT);
  pinMode(LED_B, OUTPUT);
  pinMode(BUZZER, OUTPUT);
  pinMode(PYRO1, OUTPUT);
  pinMode(PYRO2, OUTPUT);
  digitalWrite(PYRO1, LOW);
  digitalWrite(PYRO2, LOW);
  digitalWrite(BUZZER, LOW);

  Serial.println("=== TestPad Start ===");

  // --- LoRa ---
  LoRa.setPins(LORA_CS, LORA_RST, LORA_DIO0);
  if (!LoRa.begin(868E6)) Serial.println("LoRa init failed!");
  LoRa.setTxPower(20, PA_OUTPUT_PA_BOOST_PIN);
  LoRa.setSpreadingFactor(7);
  LoRa.enableCrc();
  LoRa.setSignalBandwidth(125E3);
  LoRa.setCodingRate4(8);
  Serial.println("LoRa Initialized OK!");

  // --- SPI1 for SD ---
  SPI1.setRX(SD_MISO);
  SPI1.setTX(SD_MOSI);
  SPI1.setSCK(SD_SCK);
  SPI1.begin();
  if (!SD.begin(sdConfig)) Serial.println("SD init failed on SPI1!");
  else Serial.println("SD init OK (SPI1)");

  // --- HX711 ---
  myScale.begin(DT, CLK);
  myScale.set_scale(-22.166132); // your calibration
  myScale.tare();
  int retries = 5;
  while (retries-- > 0 && !myScale.is_ready()) {
    Serial.println("HX711 not ready, retrying...");
    delay(200);
  }
  if (myScale.is_ready()) Serial.println("HX711 ready!");
  else Serial.println("HX711 failed to initialize!");

  currentState = IDLE;
  digitalWrite(LED_R,LOW);
  digitalWrite(LED_G,LOW);
  digitalWrite(LED_B,LOW);
}

void loop() {
  int packetSize = LoRa.parsePacket();
  if (packetSize) {
    String LoRaData = "";
    while (LoRa.available()) LoRaData += (char)LoRa.read();
    LoRaData.trim();
    Serial.print("Received: "); Serial.println(LoRaData);

    if (LoRaData == "ABORT") {
      digitalWrite(BUZZER, HIGH); delay(200); digitalWrite(BUZZER, LOW);
      LoRa.beginPacket(); LoRa.print("Abort initiated"); LoRa.endPacket();
      currentState = ABORT;
      delay(3000);
      LoRa.beginPacket(); LoRa.print("Abort complete. Back to IDLE"); LoRa.endPacket();
      currentState = IDLE;
      return;
    }

    switch (currentState) {
      case IDLE:
        if (LoRaData == "INITIALISATION") {currentState = INITIALISATION;}
        else if (LoRaData == "ARM") currentState = ARM;
        else if (LoRaData == "IGNITE") currentState = IGNITE;
        break;

      case INITIALISATION:
        Serial.println("INIT");
        digitalWrite(LED_R,LOW);
        digitalWrite(LED_G,LOW);
        digitalWrite(LED_B,HIGH);
        myfile = SD.open("log2.txt", FILE_WRITE);
        if (myfile) { myfile.println("Initialization check OK"); myfile.close(); }
        LoRa.beginPacket(); LoRa.print("Initialization done"); LoRa.endPacket();
        currentState = IDLE;
        break;

      case ARM:
        Serial.println("ARMED");
        digitalWrite(LED_G,HIGH);
        digitalWrite(LED_R,LOW);
        digitalWrite(LED_B,LOW);
        LoRa.beginPacket(); LoRa.print("ARMED"); LoRa.endPacket();
        currentState = IDLE;
        break;

      case IGNITE: {
        Serial.println("IGNITE");
        digitalWrite(LED_R,HIGH);
        digitalWrite(LED_G,LOW);
        digitalWrite(LED_B,LOW);
         //Open SD file for logging
        myfile = SD.open("log2.txt", FILE_WRITE);
        if (myfile) { myfile.println("Starting data logging!"); myfile.close();}
        if (!myfile) {Serial.println("Failed to open log2.txt for writing!");}
        Serial.println("1");
        LoRa.beginPacket(); LoRa.print("===Ignition Sequence==="); LoRa.endPacket();
        Serial.println("=== Ignition Sequence ===");

        for (int t = 10; t >= 0; t--) {
          LoRa.beginPacket(); LoRa.print("T minus "); LoRa.print(t); LoRa.print(" seconds"); LoRa.endPacket();
          Serial.print("T-"); Serial.print(t); Serial.println("s");

          // Sample HX711 and log every second (or faster)
          myfile = SD.open("log2.txt", FILE_WRITE);
          if (myfile) {
            float reading;
            if(myScale.is_ready()) {
              reading = myScale.get_units(5); // average 5 readings
            }
            else {
              reading = 1111;
            }
            myfile.print(millis() / 1000.0, 3);
            myfile.print(", ");
            myfile.println(reading);
          }
          myfile.close();

          delay(1000);
        }

        // Fire pyro
        digitalWrite(PYRO1, HIGH);
        digitalWrite(PYRO2, HIGH);
        digitalWrite(BUZZER, HIGH); delay(100); digitalWrite(BUZZER, LOW);
        digitalWrite(PYRO1, LOW);
        digitalWrite(PYRO2, LOW);
        LoRa.beginPacket(); LoRa.print("Ignition complete"); LoRa.endPacket();

        // Close SD file
        if (myfile) myfile.close();

        currentState = IDLE;
        break;
      }

      case ABORT:
        LoRa.beginPacket(); LoRa.print("Abort Initiated"); LoRa.endPacket();
        break;
    }
  }
}
