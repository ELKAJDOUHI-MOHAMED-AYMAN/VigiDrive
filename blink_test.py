import cv2
import mediapipe as mp
import numpy as np
import time
import json
from collections import deque
import paho.mqtt.client as mqtt

# ═══ MQTT CONFIG ════════════════════════════════
MQTT_BROKER      = "localhost"
MQTT_PORT        = 1883
TOPIC_ALERTES    = "vigidrive/alertes"
TOPIC_RAPPORT    = "vigidrive/rapport"
RAPPORT_SEC      = 30
WARMUP_SEC       = 15

mqtt_client = mqtt.Client(client_id="vigidrive-mediapipe")
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start()

def publier(niveau, type_alerte, details={}):
    payload = {
        "niveau":    niveau,
        "type":      type_alerte,
        "timestamp": time.strftime("%H:%M:%S"),
        **details
    }
    mqtt_client.publish(TOPIC_ALERTES, json.dumps(payload))
    print(f"\n>>> MQTT niveau={niveau} type={type_alerte} | {details}")

debut_rapport   = time.time()
nb_alertes      = 0
etat_precedent  = None

# ═══ STREAM URL ══════════════════════════════════
url = "http://127.0.0.1:5000/stream"
cap = cv2.VideoCapture(url)

# ═══ MEDIAPIPE ═══════════════════════════════════
mp_face_mesh = mp.solutions.face_mesh
face_mesh = mp_face_mesh.FaceMesh(
    max_num_faces=1,
    refine_landmarks=True,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

UPPER_LIP    = 13
LOWER_LIP    = 14
MOUTH_LEFT   = 78
MOUTH_RIGHT  = 308
LEFT_EYE     = [33,160,158,133,153,144]
RIGHT_EYE    = [362,385,387,263,373,380]

ear_history       = deque(maxlen=300)
mar_history       = deque(maxlen=300)
eye_history       = deque()
microsleep_history= deque()
yawn_history      = deque()

yawn_count        = 0
yawn_start_time   = None
yawn_reported     = False
WINDOW_SECONDS    = 60
LONG_MICROSLEEP_SECONDS = 4

def dist(p1, p2):
    return np.linalg.norm(np.array(p1) - np.array(p2))

def eye_ear(landmarks, eye):
    p1=(landmarks[eye[0]].x, landmarks[eye[0]].y)
    p2=(landmarks[eye[1]].x, landmarks[eye[1]].y)
    p3=(landmarks[eye[2]].x, landmarks[eye[2]].y)
    p4=(landmarks[eye[3]].x, landmarks[eye[3]].y)
    p5=(landmarks[eye[4]].x, landmarks[eye[4]].y)
    p6=(landmarks[eye[5]].x, landmarks[eye[5]].y)
    v1 = dist(p2,p6)
    v2 = dist(p3,p5)
    h  = dist(p1,p4)
    return (v1+v2)/(2*h)

def calculate_ear(landmarks):
    return (eye_ear(landmarks, LEFT_EYE) + eye_ear(landmarks, RIGHT_EYE)) / 2

def calculate_mar(landmarks):
    upper    = (landmarks[UPPER_LIP].x,   landmarks[UPPER_LIP].y)
    lower    = (landmarks[LOWER_LIP].x,   landmarks[LOWER_LIP].y)
    left     = (landmarks[MOUTH_LEFT].x,  landmarks[MOUTH_LEFT].y)
    right    = (landmarks[MOUTH_RIGHT].x, landmarks[MOUTH_RIGHT].y)
    vertical   = dist(upper, lower)
    horizontal = dist(left,  right)
    return vertical / horizontal

# ═══ CALIBRATION 5s ══════════════════════════════
print("\n=== CALIBRATION 5 SECONDES ===")
print("Regarde normalement la camera...\n")
start = time.time()
while time.time() - start < 5:
    ret, frame = cap.read()
    if not ret:
        continue
    rgb     = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = face_mesh.process(rgb)
    if results.multi_face_landmarks:
        landmarks = results.multi_face_landmarks[0].landmark
        ear_history.append(calculate_ear(landmarks))
        mar_history.append(calculate_mar(landmarks))

print("Calibration terminee — Detection active\n")

blink_count          = 0
total_microsleeps    = 0
closed_frames        = 0
microsleep_reported  = False
eye_closure_start    = None
fps                  = 15
frame_counter        = 0
fps_start            = time.time()
last_print           = time.time()
debut_detection      = time.time()

# ═══ BOUCLE PRINCIPALE ═══════════════════════════
while True:
    ret, frame = cap.read()
    if not ret:
        continue

    frame_counter += 1
    rgb     = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    results = face_mesh.process(rgb)

    if not results.multi_face_landmarks:
        continue

    landmarks = results.multi_face_landmarks[0].landmark
    ear = calculate_ear(landmarks)
    mar = calculate_mar(landmarks)
    ear_history.append(ear)
    mar_history.append(mar)

    baseline      = np.percentile(list(ear_history), 90) if len(ear_history) > 50 else ear
    threshold     = baseline * 0.65
    mar_baseline  = np.percentile(list(mar_history), 50) if len(mar_history) > 50 else mar
    mar_threshold = max(mar_baseline * 3.0, 0.35)

    if time.time() - fps_start >= 5:
        fps           = frame_counter / (time.time() - fps_start)
        frame_counter = 0
        fps_start     = time.time()

    BLINK_MIN_FRAMES  = max(2, int(0.10 * fps))
    BLINK_MAX_FRAMES  = max(6, int(0.40 * fps))
    MICROSLEEP_FRAMES = int(2.0 * fps)

    eye_closed = ear < threshold
    mouth_open = mar > mar_threshold
    now        = time.time()

    eye_history.append((now, eye_closed))
    while eye_history and now - eye_history[0][0] > WINDOW_SECONDS:
        eye_history.popleft()

    # ── Yeux fermés ──────────────────────────────
    if eye_closed:
        if closed_frames == 0:
            eye_closure_start = now
        closed_frames += 1
        if closed_frames >= MICROSLEEP_FRAMES and not microsleep_reported:
            microsleep_reported = True
            nb_alertes += 1
            print("\nDROWSINESS EVENT DETECTED")
    else:
        if eye_closure_start is not None:
            duration_sec = now - eye_closure_start
            if duration_sec >= 2:
                total_microsleeps += 1
                microsleep_history.append((now, duration_sec))
                if duration_sec < 5:
                    print(f"\nMICROSLEEP #{total_microsleeps} ({duration_sec:.1f}s)")
                elif duration_sec < 15:
                    print(f"\nPROLONGED EYE CLOSURE ({duration_sec:.1f}s)")
                else:
                    print(f"\nCRITICAL EYE CLOSURE ({duration_sec:.1f}s)")

        if BLINK_MIN_FRAMES <= closed_frames <= BLINK_MAX_FRAMES and not microsleep_reported:
            blink_count += 1
            print(f"BLINK #{blink_count}")

        closed_frames       = 0
        eye_closure_start   = None
        microsleep_reported = False

    # ── Bâillement ───────────────────────────────
    if mouth_open:
        if yawn_start_time is None:
            yawn_start_time = now
        yawn_duration = now - yawn_start_time
        if yawn_duration >= 1.5 and not yawn_reported:
            yawn_count += 1
            yawn_history.append((now, yawn_duration))
            yawn_reported = True
            print(f"\nYAWN #{yawn_count}")
    else:
        yawn_start_time = None
        yawn_reported   = False

    while microsleep_history and now - microsleep_history[0][0] > 60:
        microsleep_history.popleft()
    while yawn_history and now - yawn_history[0][0] > 60:
        yawn_history.popleft()

    recent_yawns       = len(yawn_history)
    recent_microsleeps = len(microsleep_history)
    long_microsleep    = any(d >= LONG_MICROSLEEP_SECONDS for _, d in microsleep_history)

    perclos = (sum(1 for _, c in eye_history if c) / len(eye_history) * 100) if eye_history else 0

    warmup_ok = (now - debut_detection > WARMUP_SEC) and (len(eye_history) > fps * 5)

    # ── Status ────────────────────────────────────
    if not warmup_ok:
        status = "NORMAL"
    elif perclos >= 30 or recent_microsleeps >= 2 or recent_yawns >= 3:
        status = "DROWSY"
    elif perclos >= 15 or recent_microsleeps >= 1 or long_microsleep or recent_yawns >= 1:
        status = "WARNING"
    else:
        status = "NORMAL"

    # ── MQTT sur CHANGEMENT d'etat (allume ET eteint l'ESP32)
    if status != etat_precedent:
        niveau_map = {"NORMAL": 0, "WARNING": 1, "DROWSY": 3}
        publier(niveau_map[status], status.lower(), {
            "PERCLOS":       round(perclos, 1),
            "microsommeils": recent_microsleeps,
            "baillements":   recent_yawns
        })
        etat_precedent = status

    # ── MQTT Rapport 30s ──────────────────────────
    if now - debut_rapport >= RAPPORT_SEC:
        score   = min(100, int(perclos*2 + recent_yawns*10 + nb_alertes*5))
        rapport = {
            "score":        score,
            "etat":         status,
            "PERCLOS":      round(perclos, 1),
            "baillements":  yawn_count,
            "alertes":      nb_alertes,
            "clignements":  blink_count,
            "microsommeils": total_microsleeps
        }
        mqtt_client.publish(TOPIC_RAPPORT, json.dumps(rapport))
        print(f"\nRAPPORT 30s | Score:{score}/100 | {status} | PERCLOS:{perclos:.1f}% | Bail:{yawn_count} | Blinks:{blink_count}")
        nb_alertes    = 0
        debut_rapport = now

    # ── Affichage ─────────────────────────────────
    if now - last_print > 2:
        state = "CLOSED" if eye_closed else "OPEN"
        print(
            f"EAR={ear:.3f} | MAR={mar:.3f} | MTH={mar_threshold:.3f} | "
            f"PERCLOS={perclos:.1f}% | {status} | "
            f"B={blink_count} | Y={yawn_count} | "
            f"RM={recent_microsleeps} | FPS={fps:.1f} | {state}"
        )
        last_print = now

    time.sleep(0.05)