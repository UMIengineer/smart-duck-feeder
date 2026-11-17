"""
Smart Duck Feeder - Flask Dashboard (v3)
Adds event-driven hopper telemetry display (section 5.4):
  - hopper_status table now includes timestamp and distance_cm columns
  - Dashboard reads MOST RECENT row, representing the last post-dispense reading
  - 'Last measured' time shown so operator knows the data freshness
"""

from flask import Flask, render_template, jsonify
import sqlite3
import subprocess
from datetime import datetime

app = Flask(__name__)
DB_NAME = "/home/raspberrypi/Desktop/Final/duck_feeder.db"


# ----------------------------------------------------------------------------
# Schema bootstrap - now including timestamp and distance_cm in hopper_status.
# ----------------------------------------------------------------------------
def init_schema():
    conn = sqlite3.connect(DB_NAME)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS feed_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            dispensed_amount REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hopper_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            current_level REAL NOT NULL,
            max_capacity REAL NOT NULL DEFAULT 21000,
            distance_cm REAL
        );
        CREATE TABLE IF NOT EXISTS fault_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            error_code TEXT NOT NULL,
            detail TEXT
        );
    """)
    conn.commit()
    conn.close()


def migrate_hopper_schema():
    """
    Add the new timestamp and distance_cm columns to hopper_status if the
    user's existing database predates v3. ALTER TABLE ADD COLUMN is safe
    because SQLite ignores it for already-existing columns when wrapped
    in a try/except.
    """
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE hopper_status ADD COLUMN timestamp TEXT")
    except sqlite3.OperationalError:
        pass        # column already exists
    try:
        cur.execute("ALTER TABLE hopper_status ADD COLUMN distance_cm REAL")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


init_schema()
migrate_hopper_schema()


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def get_battery_level():
    """Query PiSugar local server (section 4.4) for current charge percent."""
    try:
        output = subprocess.getoutput('echo "get battery" | nc -q 0 127.0.0.1 8423')
        if "battery:" in output:
            return float(output.split(':')[1].strip())
        return None
    except Exception:
        return None


def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def get_active_fault():
    """Return the most recent fault, or None if system is healthy."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT error_code, detail, timestamp FROM fault_log "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {"code": row["error_code"], "detail": row["detail"], "timestamp": row["timestamp"]}


def humanise_age(iso_timestamp):
    """Convert an ISO timestamp into a human phrase like '12 minutes ago'."""
    if not iso_timestamp:
        return "never"
    try:
        ts = datetime.fromisoformat(iso_timestamp)
        delta = datetime.now() - ts
        seconds = delta.total_seconds()
        if seconds < 60:
            return f"{int(seconds)} seconds ago"
        if seconds < 3600:
            return f"{int(seconds / 60)} minutes ago"
        if seconds < 86400:
            return f"{int(seconds / 3600)} hours ago"
        return f"{int(seconds / 86400)} days ago"
    except (ValueError, TypeError):
        return "unknown"


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.route('/')
def dashboard():
    conn = get_db_connection()

    # Section 5.4 - read the MOST RECENT hopper measurement (last dispense event).
    hopper = conn.execute(
        'SELECT * FROM hopper_status ORDER BY id DESC LIMIT 1'
    ).fetchone()

    avg_data = conn.execute('''
        SELECT AVG(daily_total) FROM (
            SELECT SUM(dispensed_amount) AS daily_total
            FROM feed_events
            WHERE timestamp >= date('now', '-7 days')
            GROUP BY date(timestamp)
        )
    ''').fetchone()[0]
    conn.close()

    battery = get_battery_level()
    current_level = hopper['current_level'] if hopper else 0
    max_cap = hopper['max_capacity'] if hopper else 21000
    percentage = round((current_level / max_cap) * 100, 1) if max_cap > 0 else 0
    avg_daily = avg_data if avg_data else 0
    days_left = round(current_level / avg_daily, 1) if avg_daily > 0 else "N/A"

    # Section 5.4 - 'last measured' freshness indicator.
    last_measured_iso = hopper['timestamp'] if hopper else None
    last_measured_human = humanise_age(last_measured_iso)
    distance_cm = hopper['distance_cm'] if hopper and 'distance_cm' in hopper.keys() else None

    fault = get_active_fault()
    system_status = "Hardware Fault" if fault else "Online"

    return render_template(
        'index.html',
        battery=battery if battery is not None else "Error",
        hopper_level=current_level,
        hopper_percent=percentage,
        days_left=days_left,
        last_measured=last_measured_human,
        distance_cm=distance_cm,
        system_status=system_status,
        fault=fault,
    )


@app.route('/api/chart-data')
def chart_data():
    conn = get_db_connection()
    data = conn.execute('''
        SELECT date(timestamp) AS day, SUM(dispensed_amount) AS total
        FROM feed_events
        GROUP BY day
        ORDER BY day ASC
        LIMIT 7
    ''').fetchall()
    conn.close()
    return jsonify({
        "labels": [row['day'] for row in data],
        "totals": [row['total'] for row in data],
    })


@app.route('/api/fault-log')
def fault_log():
    """Expose last 20 faults for the diagnostics view (section 4.4.1)."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT timestamp, error_code, detail FROM fault_log "
        "ORDER BY id DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/refill', methods=['POST'])
def refill_hopper():
    """
    Operator clicks Refill after physically refilling the hopper.
    This writes a fresh hopper_status row marking the hopper as full;
    the next dispense will then overwrite it with an actual measurement.
    """
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO hopper_status (timestamp, current_level, max_capacity, distance_cm) "
        "VALUES (?, ?, ?, ?)",
        (datetime.now().isoformat(), 21000, 21000, None),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "message": "Hopper marked as full."})


@app.route('/calibrate', methods=['POST'])
def calibrate_hopper():
    """
    Section 4.4 - 'CALIBRATE' button. Triggers an ultrasonic re-measurement.
    Touches a flag file that the next detector run will pick up to force a
    re-measurement even without a feeding event.
    """
    flag_path = "/tmp/duck_feeder_calibrate.flag"
    try:
        with open(flag_path, "w") as f:
            f.write("1")
        return jsonify({"status": "success", "message": "Calibration requested."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    # debug=False for deployment; True only during bench testing.
    app.run(host='0.0.0.0', port=5000, debug=False)
