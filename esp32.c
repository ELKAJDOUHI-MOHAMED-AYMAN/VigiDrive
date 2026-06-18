#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <math.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// ═══ WIFI + MQTT ══════════════════════════════
const char* WIFI_SSID     = "meed";
const char* WIFI_PASSWORD = "12345678";
const char* MQTT_SERVER   = "192.168.137.205";
const int   MQTT_PORT     = 1883;
const char* MQTT_CLIENT   = "ESP32-VigiDrive";

// Topics
const char* TOPIC_TILT     = "vigidrive/tilt";      // envoie MPU → Pi
const char* TOPIC_COMMANDE = "vigidrive/commande";  // reçoit fusion Node-RED

// ═══ PINS ════════════════════════════════════
#define RED_LED    18
#define ORANGE_LED 19
#define GREEN_LED  15
#define BUZZER     23

// ═══ MPU ═════════════════════════════════════
const float   TILT_THRESHOLD = 35.0;
unsigned long tiltStart      = 0;
bool          tiltDetected   = false;

// ═══ ÉTAT COMMANDE (reçu de Node-RED) ════════
int           cmd_niveau     = 0;
unsigned long lastCommande   = 0;
const unsigned long CMD_TIMEOUT = 10000; // reset après 10s sans commande

Adafruit_MPU6050 mpu;
WiFiClient       espClient;
PubSubClient     mqttClient(espClient);

// ═══ LEDS + BUZZER ═══════════════════════════
void setNormal() {
    digitalWrite(GREEN_LED,  HIGH);
    digitalWrite(ORANGE_LED, LOW);
    digitalWrite(RED_LED,    LOW);
    digitalWrite(BUZZER,     LOW);
}

void setWarning() {
    digitalWrite(GREEN_LED,  LOW);
    digitalWrite(ORANGE_LED, HIGH);
    digitalWrite(RED_LED,    LOW);
    digitalWrite(BUZZER,     LOW);
}

void setDanger() {
    digitalWrite(GREEN_LED,  LOW);
    digitalWrite(ORANGE_LED, LOW);
    digitalWrite(RED_LED,    HIGH);
    digitalWrite(BUZZER,     HIGH);
}

void setCritique() {
    // Rouge clignotant rapide
    digitalWrite(ORANGE_LED, LOW);
    digitalWrite(GREEN_LED,  LOW);
    digitalWrite(RED_LED,    HIGH);
    digitalWrite(BUZZER,     HIGH);
    delay(150);
    digitalWrite(RED_LED,    LOW);
    delay(150);
}

// ═══ CALLBACK MQTT ════════════════════════════
void mqttCallback(char* topic, byte* payload, unsigned int length) {
    String msg = "";
    for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];

    Serial.printf("MQTT [%s]: %s\n", topic, msg.c_str());

    StaticJsonDocument<256> doc;
    if (deserializeJson(doc, msg)) return;

    // Commande fusionnée depuis Node-RED
    if (String(topic) == TOPIC_COMMANDE) {
        cmd_niveau   = doc["niveau"]  | 0;
        lastCommande = millis();

        const char* etat   = doc["etat"]   | "NORMAL";
        bool        buzzer = doc["buzzer"] | false;
        float       perclos= doc["perclos"]| 0.0;
        float       tilt   = doc["tilt"]   | 0.0;

        Serial.printf(">>> COMMANDE: niveau=%d etat=%s buzzer=%d PERCLOS=%.1f TILT=%.1f\n",
                      cmd_niveau, etat, buzzer, perclos, tilt);
    }
}

// ═══ CONNEXION WIFI ══════════════════════════
void connectWifi() {
    Serial.printf("WiFi: %s ", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    while (WiFi.status() != WL_CONNECTED) {
        delay(500); Serial.print(".");
    }
    Serial.printf("\nConnecte! IP: %s\n", WiFi.localIP().toString().c_str());
}

// ═══ CONNEXION MQTT ══════════════════════════
void connectMqtt() {
    while (!mqttClient.connected()) {
        Serial.print("MQTT...");
        if (mqttClient.connect(MQTT_CLIENT)) {
            Serial.println("OK!");
            mqttClient.subscribe(TOPIC_COMMANDE); // écoute fusion Node-RED
        } else {
            Serial.printf("echec rc=%d\n", mqttClient.state());
            delay(2000);
        }
    }
}

// ═══ SETUP ═══════════════════════════════════
void setup() {
    Serial.begin(115200);
    pinMode(RED_LED,    OUTPUT);
    pinMode(ORANGE_LED, OUTPUT);
    pinMode(GREEN_LED,  OUTPUT);
    pinMode(BUZZER,     OUTPUT);
    setNormal();

    if (!mpu.begin()) {
        Serial.println("MPU6050 NON TROUVE!");
        while (1);
    }
    Serial.println("MPU6050 OK!");

    connectWifi();
    mqttClient.setServer(MQTT_SERVER, MQTT_PORT);
    mqttClient.setCallback(mqttCallback);
    connectMqtt();
}

// ═══ LOOP ════════════════════════════════════
void loop() {
    if (!mqttClient.connected()) connectMqtt();
    mqttClient.loop();

    // ── Lecture MPU6050 ──────────────────────
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);

    float ax = a.acceleration.x;
    float ay = a.acceleration.y;
    float az = a.acceleration.z;

    float pitch      = atan2(ax, sqrt(ay*ay + az*az)) * 180.0 / PI;
    float roll       = atan2(ay, sqrt(ax*ax + az*az)) * 180.0 / PI;
    float totalTilt  = sqrt(pitch*pitch + roll*roll);
    bool  headTilted = totalTilt > TILT_THRESHOLD;

    // ── Niveau MPU ───────────────────────────
    int mpu_niveau = 0;
    if (headTilted) {
        if (!tiltDetected) { tiltStart = millis(); tiltDetected = true; }
        unsigned long dur = millis() - tiltStart;
        if      (dur >= 10000) mpu_niveau = 3;
        else if (dur >= 6000)  mpu_niveau = 3;
        else if (dur >= 3000)  mpu_niveau = 2;
        else                   mpu_niveau = 1;
    } else {
        tiltDetected = false;
        mpu_niveau   = 0;
    }

    // ── Publier tilt → Node-RED ──────────────
    StaticJsonDocument<128> doc;
    doc["pitch"]   = round(pitch * 10) / 10.0;
    doc["roll"]    = round(roll  * 10) / 10.0;
    doc["tilt"]    = round(totalTilt * 10) / 10.0;
    doc["niveau"]  = mpu_niveau;
    doc["tilted"]  = headTilted;
    char buf[128];
    serializeJson(doc, buf);
    mqttClient.publish(TOPIC_TILT, buf);

    // ── Timeout commande (10s sans réponse) ──
    if (millis() - lastCommande > CMD_TIMEOUT) {
        cmd_niveau = 0;
    }

    // ── LEDs/Buzzer selon commande Node-RED ──
    // Note: Node-RED fait la fusion camera+MPU
    // et envoie le niveau final
    if      (cmd_niveau >= 4) { setCritique(); Serial.println("🚨 CRITIQUE"); }
    else if (cmd_niveau >= 3) { setDanger();   Serial.println("🔴 DANGER");   }
    else if (cmd_niveau >= 1) { setWarning();  Serial.println("🟡 WARNING");  }
    else                      { setNormal();                                   }

    Serial.printf("MPU: P=%.1f R=%.1f T=%.1f(%s) | CMD=%d\n",
        pitch, roll, totalTilt,
        headTilted ? "TILT" : "OK",
        cmd_niveau);

    delay(100);
}