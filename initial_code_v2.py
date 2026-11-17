"""
Smart Duck Feeder - Wake Controller (v2)
PIR-driven interrupt loop that launches the duck detector subprocess.
Implements the 5-minute global cooldown specified in BIO-01 and section 4.1.
"""

import time
import subprocess
import os
from pathlib import Path
from gpiozero import MotionSensor

# --- Configuration ---
PIR_PIN = 17                        # BCM numbering (BOARD pin 11 = BCM pin 17)
NO_MOTION_TIMEOUT = 60              # seconds of no motion before killing detector
COOLDOWN_SECONDS = 300              # 5-minute cooldown after a successful dispense (BIO-01)
LONG_IDLE_THRESHOLD = 600    		# 10 min with no motion → enter deep idle
LONG_IDLE_SLEEP = 30         		# check every 30s in deep idle (vs 2s normal)
COOLDOWN_FLAG = Path("/tmp/duck_feeder_cooldown.flag")  # written by detector after dispense
DETECTOR_VENV = "/home/raspberrypi/Desktop/duck_inference_env/bin/python"
DETECTOR_SCRIPT = "/home/raspberrypi/Desktop/Final/duckdetector_v4.py"

print("Warming up PIR sensor (10 s baseline calibration)...")
# Initialize the PIR sensor (handles setup and cleanup automatically)
pir = MotionSensor(PIR_PIN)
time.sleep(10)
print("System armed. Listening for thermal motion.")

detector_process = None
last_motion_time = 0


def in_cooldown() -> bool:
    """Check whether a recent dispense has armed the global cooldown lock."""
    if not COOLDOWN_FLAG.exists():
        return False
    age = time.time() - COOLDOWN_FLAG.stat().st_mtime
    if age < COOLDOWN_SECONDS:
        return True
    # Cooldown expired - clean up the stale flag file.
    try:
        COOLDOWN_FLAG.unlink()
    except FileNotFoundError:
        pass
    return False


def on_motion_detected():
    """Hardware interrupt callback - wakes the detector subprocess."""
    global detector_process, last_motion_time
    last_motion_time = time.time()

    if in_cooldown():
        # Suppress detector launch during the 5-minute lockout.
        return

    if detector_process is None or detector_process.poll() is not None:
        print(f"[{time.strftime('%H:%M:%S')}] Motion detected -> launching detector.")
        detector_process = subprocess.Popen([DETECTOR_VENV, DETECTOR_SCRIPT])


# Register the hardware interrupt using gpiozero's event handler.
# This replaces the broken RPi.GPIO edge detection.
pir.when_motion = on_motion_detected

try:
    while True:
        # Background loop only handles detector cleanup; no polling of the PIR pin.
        if detector_process is not None and detector_process.poll() is None:
            time_since_last_motion = time.time() - last_motion_time
            if time_since_last_motion > NO_MOTION_TIMEOUT:
                print(f"No motion for {NO_MOTION_TIMEOUT} s -> stopping detector.")
                detector_process.terminate()
                detector_process.wait()
                detector_process = None
                print("Detector closed. Returning to interrupt-listen state.")

        # Long sleep - the loop is no longer responsible for motion detection,
        # so we don't need 100 ms granularity. This drops CPU draw substantially.
        time.sleep(2.0)

except KeyboardInterrupt:
    print("\nManual stop received.")
finally:
    if detector_process is not None and detector_process.poll() is None:
        detector_process.terminate()
        detector_process.wait()
    pir.close()
    print("GPIO cleaned up. Exited.")