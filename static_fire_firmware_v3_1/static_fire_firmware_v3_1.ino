// =============================================================================
//  STATIC FIRE THRUST LOGGER — v3.1
//  DJS Impulse | Raspberry Pi Pico W + W5500 Ethernet
//  Hardware: HX711 Load Cell ADC, SD Card (SPI1), SRAM ring buffer,
//            EEPROM flash backup, W5500 Ethernet, 2x Pyro channels
//
//  ARCHITECTURE OVERVIEW:
//  ┌──────────────────────────────────────────────────────────────────────┐
//  │  STATE MACHINE:                                                      │
//  │  IDLE → INIT → TARE → ARMED → LOG_READY → IGNITE → LOGGING → COMPLETE│
//  │                                                                      │
//  │  DATA PIPELINE:                                                      │
//  │  HX711 ──► EMA Filter ──► Ring Buffer (SRAM)                         │
//  │               │                  ├──► SD Card (t_ms,raw_N,filt_N)    │
//  │               │                  ├──► Ethernet TX (real-time)        │
//  │               └─(raw also kept)  └──► EEPROM (on LOG_STOP)           │
//  └──────────────────────────────────────────────────────────────────────┘
//
//  COMMANDS (over TCP, newline-terminated):
//    INIT       — Verify load cell, enter INIT state
//    TARE       — Zero the load cell
//    ARM        — Enter ARMED state (pyros live)
//    LOG_START  — Verify logging works, enter LOG_READY (REQUIRED before IGNITE)
//    IGNITE     — 10s countdown, fire pyros (only valid in LOG_READY state)
//    LOG_STOP   — End logging window, flush SD, commit to EEPROM
//    STATUS     — Report current state, SD/EEPROM/ring buf status
//    DISARM     — Return to IDLE, safe pyros, stop logging
//
//  RESPONSES (from device, newline-terminated):
//    OK:<msg>                   — Acknowledgement
//    ERR:<msg>                  — Error
//    DATA:<t_ms>,<raw_N>,<filt_N> — Thrust sample (both raw and filtered)
//    STATE:<state>              — State change notification
//    T-<n>                      — Countdown tick
//    COMPLETE                   — Logging session ended
// =============================================================================

#include <Arduino.h>
#include <SPI.h>
#include <SD.h>
#include <EEPROM.h>
#include <Ethernet.h>
#include "HX711.h"

// =============================================================================
//  COMPILE-TIME CONFIGURATION — edit these to match your hardware
// =============================================================================

// ---- Network ----
static byte    MAC_ADDR[]  = { 0xDE, 0xAD, 0xBE, 0xEF, 0xFE, 0xED };
static IPAddress DEVICE_IP(192, 168, 1, 60);
static const uint16_t TCP_PORT = 8080;

// ---- HX711 ----
#define HX_DOUT      2
#define HX_SCK       3
// calibration factor: negative = load cell wired in pulling direction
// Re-calibrate: weigh a known mass, adjust until get_units() returns grams
static const float HX_CALIBRATION =  -38.2349f;  // raw/gram  (tune per cell)
static const float GRAMS_TO_NEWTONS = 0.00980665f;

// ---- Pyro ----
#define PYRO_A       4
#define PYRO_B       5
#define PYRO_FIRE_MS 150   // pulse width — long enough to heat bridge wire

// ---- SD (SPI1) ----
#define SD_MISO      12
#define SD_MOSI      11
#define SD_SCK       10
#define SD_CS        13
static const char SD_FILENAME[] = "thrust.csv";
static const uint32_t SD_PREALLOCATE_KB = 512;

// ---- Ring buffer (SRAM) ----
// 512 entries × 8 bytes = 4 KB SRAM; at 100 Hz = 5.12 s of history
// Increase for longer windows if RAM permits
#define RING_SIZE    1024   // bumped for ~10s at 100 Hz

// ---- EEPROM ----
#define EEPROM_TOTAL  4096
#define EEPROM_REC    12    // uint32 time (4) + float raw (4) + float filt (4)
#define EEPROM_MAX    (EEPROM_TOTAL / EEPROM_REC)   // 341 records

// ---- Sampling ----
// HX711 at RATE=1 pin high → 80 SPS; at 0 pin low → 10 SPS
// We read at up to 100 Hz but the HX711 ready flag gates actual reads
#define SAMPLE_INTERVAL_US  10000UL   // 100 Hz target (10 000 µs)

// ---- EMA filter ----
// alpha: 0.0 = maximum smoothing (slow response), 1.0 = no filtering (raw)
// Good starting point: 0.2 for noisy bench, 0.4 for cleaner setups.
// Rule of thumb: if the filtered curve looks too laggy on ignition, increase alpha.
//                if output still looks noisy, decrease alpha.
static const float EMA_ALPHA = 0.4f;

// ---- SD flush ----
#define SD_FLUSH_INTERVAL_MS  200   // how often ring → SD, keep <250ms for safety

// =============================================================================
//  DATA TYPES
// =============================================================================

struct LogEntry {
    uint32_t t_ms;        // ms since LOG_START command (not since boot)
    float    thrust_raw;  // Newtons, unfiltered
    float    thrust_filt; // Newtons, EMA filtered
};

struct RingBuffer {
    LogEntry buf[RING_SIZE];
    volatile uint16_t head;   // next write index
    uint16_t sdTail;          // next unwritten-to-SD index
    uint16_t count;           // total entries pushed (saturates at RING_SIZE)
};

enum FwState {
    FW_IDLE,
    FW_INIT,
    FW_TARE,
    FW_ARMED,
    FW_LOG_READY,   // LOG_START confirmed OK — safe to IGNITE
    FW_IGNITE,
    FW_LOGGING,
    FW_COMPLETE
};

// =============================================================================
//  GLOBALS
// =============================================================================

HX711       scale;
EthernetServer tcpServer(TCP_PORT);
EthernetClient activeClient;

RingBuffer  ring = {{}, 0, 0, 0};
float       emaValue = 0.0f;   // EMA filter state (Newtons)
bool        emaInit  = false;  // first sample seeds EMA directly

FwState  fwState      = FW_IDLE;
bool     sdReady      = false;
uint8_t  sdFailCount  = 0;
File     logFile;

bool     logging      = false;   // true while data window is open
uint32_t logStartMs   = 0;       // millis() at LOG_START
uint32_t lastSampleUs = 0;       // micros() of last HX711 read
uint32_t lastSDFlushMs = 0;

bool     eepromDone   = false;

// =============================================================================
//  EMA FILTER  (Exponential Moving Average)
//  filtered = alpha * raw + (1 - alpha) * prev_filtered
//  First sample seeds the filter directly to avoid startup ramp-up artifact.
// =============================================================================

float emaUpdate(float raw) {
    if (!emaInit) {
        emaValue = raw;
        emaInit  = true;
        return raw;
    }
    emaValue = EMA_ALPHA * raw + (1.0f - EMA_ALPHA) * emaValue;
    return emaValue;
}

// =============================================================================
//  RING BUFFER
// =============================================================================

void ringPush(uint32_t t_ms, float raw_n, float filt_n) {
    ring.buf[ring.head].t_ms        = t_ms;
    ring.buf[ring.head].thrust_raw  = raw_n;
    ring.buf[ring.head].thrust_filt = filt_n;
    ring.head = (ring.head + 1) % RING_SIZE;

    if (ring.count < RING_SIZE) {
        ring.count++;
    } else {
        // Buffer wrapped — advance SD tail too so it never reads stale data
        ring.sdTail = (ring.sdTail + 1) % RING_SIZE;
    }
}

// How many entries are waiting to be written to SD
uint16_t ringPendingSD() {
    return (ring.head - ring.sdTail + RING_SIZE) % RING_SIZE;
}

// =============================================================================
//  SD CARD
// =============================================================================

void initSD() {
    SPI1.setRX(SD_MISO);
    SPI1.setTX(SD_MOSI);
    SPI1.setSCK(SD_SCK);
    SPI1.begin();

    if (!SD.begin(SD_CS, SPI1)) {
        Serial.println(F("SD: init failed"));
        sdReady = false;
        return;
    }

    // Always append — supports power-cycle survival
    logFile = SD.open(SD_FILENAME, FILE_WRITE);
    if (!logFile) {
        Serial.println(F("SD: open failed"));
        sdReady = false;
        return;
    }

    // Write header only if file is new
    if (logFile.size() == 0) {
        logFile.println(F("t_ms,thrust_raw_N,thrust_filt_N"));
        // Pre-allocate to avoid FAT fragmentation mid-test
        uint32_t preBytes = (uint32_t)SD_PREALLOCATE_KB * 1024UL;
        uint32_t headerPos = logFile.position();
        logFile.seek(preBytes - 1);
        logFile.write((uint8_t)0);
        logFile.seek(headerPos);
        logFile.flush();
        Serial.println(F("SD: pre-allocated OK"));
    }

    sdReady    = true;
    sdFailCount = 0;
    Serial.println(F("SD: ready"));
}

void flushRingToSD() {
    if (!sdReady) return;
    uint16_t pending = ringPendingSD();
    if (pending == 0) return;

    uint16_t idx = ring.sdTail;
    bool ok = true;

    for (uint16_t i = 0; i < pending; i++) {
        LogEntry &e = ring.buf[idx];
        idx = (idx + 1) % RING_SIZE;

        // Use print() return to detect write failures
        if (!logFile.print(e.t_ms))             { ok = false; break; }
        logFile.print(',');
        if (!logFile.print(e.thrust_raw, 4))    { ok = false; break; }
        logFile.print(',');
        if (!logFile.println(e.thrust_filt, 4)) { ok = false; break; }
    }

    if (ok) {
        logFile.flush();
        ring.sdTail = idx;
        sdFailCount = 0;
    } else {
        sdFailCount++;
        Serial.print(F("SD: write fail #"));
        Serial.println(sdFailCount);
        if (sdFailCount >= 10) {
            sdReady = false;
            logFile.close();
            Serial.println(F("SD: logging halted — too many failures"));
        }
    }
}

// =============================================================================
//  EEPROM
// =============================================================================

// Commit the most recent N entries from the ring buffer to EEPROM flash.
// Called on LOG_STOP command or on request.
void commitRingToEEPROM() {
    if (eepromDone) return;

    uint16_t toCopy = ring.count;
    if (toCopy > EEPROM_MAX) toCopy = EEPROM_MAX;

    // Walk from oldest in ring
    uint16_t readIdx = ring.sdTail;
    uint16_t addr    = 0;

    for (uint16_t i = 0; i < toCopy; i++) {
        LogEntry &e = ring.buf[readIdx];
        EEPROM.put(addr,     e.t_ms);
        EEPROM.put(addr + 4, e.thrust_raw);
        EEPROM.put(addr + 8, e.thrust_filt);
        addr += EEPROM_REC;
        readIdx = (readIdx + 1) % RING_SIZE;
    }

    EEPROM.commit();
    eepromDone = true;
    Serial.print(F("EEPROM: committed "));
    Serial.print(toCopy);
    Serial.println(F(" records"));
}

// =============================================================================
//  ETHERNET HELPERS
// =============================================================================

void netSend(const char *msg) {
    if (activeClient && activeClient.connected()) {
        activeClient.print(msg);
        activeClient.print('\n');
    }
}

void netSendOK(const char *msg) {
    if (activeClient && activeClient.connected()) {
        activeClient.print(F("OK:"));
        activeClient.print(msg);
        activeClient.print('\n');
    }
    Serial.print(F("OK:"));
    Serial.println(msg);
}

void netSendErr(const char *msg) {
    if (activeClient && activeClient.connected()) {
        activeClient.print(F("ERR:"));
        activeClient.print(msg);
        activeClient.print('\n');
    }
    Serial.print(F("ERR:"));
    Serial.println(msg);
}

// Send a live data sample over TCP — both raw and filtered
void netSendData(uint32_t t_ms, float raw_n, float filt_n) {
    if (!activeClient || !activeClient.connected()) return;
    activeClient.print(F("DATA:"));
    activeClient.print(t_ms);
    activeClient.print(',');
    activeClient.print(raw_n, 4);
    activeClient.print(',');
    activeClient.println(filt_n, 4);
}

void sendStatus() {
    char buf[80];
    const char *stateStr[] = {"IDLE","INIT","TARE","ARMED","LOG_READY","IGNITE","LOGGING","COMPLETE"};
    snprintf(buf, sizeof(buf), "STATUS:state=%s,sd=%d,ring=%u,eeprom=%d",
             stateStr[(int)fwState], (int)sdReady, ring.count, (int)eepromDone);
    netSend(buf);
    Serial.println(buf);
}

// =============================================================================
//  PYRO
// =============================================================================

void safePyros() {
    digitalWrite(PYRO_A, LOW);
    digitalWrite(PYRO_B, LOW);
}

void firePyros() {
    digitalWrite(PYRO_A, HIGH);
    digitalWrite(PYRO_B, HIGH);
    delay(PYRO_FIRE_MS);
    safePyros();
}

// =============================================================================
//  COMMAND HANDLER
// Called with one complete newline-terminated command string.
//  All state transitions happen here.
// =============================================================================

void handleCommand(String &cmd) {
    cmd.trim();
    cmd.toUpperCase();
    if (cmd.length() == 0) return;

    Serial.print(F("CMD: ")); Serial.println(cmd);

    // --- STATUS (any state) ---
    if (cmd == "STATUS") {
        sendStatus();
        return;
    }

    // --- DISARM (any state) ---
    if (cmd == "DISARM") {
        safePyros();
        logging  = false;
        fwState  = FW_IDLE;
        netSendOK("DISARMED — returned to IDLE");
        netSend("STATE:IDLE");
        return;
    }

    // -------------------------------------------------------------------------
    //  IDLE
    // -------------------------------------------------------------------------
    if (fwState == FW_IDLE) {
        if (cmd == "INIT") {
            fwState = FW_INIT;
            netSend("STATE:INIT");

            if (!scale.is_ready()) {
                netSendErr("HX711 not ready — check wiring");
                fwState = FW_IDLE;
                netSend("STATE:IDLE");
                return;
            }
            float sample = scale.get_units(3);  // 3-sample average just for check
            (void)sample;   // value not critical here
            netSendOK("HX711 verified");
            netSendOK("Send TARE to zero the cell, then ARM");
            return;
        }
        netSendErr("In IDLE — send INIT first");
        return;
    }

    // -------------------------------------------------------------------------
    //  INIT
    // -------------------------------------------------------------------------
    if (fwState == FW_INIT) {
        if (cmd == "TARE") {
            fwState = FW_TARE;
            netSend("STATE:TARE");
            netSendOK("Taring... keep cell unloaded");
            scale.tare(10);  // average 10 readings
            emaValue = 0.0f;
            emaInit  = false;
            netSendOK("Tare complete — send ARM when ready");
            fwState = FW_TARE;
            return;
        }
        netSendErr("In INIT — send TARE first");
        return;
    }

    // -------------------------------------------------------------------------
    //  TARE
    // -------------------------------------------------------------------------
    if (fwState == FW_TARE) {
        if (cmd == "TARE") {
            // Allow re-tare
            scale.tare(10);
            emaValue = 0.0f; emaInit = false;
            netSendOK("Re-tare complete");
            return;
        }
        if (cmd == "ARM") {
            fwState = FW_ARMED;
            netSend("STATE:ARMED");
            netSendOK("System ARMED — send LOG_START to verify logging, then IGNITE.");
            return;
        }
        netSendErr("In TARE state — send ARM or TARE again");
        return;
    }

    // -------------------------------------------------------------------------
    //  ARMED  — logging NOT yet started; LOG_START must come first
    // -------------------------------------------------------------------------
    if (fwState == FW_ARMED) {
        if (cmd == "LOG_START") {
            // Reset ring and EMA, begin collecting samples
            ring.head   = 0;
            ring.sdTail = 0;
            ring.count  = 0;
            eepromDone  = false;
            emaValue    = 0.0f;
            emaInit     = false;
            logStartMs  = millis();
            lastSDFlushMs = millis();
            logging     = true;
            fwState     = FW_LOG_READY;
            netSend("STATE:LOG_READY");
            netSendOK("Logging verified and running. Send IGNITE when ready.");
            return;
        }
        if (cmd == "IGNITE") {
            netSendErr("Must LOG_START before IGNITE to verify logging.");
            return;
        }
        netSendErr("ARMED — send LOG_START first, then IGNITE.");
        return;
    }

    // -------------------------------------------------------------------------
    //  LOG_READY  — logging confirmed running, waiting for ignition command
    // -------------------------------------------------------------------------
    if (fwState == FW_LOG_READY) {
        if (cmd == "IGNITE") {
            fwState = FW_IGNITE;
            netSend("STATE:IGNITE");
            netSendOK("COUNTDOWN STARTED — logging already running");

            // Countdown — 10s to give crew time to clear
            for (int i = 10; i > 0; i--) {
                char buf[8];
                snprintf(buf, sizeof(buf), "T-%d", i);
                netSend(buf);
                Serial.println(buf);
                delay(1000);
            }

            // Fire pyros
            firePyros();
            netSendOK("PYROS FIRED");
            Serial.println(F("PYROS FIRED"));

            // Transition to LOGGING — data was already flowing since LOG_START
            fwState = FW_LOGGING;
            netSend("STATE:LOGGING");
            netSendOK("Motor should be burning. Send LOG_STOP when complete.");
            return;
        }
        if (cmd == "STATUS") { sendStatus(); return; }
        netSendErr("LOG_READY — send IGNITE to fire, or DISARM to abort.");
        return;
    }

    // -------------------------------------------------------------------------
    //  LOGGING
    // -------------------------------------------------------------------------
    if (fwState == FW_LOGGING) {
        if (cmd == "LOG_STOP") {
            logging = false;
            flushRingToSD();      // drain any remaining samples
            commitRingToEEPROM();
            fwState = FW_COMPLETE;
            netSend("STATE:COMPLETE");
            netSend("COMPLETE");
            netSendOK("Logging stopped. EEPROM committed.");
            return;
        }
        if (cmd == "STATUS") { sendStatus(); return; }
        netSendErr("Logging in progress — send LOG_STOP to end");
        return;
    }

    // -------------------------------------------------------------------------
    //  COMPLETE
    // -------------------------------------------------------------------------
    if (fwState == FW_COMPLETE) {
        if (cmd == "INIT") {
            // Allow restart for another firing in same session
            fwState = FW_IDLE;
            netSend("STATE:IDLE");
            handleCommand(const_cast<String&>(cmd = "INIT"));
            return;
        }
        netSendErr("Session complete — power cycle or send INIT to restart");
        return;
    }

    // Fallthrough
    netSendErr("Unknown command or invalid in current state");
}

// =============================================================================
//  SAMPLING — called from loop(), runs as fast as possible
//  Only actually reads HX711 when it signals ready AND the interval has elapsed.
// =============================================================================

void sampleHX711() {
    if (!logging) return;

    uint32_t nowUs = micros();
    if ((nowUs - lastSampleUs) < SAMPLE_INTERVAL_US) return;

    if (!scale.is_ready()) return;  // don't block — skip this tick

    lastSampleUs = nowUs;

    // Single reading, calibration-scaled → grams → Newtons
    float raw_g   = scale.get_units(1);
    float raw_n   = raw_g * GRAMS_TO_NEWTONS;

    // EMA filter
    float filt_n  = emaUpdate(raw_n);

    uint32_t t_ms = millis() - logStartMs;

    // Push both to ring buffer (SRAM)
    ringPush(t_ms, raw_n, filt_n);

    // Transmit both live over Ethernet
    netSendData(t_ms, raw_n, filt_n);
}

// =============================================================================
//  SETUP
// =============================================================================

void setup() {
    Serial.begin(115200);
    while (!Serial && millis() < 2000) {}
    Serial.println(F("\n=== STATIC FIRE LOGGER v3.0 — DJS Impulse ==="));

    // Pyros safe by default
    pinMode(PYRO_A, OUTPUT);
    pinMode(PYRO_B, OUTPUT);
    safePyros();

    // EEPROM
    EEPROM.begin(EEPROM_TOTAL);

    // HX711
    scale.begin(HX_DOUT, HX_SCK);
    scale.set_scale(HX_CALIBRATION);
    // NOTE: We do NOT tare in setup() — tare is commanded explicitly via TCP
    // so the operator controls zero timing, not firmware boot timing.
    Serial.println(F("HX711: configured (not tared — use TARE command)"));

    // SD
    initSD();

    // Ethernet (W5500 on default SPI)
    Ethernet.init(17);   // adjust CS pin if needed
    Ethernet.begin(MAC_ADDR, DEVICE_IP);
    tcpServer.begin();
    Serial.print(F("TCP server @ "));
    Serial.print(Ethernet.localIP());
    Serial.print(F(":"));
    Serial.println(TCP_PORT);

    // Visual ready signal
    pinMode(LED_BUILTIN, OUTPUT);
    for (int i = 0; i < 3; i++) {
        digitalWrite(LED_BUILTIN, HIGH); delay(100);
        digitalWrite(LED_BUILTIN, LOW);  delay(100);
    }
    Serial.println(F("READY — connect via TCP and send INIT"));
}

// =============================================================================
//  LOOP — non-blocking, cooperative
// =============================================================================

void loop() {
    uint32_t nowMs = millis();

    // ---- Ethernet: accept new client or keep existing ----
    if (!activeClient || !activeClient.connected()) {
        EthernetClient newClient = tcpServer.available();
        if (newClient) {
            activeClient = newClient;
            Serial.println(F("Client connected"));
            netSendOK("STATIC FIRE LOGGER v3.0 — send INIT to begin");
            sendStatus();
        }
    }

    // ---- Read commands from client ----
    if (activeClient && activeClient.connected()) {
        static String cmdBuf = "";
        while (activeClient.available()) {
            char c = activeClient.read();
            if (c == '\n') {
                handleCommand(cmdBuf);
                cmdBuf = "";
            } else if (c != '\r') {
                cmdBuf += c;
                if (cmdBuf.length() > 64) cmdBuf = "";  // guard against runaway
            }
        }
    }

    /*// ---- ADD THIS: Read commands from USB SERIAL MONITOR ----
    if (Serial.available()) {
        static String serialBuf = "";
        while (Serial.available()) {
            char c = Serial.read();
            if (c == '\n') {
                handleCommand(serialBuf);
                serialBuf = "";
            } else if (c != '\r') {
                serialBuf += c;
                if (serialBuf.length() > 64) serialBuf = "";
            }
        }
    }*/

    // ---- HX711 sampling (100 Hz, non-blocking) ----
    sampleHX711();

    // ---- SD flush (periodic, non-blocking) ----
    if (logging && (nowMs - lastSDFlushMs) >= SD_FLUSH_INTERVAL_MS) {
        flushRingToSD();
        lastSDFlushMs = nowMs;
    }
}

// =============================================================================
//  CALIBRATION NOTES
//  1. Find raw scale factor:
//     a. tare with nothing on cell
//     b. place known mass (e.g. 1 kg = 9.807 N)
//     c. read scale.get_units(10) without set_scale — gives raw ADC count
//     d. calibration_factor = raw_count / mass_in_grams
//  2. Verify: after set_scale(factor) and tare(), get_units(10) should
//     return ~1000 for a 1 kg load.
//  3. set_scale() is negative when the cell reads negative for compression.
//
//  EMA TUNING
//  EMA_ALPHA lives at the top of the config section.
//  - 0.1 : heavy smoothing, slow to track fast thrust changes
//  - 0.2 : good default for noisy lab/test-stand environments
//  - 0.4 : lighter smoothing, tracks ignition transient well
//  - 1.0 : no filtering (pure raw passthrough)
//  Procedure: run a dry LOG_START session, watch DATA: lines over TCP.
//  Compare raw_N vs filt_N columns. If filt lags ignition spike, increase alpha.
//  If filt still looks noisy, decrease alpha.
//  Both columns are always logged so you can post-process with a different
//  alpha offline if needed.
// =============================================================================
