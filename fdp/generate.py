"""Simulate the client's source systems, including the mess real systems produce.

Sources (what a forward deployed engineer actually gets handed):
  * TMS     daily CSV exports of shipments. A shipment re-appears every time its
            status changes; each transporter's export formats dates differently;
            cities are spelled five ways; weights come in kg, MT or bare numbers.
  * GPS     telematics pings as gzipped JSON lines per day in object storage,
            with duplicates, (0,0) fixes, speed spikes and out-of-order rows.
  * ERP     invoices in the client's database (erp schema, standing in for SQL
            Server), revised in place, so extraction must be incremental + upsert.
  * Rates   contract rate card CSV, with a mid-period fuel revision.

Planted findings the analysis must recover (see tests/test_findings.py):
  * one transporter overbills ~30% of its invoices by 4-12%
  * the Hyderabad -> Mumbai lane is chronically late
  * rates rose 6% on 1 July 2026, so joining invoices to the *latest* rate
    instead of the rate *effective at dispatch* hides June overbilling
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .db import bootstrap, transaction
from .storage import ObjectStore, open_store

CITIES = {
    "Hyderabad": (17.385, 78.4867, "36"),
    "Chennai": (13.0827, 80.2707, "33"),
    "Bangalore": (12.9716, 77.5946, "29"),
    "Mumbai": (19.076, 72.8777, "27"),
    "Pune": (18.5204, 73.8567, "27"),
    "Vijayawada": (16.5062, 80.648, "37"),
    "Visakhapatnam": (17.6868, 83.2185, "37"),
    "Nagpur": (21.1458, 79.0882, "27"),
}
ALIASES = {
    "Hyderabad": ["Hyderabad", "HYDERABAD", "Hyd", "Secunderabad", " hyderabad "],
    "Chennai": ["Chennai", "CHENNAI", "Madras", "chennai"],
    "Bangalore": ["Bangalore", "Bengaluru", "BLR", "bangalore "],
    "Mumbai": ["Mumbai", "Bombay", "MUMBAI", "Navi Mumbai"],
    "Pune": ["Pune", "PUNE", "Poona"],
    "Vijayawada": ["Vijayawada", "Bezawada", "VIJAYAWADA"],
    "Visakhapatnam": ["Visakhapatnam", "Vizag", "VSKP"],
    "Nagpur": ["Nagpur", "NAGPUR"],
}
LANES = [
    ("Hyderabad", "Chennai"),
    ("Hyderabad", "Bangalore"),
    ("Hyderabad", "Mumbai"),
    ("Hyderabad", "Pune"),
    ("Hyderabad", "Vijayawada"),
    ("Hyderabad", "Visakhapatnam"),
    ("Hyderabad", "Nagpur"),
    ("Chennai", "Hyderabad"),
    ("Bangalore", "Hyderabad"),
    ("Mumbai", "Hyderabad"),
]
TRANSPORTERS = {
    # name: (state, date format in their TMS export, overbill probability, punctuality factor)
    "Sri Balaji Roadways": ("36", "%d/%m/%Y %H:%M", 0.02, 1.00),
    "Godavari Freight Carriers": ("37", "%Y-%m-%d %H:%M:%S", 0.30, 1.05),
    "Deccan Cargo Movers": ("36", "%d-%b-%Y %I:%M %p", 0.02, 0.95),
    "Krishna Transport Co": ("37", "%d/%m/%Y %H:%M", 0.03, 1.10),
    "Western Ghats Logistics": ("27", "%Y-%m-%dT%H:%M:%S", 0.02, 1.00),
}
GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ROAD_FACTOR = 1.25  # road km per straight-line km
FUEL_REVISION = date(2026, 7, 1)


def gstin(rng: random.Random, state: str) -> str:
    body = state + "".join(rng.choices("ABCDEFGHIJKLMNOPQRSTUVWXYZ", k=5)) + "".join(rng.choices("0123456789", k=4))
    body += rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + rng.choice("123456789") + "Z"
    total = 0
    for i, ch in enumerate(body):
        p = GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += p // 36 + p % 36
    return body + GSTIN_CHARS[(36 - total % 36) % 36]


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


def road_km(o: str, d: str) -> float:
    return round(haversine_km(CITIES[o][:2], CITIES[d][:2]) * ROAD_FACTOR, 1)


def base_rate(o: str, d: str, cap: int) -> int:
    km = road_km(o, d)
    per_km = 52 if cap == 10 else 78
    return int(round((4000 + km * per_km) / 500) * 500)


def rate_on(o: str, d: str, cap: int, day: date) -> int:
    rate = base_rate(o, d, cap)
    return int(round(rate * 1.06 / 100) * 100) if day >= FUEL_REVISION else rate


@dataclass
class Shipment:
    shipment_id: str
    transporter: str
    origin: str
    destination: str
    vehicle_no: str
    weight_kg: int
    capacity_t: int
    booked_at: datetime
    dispatched_at: datetime
    promised_at: datetime
    delivered_at: datetime
    stops: list[tuple[float, float]]  # (km into trip, minutes stopped)


def _plate(rng: random.Random, state: str) -> str:
    prefix = {"36": "TS", "37": "AP", "27": "MH", "33": "TN", "29": "KA"}[state]
    return f"{prefix}{rng.randint(1, 40):02d}{''.join(rng.choices('ABCEHKLMNPRTUVWXYZ', k=2))}{rng.randint(1000, 9999)}"


def build_shipments(n: int, start: date, days: int, rng: random.Random) -> list[Shipment]:
    # A truck is on one trip at a time, otherwise GPS pings could not be attributed to a shipment.
    free_at: dict[str, dict[str, datetime]] = {t: {} for t in TRANSPORTERS}
    out = []
    for i in range(n):
        t = rng.choice(list(TRANSPORTERS))
        o, d = rng.choice(LANES)
        cap = rng.choice([10, 20])
        booked = datetime.combine(start + timedelta(days=rng.randrange(days)), datetime.min.time()) + timedelta(
            hours=rng.randint(6, 20), minutes=rng.randint(0, 59)
        )
        dispatched = booked + timedelta(hours=rng.uniform(3, 30))
        km = road_km(o, d)
        planned_h = km / 38 + 4  # 38 km/h door-to-door plus a buffer: what the TMS promises
        promised = dispatched + timedelta(hours=planned_h)
        # Reality: speed varies by transporter; the Mumbai lane has ghat sections and checkpost queues.
        speed = rng.gauss(44, 6) / TRANSPORTERS[t][3]
        if d == "Mumbai" or o == "Mumbai":
            speed *= 0.72
        stops = [(rng.uniform(0.1, 0.9) * km, rng.uniform(20, 70)) for _ in range(rng.randint(1, 3))]
        if rng.random() < 0.08:  # breakdown / detention
            stops.append((rng.uniform(0.2, 0.8) * km, rng.uniform(180, 600)))
        stops.sort()
        drive_h = km / max(speed, 18)
        delivered = dispatched + timedelta(hours=drive_h + sum(m for _, m in stops) / 60)
        out.append(
            Shipment(
                f"SHP{100000 + i}",
                t,
                o,
                d,
                "",
                int(round(rng.uniform(0.55, 0.97) * cap * 1000, -1)),
                cap,
                booked,
                dispatched,
                promised,
                delivered,
                stops,
            )
        )
    # Assign trucks in dispatch order (greedy interval scheduling): reuse a free truck, else add one.
    for s in sorted(out, key=lambda s: s.dispatched_at):
        fleet = free_at[s.transporter]
        s.vehicle_no = next((v for v, at in sorted(fleet.items()) if at <= s.dispatched_at), None) or _plate(
            rng, TRANSPORTERS[s.transporter][0]
        )
        fleet[s.vehicle_no] = s.delivered_at + timedelta(hours=2)
    return out


# ---------------------------------------------------------------- TMS exports (CSV per day)


def _fmt_weight(rng: random.Random, kg: int) -> str:
    return rng.choice([f"{kg}", f"{kg} kg", f"{kg / 1000:.2f} MT", f"{kg:,} KGS"])


def _fmt_plate(rng: random.Random, plate: str) -> str:
    p = rng.choice(
        [plate, plate.lower(), f"{plate[:2]} {plate[2:4]} {plate[4:-4]} {plate[-4:]}", f"{plate[:2]}-{plate[2:4]}-{plate[4:]}"]
    )
    return p if rng.random() > 0.05 else f" {p} "


TMS_COLUMNS = [
    "shipment_id",
    "booked_at",
    "dispatched_at",
    "delivered_at",
    "promised_at",
    "origin",
    "destination",
    "transporter",
    "vehicle_no",
    "weight",
    "status",
    "updated_at",
]


def tms_events(shipments: list[Shipment], rng: random.Random) -> dict[date, list[dict]]:
    """Each status change emits a full snapshot row in that day's export."""
    by_day: dict[date, list[dict]] = {}
    for s in shipments:
        fmt = TRANSPORTERS[s.transporter][1]

        def f(dt: datetime | None, fmt: str = fmt) -> str:
            return dt.strftime(fmt) if dt else ""

        origin, dest = rng.choice(ALIASES[s.origin]), rng.choice(ALIASES[s.destination])
        if rng.random() < 0.004:
            dest = ""  # broken record: staging must quarantine it, not drop it silently
        base = {
            "shipment_id": s.shipment_id,
            "booked_at": f(s.booked_at),
            "promised_at": f(s.promised_at),
            "origin": origin,
            "destination": dest,
            "transporter": s.transporter,
            "vehicle_no": _fmt_plate(rng, s.vehicle_no),
            "weight": _fmt_weight(rng, s.weight_kg),
        }
        for status, at in (("BOOKED", s.booked_at), ("IN_TRANSIT", s.dispatched_at), ("DELIVERED", s.delivered_at)):
            row = {
                **base,
                "status": status,
                "updated_at": f(at),
                "dispatched_at": f(s.dispatched_at) if status != "BOOKED" else "",
                "delivered_at": f(s.delivered_at) if status == "DELIVERED" else "",
            }
            by_day.setdefault(at.date(), []).append(row)
            if rng.random() < 0.005:
                by_day[at.date()].append(dict(row))  # exact duplicate, as exports do
    return by_day


def write_tms(store: ObjectStore, by_day: dict[date, list[dict]]) -> int:
    for day, rows in by_day.items():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=TMS_COLUMNS)
        w.writeheader()
        w.writerows(rows)
        store.put(f"tms/dt={day.isoformat()}/shipments.csv", buf.getvalue().encode())
    return sum(map(len, by_day.values()))


# ---------------------------------------------------------------- GPS pings (JSONL.gz per day)


def _position(o: str, d: str, frac: float) -> tuple[float, float]:
    (la1, lo1, _), (la2, lo2, _) = CITIES[o], CITIES[d]
    wiggle = math.sin(frac * math.pi * 3) * 0.08  # roads are not straight lines
    return la1 + (la2 - la1) * frac + wiggle, lo1 + (lo2 - lo1) * frac - wiggle


def gps_pings(shipments: list[Shipment], interval_min: int, rng: random.Random) -> dict[date, list[dict]]:
    by_day: dict[date, list[dict]] = {}
    for s in shipments:
        km = road_km(s.origin, s.destination)
        total_min = (s.delivered_at - s.dispatched_at).total_seconds() / 60
        stop_min = sum(m for _, m in s.stops)
        speed = km / max((total_min - stop_min) / 60, 0.1)
        t, dist, stop_i, stopped_for = s.dispatched_at, 0.0, 0, 0.0
        while t <= s.delivered_at:
            in_stop = stop_i < len(s.stops) and dist >= s.stops[stop_i][0]
            if in_stop:
                v = 0.0
                stopped_for += interval_min
                if stopped_for >= s.stops[stop_i][1]:
                    stop_i, stopped_for = stop_i + 1, 0.0
            else:
                v = max(0.0, rng.gauss(speed, 6))
                dist = min(km, dist + v * interval_min / 60)
            lat, lon = _position(s.origin, s.destination, dist / km)
            ping = {
                "vehicle_no": s.vehicle_no,
                "ts": t.isoformat(timespec="seconds"),
                "lat": round(lat + rng.gauss(0, 0.0004), 5),
                "lon": round(lon + rng.gauss(0, 0.0004), 5),
                "speed_kmph": round(v, 1),
            }
            r = rng.random()
            if r < 0.002:
                ping.update(lat=0.0, lon=0.0)  # cold-start GPS fix
            elif r < 0.003:
                ping["speed_kmph"] = round(rng.uniform(250, 400), 1)  # sensor spike
            day_rows = by_day.setdefault(t.date(), [])
            day_rows.append(ping)
            if rng.random() < 0.001:
                day_rows.append(dict(ping))  # duplicate delivery from the telematics vendor
            t += timedelta(minutes=interval_min)
    for rows in by_day.values():
        rng.shuffle(rows)  # files arrive in no particular order
    return by_day


def write_gps(store: ObjectStore, by_day: dict[date, list[dict]]) -> int:
    for day, rows in by_day.items():
        body = "\n".join(json.dumps(r, separators=(",", ":")) for r in rows).encode()
        store.put(f"gps/dt={day.isoformat()}/pings.jsonl.gz", gzip.compress(body, mtime=0))
    return sum(map(len, by_day.values()))


# ---------------------------------------------------------------- rate cards


def write_rate_cards(store: ObjectStore) -> int:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["origin", "destination", "capacity_t", "rate_per_trip", "effective_from"])
    n = 0
    for o, d in LANES:
        for cap in (10, 20):
            w.writerow([o, d, cap, base_rate(o, d, cap), "2026-04-01"])
            w.writerow([o, d, cap, rate_on(o, d, cap, FUEL_REVISION), FUEL_REVISION.isoformat()])
            n += 2
    store.put("rates/rate_card_2026.csv", buf.getvalue().encode())
    return n


# ---------------------------------------------------------------- ERP invoices (source database)

BUYER_GSTINS: dict[str, str] = {}


def erp_invoices(shipments: list[Shipment], rng: random.Random) -> list[dict]:
    transporter_gstin = {t: gstin(rng, st) for t, (st, *_r) in TRANSPORTERS.items()}
    for _lat, _lon, st in CITIES.values():
        BUYER_GSTINS.setdefault(st, gstin(rng, st))
    rows = []
    for s in shipments:
        if rng.random() < 0.02:
            continue  # delivered but never invoiced: a revenue leak the marts should surface
        inv_date = (s.delivered_at + timedelta(days=rng.randint(0, 4))).date()
        rate = rate_on(s.origin, s.destination, s.capacity_t, s.dispatched_at.date())
        freight = float(rate)
        if rng.random() < TRANSPORTERS[s.transporter][2]:
            freight = round(rate * rng.uniform(1.04, 1.12), -1)  # padded just enough to look plausible
        other = 1500.0 if rng.random() < 0.3 else 0.0
        taxable = freight + other
        seller = transporter_gstin[s.transporter]
        buyer = BUYER_GSTINS[CITIES[s.destination][2]]
        if seller[:2] == buyer[:2]:
            cgst = sgst = round(taxable * 0.06, 2)
            igst = 0.0
        else:
            cgst = sgst = 0.0
            igst = round(taxable * 0.12, 2)
        total = round(taxable + cgst + sgst + igst, 2)
        if rng.random() < 0.01:
            total += rng.choice([100.0, 1000.0, -250.0])  # arithmetic error on the paper
        updated = datetime.combine(inv_date, datetime.min.time()) + timedelta(hours=rng.randint(9, 19))
        rows.append(
            {
                "invoice_id": f"INV-{s.shipment_id[3:]}",
                "shipment_id": s.shipment_id,
                "transporter": s.transporter,
                "seller_gstin": seller,
                "buyer_gstin": buyer,
                "invoice_date": inv_date,
                "freight_amount": freight,
                "other_charges": other,
                "taxable_value": taxable,
                "cgst": cgst,
                "sgst": sgst,
                "igst": igst,
                "total_amount": total,
                "updated_at": updated,
            }
        )
    for _ in range(max(1, len(rows) // 150)):  # invoices for shipments the TMS never saw
        ghost = dict(rng.choice(rows))
        ghost.update(invoice_id=f"INV-X{rng.randint(10000, 99999)}", shipment_id=f"SHP{rng.randint(900000, 999999)}")
        rows.append(ghost)
    return rows


def insert_erp(rows: list[dict]) -> None:
    cols = list(rows[0])
    with transaction() as conn, conn.cursor() as cur:
        cur.execute("truncate erp.invoices")
        with cur.copy(f"copy erp.invoices ({', '.join(cols)}) from stdin") as cp:
            for r in rows:
                cp.write_row([r[c] for c in cols])


def revise_erp_invoices(fraction: float = 0.05, seed: int = 3) -> int:
    """Simulate the ERP correcting invoices after the fact (extra charges billed, invoice re-issued)."""
    with transaction() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "select invoice_id from erp.invoices where abs(hashtext(invoice_id || %s::text)) %% 1000 < %s",
                (str(seed), int(fraction * 1000)),
            )
        ]
        conn.execute(
            """
            update erp.invoices
               set other_charges = other_charges + 500,
                   taxable_value = taxable_value + 500,
                   igst = case when igst > 0 then round((taxable_value + 500) * 0.12, 2) else 0 end,
                   cgst = case when cgst > 0 then round((taxable_value + 500) * 0.06, 2) else 0 end,
                   sgst = case when sgst > 0 then round((taxable_value + 500) * 0.06, 2) else 0 end,
                   updated_at = now()
             where invoice_id = any(%s)
            """,
            (ids,),
        )
        conn.execute(
            "update erp.invoices set total_amount = taxable_value + cgst + sgst + igst where invoice_id = any(%s)", (ids,)
        )
    return len(ids)


def generate(
    shipments: int = 1200,
    start: date = date(2026, 6, 1),
    days: int = 92,
    ping_minutes: int = 2,
    seed: int = 2026,
    store: ObjectStore | None = None,
) -> dict:
    rng = random.Random(seed)
    store = store or open_store()
    bootstrap()
    ships = build_shipments(shipments, start, days, rng)
    stats = {
        "shipments": len(ships),
        "tms_rows": write_tms(store, tms_events(ships, rng)),
        "gps_pings": write_gps(store, gps_pings(ships, ping_minutes, rng)),
        "rate_rows": write_rate_cards(store),
    }
    invoices = erp_invoices(ships, rng)
    insert_erp(invoices)
    stats["erp_invoices"] = len(invoices)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate source-system data")
    ap.add_argument("--shipments", type=int, default=1200)
    ap.add_argument("--days", type=int, default=92)
    ap.add_argument("--ping-minutes", type=int, default=2)
    ap.add_argument("--seed", type=int, default=2026)
    a = ap.parse_args()
    print(json.dumps(generate(a.shipments, days=a.days, ping_minutes=a.ping_minutes, seed=a.seed), indent=2))


if __name__ == "__main__":
    main()
