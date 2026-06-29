#include <SPI.h>
#include <Ethernet.h>
#include "HX711.h"

// ================= NETWORK =================
byte mac[] = { 0xDE, 0xAD, 0xBE, 0xEF, 0xFE, 0xED };
IPAddress ip(192, 168, 1, 60);
EthernetServer server(8080);
HX711 myscale;

// ================= HX711 =================
#define HX_DT 2
#define HX_SCK 3


// ================= PINS =================
#define PYRO1 4
#define PYRO2 5

// ================= STATES =================
enum State {
  IDLE,
  INITIALISATION,
  ARMED,
  IGNITE
};

State currentState = IDLE;

// ================= HELPERS =================
void sendLine(EthernetClient &client, const String &msg) {
  client.print(msg);
  client.print('\n');
}

// ================= HX711 STREAM =================
void streamHX711(EthernetClient &client) {
  const int samplingRateHz = 80;
  const int durationSeconds = 10;
  const int totalSamples = samplingRateHz * durationSeconds;

  unsigned long startTime = millis();

  sendLine(client, "SAMPLE,Time_ms,Weight_g");

  for (int i = 0; i < totalSamples; i++) {
    float weight = myscale.get_units(2);;
    unsigned long t = millis() - startTime;

    client.print(i + 1);
    client.print(",");
    client.print(t);
    client.print(",");
    client.println(weight, 3);

    // maintain 80 Hz
    unsigned long target = startTime + (i + 1) * (1000 / samplingRateHz);
    while (millis() < target) {
      // busy wait to keep timing exact
    }
  }

  sendLine(client, "THRUST_LOG_COMPLETE");
}

// ================= COMMAND HANDLER =================
void handleCommand(String cmd, EthernetClient &client) {
  cmd.trim();
  cmd.toUpperCase();

  if (cmd.length() == 0) return;

  Serial.println("CMD: " + cmd);

  // ---------- IDLE ----------
  if (currentState == IDLE) {
    if (cmd == "INITIALISATION") {
      sendLine(client, "INITIALISING...");
      if(!myscale.get_units()){
                sendLine(client, "Loadcell not connected well");
                while(1);
              }else{
                sendLine(client, "HX711 initialised successfully");
              }
      return;
    }

    if (cmd == "ARM") {
      sendLine(client, "ARMING SYSTEM...");
      delay(1000);
      sendLine(client, "SYSTEM ARMED");
      currentState = ARMED;
      return;
    }
  }

  // ---------- ARMED → IGNITE ----------
  if (currentState == ARMED && cmd == "IGNITE") {
    currentState = IGNITE;
    sendLine(client, "COUNTDOWN STARTED");

    for (int i = 10; i > 0; i--) {
      sendLine(client, "T-" + String(i));
      delay(1000);
    }

    // pyros firing
    digitalWrite(PYRO1, HIGH);
    digitalWrite(PYRO2, HIGH);
    delay(100);
    digitalWrite(PYRO1, LOW);
    digitalWrite(PYRO2, LOW);

    sendLine(client, "PYROS FIRED");
    sendLine(client, "STARTING THRUST LOG");

    // 📡 STREAM HX711 DATA
    streamHX711(client);

    sendLine(client, "IGNITION COMPLETE");
    currentState = IDLE;
    return;
  }
}

// ================= SETUP =================
void setup() {
  Serial.begin(115200);
  delay(2000);

  pinMode(PYRO1, OUTPUT);
  pinMode(PYRO2, OUTPUT);
  digitalWrite(PYRO1, LOW);
  digitalWrite(PYRO2, LOW);

  myscale.begin(HX_DT, HX_SCK);
  myscale.set_scale(-17.492397);
  myscale.tare();
  Serial.println("HX711 Ready");

  Ethernet.init(17);
  Ethernet.begin(mac, ip);
  server.begin();

  Serial.print("TCP Server @ ");
  Serial.println(Ethernet.localIP());
}

// ================= LOOP =================
void loop() {
  EthernetClient client = server.available();
  if (!client) return;

  sendLine(client, "CONNECTED TO ROCKET CONTROLLER");
  Serial.println("Client connected");

  String buffer = "";

  while (client.connected()) {
    while (client.available()) {
      char c = client.read();
      if (c == '\n') {
        handleCommand(buffer, client);
        buffer = "";
      } else if (c != '\r') {
        buffer += c;
      }
    }
  }

  client.stop();
  Serial.println("Client disconnected");
}

