import os
import sys
import csv
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import networkx as nx

# --- CONFIG ---
API_KEY = os.environ.get("TOMTOM_API_KEY")  # set as a GitHub secret / env var, never hard-code
GRAPHML_FILENAME = "hai_chau_road_graph.graphml"
TARGET_CLASSES = ["primary", "secondary", "tertiary", "primary_link", "trunk"]

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
MAX_SEGMENTS = int(os.environ.get("MAX_SEGMENTS", "30"))  # 0 = all segments
FORCE_RUN = os.environ.get("FORCE_RUN") == "1"            # ignore the interval check
REQUEST_DELAY_S = 0.2

# Minimum minutes between collections for each period
INTERVAL_MIN = {"peak": 10, "off_peak": 30, "late_night": 60}
TOLERANCE_MIN = 2  # allows for scheduler delay

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GRAPHML_PATH = os.path.join(BASE_DIR, GRAPHML_FILENAME)
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_PATH = os.path.join(DATA_DIR, "last_run.txt")

FIELDS = [
    "timestamp", "period", "segment_id", "road_type", "mid_lat", "mid_lon",
    "current_speed_kmh", "free_flow_speed_kmh", "congestion_rate_percent", "confidence",
]


class FatalAPIError(Exception):
    """Auth / quota problems: stop the run instead of retrying every segment."""


def get_period(now):
    h = now.hour + now.minute / 60
    if 6.5 <= h < 8.5 or 16.5 <= h < 19:
        return "peak"
    if h >= 23 or h < 4:
        return "late_night"
    return "off_peak"


def get_traffic(lat, lon):
    url = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json"
    params = {"point": f"{lat},{lon}", "unit": "KMPH", "key": API_KEY}
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code in (401, 403, 429):
            raise FatalAPIError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        resp.raise_for_status()
        return resp.json().get("flowSegmentData")
    except requests.RequestException as e:
        print(f" [Request error]: {e}")
    except ValueError:
        print(" [Invalid JSON in response]")
    return None


def due_to_run(now):
    """Skip if the last successful run was too recent for the current period."""
    if FORCE_RUN:
        return True
    try:
        with open(STATE_PATH) as f:
            last = datetime.fromisoformat(f.read().strip())
    except (FileNotFoundError, ValueError):
        return True
    elapsed_min = (now - last).total_seconds() / 60
    return elapsed_min >= INTERVAL_MIN[get_period(now)] - TOLERANCE_MIN


def load_segments():
    G = nx.read_graphml(GRAPHML_PATH)
    seen, segments = set(), []
    for u, v, data in G.edges(data=True):
        r_type = str(data.get("road_class") or data.get("highway") or "")
        if not any(tc in r_type for tc in TARGET_CLASSES):
            continue
        try:
            lat_u, lon_u = float(G.nodes[u]["y"]), float(G.nodes[u]["x"])
            lat_v, lon_v = float(G.nodes[v]["y"]), float(G.nodes[v]["x"])
        except (KeyError, TypeError, ValueError):
            continue
        mid_lat = round((lat_u + lat_v) / 2, 6)
        mid_lon = round((lon_u + lon_v) / 2, 6)
        key = (round(mid_lat, 4), round(mid_lon, 4))  # drops reverse-direction duplicates
        if key in seen:
            continue
        seen.add(key)
        segments.append({
            "segment_id": data.get("segment_id", f"{u}_{v}"),
            "road_type": r_type,
            "mid_lat": mid_lat,
            "mid_lon": mid_lon,
        })

    # Evenly spaced subset so the same segments are used on every run
    if MAX_SEGMENTS > 0 and len(segments) > MAX_SEGMENTS:
        step = len(segments) / MAX_SEGMENTS
        segments = [segments[int(i * step)] for i in range(MAX_SEGMENTS)]
    return segments


def append_rows(rows, day):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"traffic_{day}.csv")
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerows(rows)
    return path


def main():
    if not API_KEY:
        sys.exit("Missing TOMTOM_API_KEY environment variable.")
    if not os.path.exists(GRAPHML_PATH):
        sys.exit(f"Graph file not found: {GRAPHML_PATH}")

    start = datetime.now(TZ)
    period = get_period(start)
    if not due_to_run(start):
        print(f"Skipping: not due yet for period '{period}'.")
        return

    segments = load_segments()
    print(f"{start.isoformat()} | period={period} | {len(segments)} segments")

    rows, failed, fatal = [], 0, None
    for seg in segments:
        try:
            flow = get_traffic(seg["mid_lat"], seg["mid_lon"])
        except FatalAPIError as e:
            fatal = e
            break
        ts = datetime.now(TZ)  # time of this request

        if flow is None:
            failed += 1
        else:
            cur = flow.get("currentSpeed")
            free = flow.get("freeFlowSpeed")
            congestion = None
            if cur is not None and free:
                congestion = max(0.0, round((1 - cur / free) * 100, 2))
            rows.append({
                "timestamp": ts.isoformat(),
                "period": get_period(ts),
                **seg,
                "current_speed_kmh": cur,
                "free_flow_speed_kmh": free,
                "congestion_rate_percent": congestion,
                "confidence": flow.get("confidence"),
            })
        time.sleep(REQUEST_DELAY_S)

    if rows:
        path = append_rows(rows, start.strftime("%Y-%m-%d"))
        with open(STATE_PATH, "w") as f:
            f.write(start.isoformat())
        print(f"Saved {len(rows)} rows ({failed} failed) to {path}")
    else:
        print(f"No rows collected ({failed} failed).")

    if fatal:
        sys.exit(f"Stopped early: {fatal}")


if __name__ == "__main__":
    main()
