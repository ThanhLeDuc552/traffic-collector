import glob
import pandas as pd

files = sorted(glob.glob("data/traffic_*.csv"))
if not files:
    raise SystemExit("No data files found in data/")

df = pd.concat((pd.read_csv(f, parse_dates=["timestamp"]) for f in files), ignore_index=True)

# Drop unreliable / incomplete readings
df = df.dropna(subset=["current_speed_kmh", "free_flow_speed_kmh"])
df = df[df["confidence"] >= 0.6]

df["day_type"] = df["timestamp"].dt.dayofweek.map(lambda d: "weekend" if d >= 5 else "weekday")

usual = (
    df.groupby(["segment_id", "day_type", "period"])
      .agg(
          n=("current_speed_kmh", "size"),
          median_speed=("current_speed_kmh", "median"),
          p25_speed=("current_speed_kmh", lambda s: s.quantile(0.25)),
          p75_speed=("current_speed_kmh", lambda s: s.quantile(0.75)),
          free_flow=("free_flow_speed_kmh", "median"),
          median_congestion=("congestion_rate_percent", "median"),
      )
      .reset_index()
)
usual["speed_ratio"] = usual["median_speed"] / usual["free_flow"]

usual.to_csv("usual_traffic.csv", index=False)
print(usual.to_string(index=False))
