import json, glob, io, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

paths = (sorted(glob.glob("debug/otp-hkd-2026_2026-10-29_2026-11-15*.json")) +
         sorted(glob.glob("debug/otp-hkd-2026_2026-10-30_2026-11-15*.json")) +
         sorted(glob.glob("debug/otp-hkd-2026_2026-11-05_2026-11-20*.json")))

for path in paths:
    data = json.load(open(path, encoding="utf-8"))
    print(f"\n=== {path}  ({len(data)} pairs) ===")
    seen = {}
    for pair in data:
        outbound = pair[0]
        legs = outbound.get("legs", [])
        if not legs:
            continue
        al = legs[0]["airline"]
        al = al["name"] if isinstance(al, dict) else al
        key = (legs[0]["departure_datetime"][:16], al, outbound.get("price"))
        seen[key] = seen.get(key, 0) + 1
    for k, v in seen.items():
        print(f"  outbound dep={k[0]}  airline={k[1]}  price={k[2]}   -> used in {v} pairs")
