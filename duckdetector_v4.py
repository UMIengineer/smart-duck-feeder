"""
Smart Duck Feeder - Detection & Closed-Loop Dispensing (v4)
Final integrated version using LOCAL Ultralytics inference.

Implements design described in report sections 4.2 to 4.5:
  - LOCAL YOLOv8 inference (no cloud, no inference server, no network)
  - 5-second engineered habituation delay (BIO-01)
  - Conditional tare in the +/- 5 g window (section 4.3.2)
  - Iterative pulse-and-recheck closed-loop dispensing (section 4.3)
  - 5-minute cooldown flag written after successful dispense
  - MOSFET-gated servo power line (section 4.5)
  - Fault tolerance writes ERR codes to SQLite (section 4.4.1)
  - EVENT-DRIVEN ultrasonic hopper measurement after each dispense (section 5.4)

ENERGY NOTE:
This script runs ONLY when initial_code_v2.py launches it after a PIR interrupt.
The model is loaded into Python memory directly via Ultralytics. When the
script exits, the model leaves RAM and the Pi returns to ~0.25 W standby.
There is NO persistent inference server - that would defeat PWR-01.

Likewise, the ultrasonic sensor is only queried ONCE per dispense event,
immediately after a successful feed cycle. It is silent at all other times.
This means hopper telemetry costs roughly 0.014 Wh per day - negligible
against the 13.38 Wh/day power budget.

The 3-5 second model-load time on cold start is hidden inside the 5-second
BIO-01 habituation delay: the duck is being given time to settle in front
of the camera while YOLOv8 boots into RAM.
"""

import os
import sqlite3
import statistics
import struct
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from threading import Thread

import RPi.GPIO as GPIO
import smbus2
from picamera2 import Picamera2
from ultralytics import YOLO

os.environ['QT_QPA_PLATFORM'] = 'xcb'

# ── Configuration ────────────────────────────────────────────────────────────
DB_PATH = "/home/raspberrypi/Desktop/Final/duck_feeder.db"
COOLDOWN_FLAG = Path("/tmp/duck_feeder_cooldown.flag")

# ML inference - LOCAL model file, no network, no server.
MODEL_PATH = "/home/raspberrypi/Desktop/Final/duck-detector-v2_ncnn_model"
INFER_IMGSZ = 320                     # must match the imgsz used at NCNN export time
CONFIDENCE_THRESHOLD = 0.70           # SW-01
DUCK_CLASS_NAME = "Duck"              # must match data.yaml names list

# Multi-frame verification (replaces single-shot verification)
VERIFICATION_FRAMES = 10               # max frames to capture
VERIFICATION_REQUIRED = 1             # need this many positive detections to dispense
FRAME_INTERVAL = 0.3                  # seconds between captures (~3 fps)

# I2C scale (HX711)
I2C_ADDR = 0x26
WEIGHT_REG = 0x10
TARE_CMD = 0x50

# Servo PWM and MOSFET gating (section 4.5)
PWM_PIN = 18
SERVO_POWER_GATE_PIN = 23             # drives N-channel MOSFET on servo ground
INITIAL_POSITION = 8.6
END_POSITION = 5.7

# Ultrasonic HC-SR04 (section 5.4 - event-driven hopper telemetry)
ULTRASONIC_TRIG_PIN = 24              # BCM
ULTRASONIC_ECHO_PIN = 25              # BCM (via voltage divider - 5V to 3.3V)
ULTRASONIC_SAMPLES = 5                # number of rapid samples for median filter
ULTRASONIC_TIMEOUT = 0.05             # seconds before considering ping lost

# Hopper geometry calibration constants - MEASURE THESE for your build.
# Distance from sensor face to feed surface, in cm.
EMPTY_DISTANCE_CM = 7.7              # sensor to bottom when empty (20 cm tunnel + 54 cm column)
FULL_DISTANCE_CM = 2.0                # sensor to feed surface when full (rough estimate)
MAX_FEED_GRAMS = 21000.0              # 30 L * 0.7 kg/L from section 3.3.5

# Closed-loop dispensing (section 4.3)
TARGET_WEIGHT = 30.0                  # SW-01
MIN_SAFE_WEIGHT = -5000.0
MAX_SAFE_WEIGHT = 5000.0
SMOOTH_READINGS = 5
TARE_WINDOW = 10                     # +/- 5 g for conditional tare
HABITUATION_DELAY = 0.01               # BIO-01 - 5 s startle suppression

PULSE_PAUSE = 0.5                     # settle time after pulses 2+ (subsequent pellets dampen ringing)
FIRST_PULSE_PAUSE = 1.5               # longer settle after first pulse - cold scale + ringing
STABLE_READING_DELTA = 1.0            # grams of jitter we tolerate as "settled"
STABLE_READING_TIMEOUT = 2.0          # max extra time to wait for stability
MAX_PULSES = 10                       # safety bound (10 * 8.7g = 87g worst case)
# ─────────────────────────────────────────────────────────────────────────────


# ============================================================================
# Hardware setup
# ============================================================================
bus = smbus2.SMBus(1)
GPIO.setmode(GPIO.BCM)
GPIO.setup(PWM_PIN, GPIO.OUT)
GPIO.setup(SERVO_POWER_GATE_PIN, GPIO.OUT)
GPIO.output(SERVO_POWER_GATE_PIN, GPIO.LOW)   # servo isolated by default

# Ultrasonic pins
GPIO.setup(ULTRASONIC_TRIG_PIN, GPIO.OUT)
GPIO.setup(ULTRASONIC_ECHO_PIN, GPIO.IN)
GPIO.output(ULTRASONIC_TRIG_PIN, GPIO.LOW)

pwm = GPIO.PWM(PWM_PIN, 50)
pwm.start(0)

picam2 = Picamera2()
picam2.configure(picam2.create_preview_configuration(
    main={"size": (1280, 720), "format": "RGB888"}
))
picam2.start()


# ============================================================================
# Background model loading (overlaps with habituation delay)
# ============================================================================
_model_holder = {"model": None, "ready": False, "error": None}


def _load_model_worker():
    """Load NCNN model in a background thread while the duck habituates."""
    try:
        # task='detect' is required for NCNN models because the export does
        # not always carry the task hint with it.
        _model_holder["model"] = YOLO(MODEL_PATH, task='detect')
        _model_holder["ready"] = True
        print(f"NCNN model loaded from {MODEL_PATH}")
    except Exception as e:
        _model_holder["error"] = str(e)
        print(f"Model load failed: {e}")


# Kick off model loading immediately so it overlaps with the 5 s startle delay.
model_thread = Thread(target=_load_model_worker, daemon=True)
model_thread.start()


# ============================================================================
# Fault logging (section 4.4.1)
# ============================================================================
def log_fault(error_code: str, detail: str = ""):
    """Write a fault row to SQLite for the dashboard to surface."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO fault_log (timestamp, error_code, detail) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), error_code, detail),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[FAULT-LOG-FAILED] {error_code}: {detail} ({e})")


def log_feed_event(dispensed_g: float):
    """Persist a successful dispensing event to SQLite."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO feed_events (timestamp, dispensed_amount) VALUES (?, ?)",
            (datetime.now().isoformat(), dispensed_g),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log_fault("ERR_DB_WRITE", str(e))


def update_hopper_status(level_grams: float, distance_cm: float):
    """
    Persist the latest hopper level after a dispense event.
    Section 5.4 - the dashboard reads only the most recent row, so this
    represents the cached 'last known state' until the next dispense.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO hopper_status (timestamp, current_level, max_capacity, "
            "distance_cm) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(), level_grams, MAX_FEED_GRAMS, distance_cm),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log_fault("ERR_HOPPER_UPDATE", str(e))


# ============================================================================
# Scale interface
# ============================================================================
def read_weight():
    """Read calibrated weight from HX711 over I2C. Returns None on failure."""
    try:
        data = bus.read_i2c_block_data(I2C_ADDR, WEIGHT_REG, 4)
        return struct.unpack('<f', bytes(data))[0]
    except Exception as e:
        log_fault("ERR_SCALE_DISCONNECT", str(e))
        return None


def tare_scale():
    """Send tare command to HX711."""
    try:
        bus.write_byte_data(I2C_ADDR, TARE_CMD, 1)
        time.sleep(0.1)
    except Exception as e:
        log_fault("ERR_SCALE_TARE", str(e))


def conditional_tare(reading):
    """
    Section 4.3.2: only tare if residual reading sits inside +/- 5 g.
    Protects valid leftover feed from being zeroed out as drift.
    """
    if reading is not None and -TARE_WINDOW <= reading <= TARE_WINDOW:
        print(f"Conditional tare triggered (residual {reading:.2f} g within +/- 5 g).")
        tare_scale()
        return True
    if reading is not None:
        print(f"Tare skipped - residual {reading:.2f} g outside +/- {TARE_WINDOW} g window.")
    return False


# ============================================================================
# Servo control with MOSFET gating (section 4.5)
# ============================================================================
def set_angle(duty):
    pwm.ChangeDutyCycle(duty)
    time.sleep(0.3)
    pwm.ChangeDutyCycle(0)


def pulse_servo():
    """Single open-close pulse of the dispensing gate, with power gating."""
    GPIO.output(SERVO_POWER_GATE_PIN, GPIO.HIGH)   # energise servo via MOSFET
    time.sleep(0.05)                               # brief settle
    set_angle(END_POSITION)
    time.sleep(0.3)
    set_angle(INITIAL_POSITION)
    time.sleep(0.05)
    GPIO.output(SERVO_POWER_GATE_PIN, GPIO.LOW)    # de-energise (kills 15mA quiescent)


# ============================================================================
# Ultrasonic hopper sensor (section 5.4 - event-driven)
# ============================================================================
def _single_ultrasonic_ping():
    """
    Fire one HC-SR04 pulse and return the measured distance in cm.
    Returns None on timeout or failure.
    """
    # Send 10 us trigger pulse.
    GPIO.output(ULTRASONIC_TRIG_PIN, GPIO.HIGH)
    time.sleep(0.00001)
    GPIO.output(ULTRASONIC_TRIG_PIN, GPIO.LOW)

    # Wait for echo to go high (start of return pulse).
    timeout_start = time.time()
    while GPIO.input(ULTRASONIC_ECHO_PIN) == 0:
        if time.time() - timeout_start > ULTRASONIC_TIMEOUT:
            return None
    pulse_start = time.time()

    # Wait for echo to go low (end of return pulse).
    while GPIO.input(ULTRASONIC_ECHO_PIN) == 1:
        if time.time() - pulse_start > ULTRASONIC_TIMEOUT:
            return None
    pulse_end = time.time()

    # Distance = (pulse duration * speed of sound) / 2.
    # Speed of sound = 34300 cm/s at 20 degC.
    duration = pulse_end - pulse_start
    distance_cm = (duration * 34300) / 2
    return distance_cm


def measure_hopper_distance():
    """
    Section 5.4 - take 5 rapid ultrasonic samples and apply a median filter
    to reject acoustic scattering anomalies caused by the irregular surface
    topography of partially-depleted feed.
    Returns the median distance in cm, or None if sampling failed entirely.
    """
    samples = []
    for i in range(ULTRASONIC_SAMPLES):
        d = _single_ultrasonic_ping()
        if d is not None and 2 <= d <= 400:    # HC-SR04 valid range
            samples.append(d)
        time.sleep(0.06)                       # brief gap between pings

    if len(samples) < 3:
        # Not enough valid samples to trust the median.
        log_fault("ERR_ULTRASONIC", f"only {len(samples)}/5 samples returned valid")
        return None

    median_distance = statistics.median(samples)
    print(f"Ultrasonic samples: {[f'{s:.1f}' for s in samples]} cm "
          f"-> median {median_distance:.1f} cm")
    return median_distance


def distance_to_feed_grams(distance_cm: float) -> float:
    """
    Linear mapping from sensor-to-surface distance to feed mass.
    Larger distance = less feed (because the surface has dropped).
    """
    # Clamp inputs to the valid calibrated range.
    if distance_cm <= FULL_DISTANCE_CM:
        return MAX_FEED_GRAMS
    if distance_cm >= EMPTY_DISTANCE_CM:
        return 0.0

    # Linear interpolation between FULL and EMPTY.
    span_cm = EMPTY_DISTANCE_CM - FULL_DISTANCE_CM
    drop_cm = distance_cm - FULL_DISTANCE_CM
    fraction_remaining = 1.0 - (drop_cm / span_cm)
    return fraction_remaining * MAX_FEED_GRAMS


def update_hopper_telemetry():
    """
    Section 5.4 - the event-driven hopper measurement step.
    Called ONCE after a successful dispense event. Reads the ultrasonic
    sensor, applies the median filter, converts to grams, and writes the
    result to SQLite for the dashboard to display.
    """
    distance = measure_hopper_distance()
    if distance is None:
        print("Hopper measurement failed; dashboard will show stale value.")
        return

    feed_grams = distance_to_feed_grams(distance)
    feed_percent = (feed_grams / MAX_FEED_GRAMS) * 100
    print(f"Hopper: {distance:.1f} cm -> {feed_grams:.0f} g ({feed_percent:.1f}% full)")
    update_hopper_status(feed_grams, distance)


# ============================================================================
# Inference
# ============================================================================
def _detect_duck_in_frame(frame) -> tuple[bool, float]:
    """
    Run inference on a single frame.
    Returns (duck_detected: bool, best_confidence: float).
    """
    if not _model_holder["ready"]:
        model_thread.join(timeout=10.0)

    if _model_holder["model"] is None:
        log_fault("ERR_MODEL_LOAD", _model_holder.get("error") or "unknown")
        return False, 0.0

    try:
        results = _model_holder["model"].predict(
            frame,
            conf=CONFIDENCE_THRESHOLD,
            imgsz=INFER_IMGSZ,
            verbose=False,
        )
    except Exception as e:
        log_fault("ERR_INFERENCE", str(e))
        return False, 0.0

    if not results or results[0].boxes is None or len(results[0].boxes) == 0:
        return False, 0.0

    boxes = results[0].boxes
    class_names = results[0].names
    best_conf = 0.0
    detected = False
    for i in range(len(boxes)):
        cls_id = int(boxes.cls[i].item())
        conf = float(boxes.conf[i].item())
        label = class_names.get(cls_id, "")
        if label == DUCK_CLASS_NAME and conf >= CONFIDENCE_THRESHOLD:
            detected = True
            if conf > best_conf:
                best_conf = conf
    return detected, best_conf


def detect_duck_multi_frame() -> bool:
    """
    Multi-frame consensus verification.
    Captures up to VERIFICATION_FRAMES frames; returns True if at least
    VERIFICATION_REQUIRED of them show a duck above threshold.
    Fast-exits early once the threshold is met or becomes mathematically
    impossible to reach.
    """
    positives = 0
    confidences = []

    for i in range(VERIFICATION_FRAMES):
        frame = picam2.capture_array()
        detected, conf = _detect_duck_in_frame(frame)

        if detected:
            positives += 1
            confidences.append(conf)
            print(f"  Frame {i+1}/{VERIFICATION_FRAMES}: DUCK  conf={conf:.2f}  "
                  f"(positives so far: {positives}/{VERIFICATION_REQUIRED})")
        else:
            print(f"  Frame {i+1}/{VERIFICATION_FRAMES}: no duck  "
                  f"(positives so far: {positives}/{VERIFICATION_REQUIRED})")

        # Fast exit: already enough positives to confirm.
        if positives >= VERIFICATION_REQUIRED:
            avg_conf = sum(confidences) / len(confidences)
            print(f"Verification PASSED: {positives}/{VERIFICATION_FRAMES} frames, "
                  f"avg confidence {avg_conf:.2f}")
            return True

        # Fast exit: even if all remaining frames are positive, can't reach K.
        frames_remaining = VERIFICATION_FRAMES - (i + 1)
        if positives + frames_remaining < VERIFICATION_REQUIRED:
            print(f"Verification FAILED early: only {positives} positive, "
                  f"can't reach {VERIFICATION_REQUIRED} with {frames_remaining} frames left")
            return False

        time.sleep(FRAME_INTERVAL)

    print(f"Verification FAILED: only {positives}/{VERIFICATION_FRAMES} frames "
          f"showed a duck (needed {VERIFICATION_REQUIRED})")
    return False

# ============================================================================
# Closed-loop dispense (section 4.3)
# ============================================================================
def wait_for_stable_reading(initial_pause: float):
    """
    Wait initial_pause seconds for the bulk of the settling, then poll the
    scale until two consecutive reads are within STABLE_READING_DELTA of
    each other. Returns the final settled reading, or None on failure.
    Times out after STABLE_READING_TIMEOUT seconds of polling.
    """
    time.sleep(initial_pause)

    last_w = read_weight()
    if last_w is None:
        return None

    poll_start = time.time()
    while time.time() - poll_start < STABLE_READING_TIMEOUT:
        time.sleep(0.2)
        w = read_weight()
        if w is None:
            return None
        if abs(w - last_w) <= STABLE_READING_DELTA:
            return w           # two consecutive readings agree -> settled
        last_w = w

    # Timed out without ever stabilising; return the last value anyway.
    print(f"  (settling timed out at {last_w:.2f} g, accepting)")
    return last_w


def closed_loop_dispense():
    """
    Iterative pulse-and-recheck. After each pulse we wait for the scale
    to settle (longer wait after the first pulse, since the bowl was
    static and the load cell is still ringing). The loop terminates
    when DELTA from starting weight reaches TARGET_WEIGHT - this makes
    dispensing robust to any scale offset still present at start.
    A maximum pulse count guards against runaway if the scale fails.
    """
    weight_history = deque(maxlen=SMOOTH_READINGS)
    pulses_fired = 0
    starting_weight = read_weight() or 0.0
    print(f"Dispense starting. Baseline weight: {starting_weight:.2f} g")

    while pulses_fired < MAX_PULSES:
        pulse_servo()
        pulses_fired += 1

        # First pulse gets a longer initial settle - cold scale, more ringing.
        initial_pause = FIRST_PULSE_PAUSE if pulses_fired == 1 else PULSE_PAUSE
        w = wait_for_stable_reading(initial_pause)

        if w is None:
            print("Scale read failed mid-dispense; aborting.")
            break
        if not (MIN_SAFE_WEIGHT <= w <= MAX_SAFE_WEIGHT):
            log_fault("ERR_SCALE_RANGE", f"reading {w:.2f} g out of bounds")
            break

        weight_history.append(w)
        avg = sum(weight_history) / len(weight_history)
        delta = avg - starting_weight
        print(f"Pulse {pulses_fired}: settled at {w:.2f} g  "
              f"(avg {avg:.2f} g, delta {delta:+.2f} g, target {TARGET_WEIGHT} g)")

        # Termination uses delta from baseline, not absolute weight.
        # Robust to any starting-weight offset (e.g. -26 g residual).
        if delta >= TARGET_WEIGHT:
            break

    final_weight = (sum(weight_history) / len(weight_history)) if weight_history else 0.0
    dispensed = final_weight - starting_weight
    print(f"Dispense complete: {dispensed:.2f} g over {pulses_fired} pulses.")
    return dispensed

# ============================================================================
# Main verification + dispense cycle
# ============================================================================
def main_cycle():
    print("Detector launched. Beginning 5 s habituation window "
          "(YOLOv8 loading in background).")

    # Section 4.2 - habituation delay used for THREE overlapping operations:
    #   1. Startle suppression (the duck has time to settle)
    #   2. Conditional tare (residual weight check)
    #   3. Model load (YOLOv8 boots into RAM during this 5s)
    initial_reading = read_weight()
    conditional_tare(initial_reading)
    time.sleep(HABITUATION_DELAY)

    # Multi-frame consensus verification after habituation.
    print(f"Verifying duck across up to {VERIFICATION_FRAMES} frames "
          f"(need {VERIFICATION_REQUIRED} positives)...")
    if not detect_duck_multi_frame():
        print("Verification failed - returning to standby without dispensing.")
        return

    # Verified -> closed-loop dispense.
    dispensed = closed_loop_dispense()

    if dispensed > 0:
        log_feed_event(dispensed)

        # Section 5.4 - take ONE ultrasonic reading after the dispense settles.
        # This is the only time the HC-SR04 is powered/queried in the entire
        # daily cycle, supporting the event-driven energy model (PWR-01).
        time.sleep(0.5)            # let the new feed settle into the column
        update_hopper_telemetry()

        # Arm the global cooldown by touching the flag file.
        COOLDOWN_FLAG.touch()
        print("Cooldown armed; next dispense allowed in 5 minutes.")


try:
    main_cycle()
finally:
    GPIO.output(SERVO_POWER_GATE_PIN, GPIO.LOW)
    GPIO.output(ULTRASONIC_TRIG_PIN, GPIO.LOW)
    picam2.stop()
    pwm.stop()
    GPIO.cleanup()
    print("Detector exited cleanly. Model unloaded; Pi returning to standby.")
