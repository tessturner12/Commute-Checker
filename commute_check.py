#!/usr/bin/env python3
"""
TfL Commute Check
Checks journey options from W12 9DX to N5 2EF, arriving by 08:45,
and emails a summary with disruption alerts.
"""

import os
import sys
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

# --- Config ---
FROM_POSTCODE = "W129DX"
TO_POSTCODE = "N52EF"
ARRIVAL_TIME = "0845"
MAX_JOURNEYS = 3
WALK_THRESHOLD_MINS = 15

TFL_APP_KEY = os.getenv("TFL_APP_KEY", "")
GMAIL_USER = os.getenv("GMAIL_USER", "tess.turner@quantum.media")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
RECIPIENT = os.getenv("RECIPIENT_EMAIL", GMAIL_USER)

MODE_ICONS = {
    "bus": "🚌",
    "tube": "🚇",
    "overground": "🚆",
    "elizabeth-line": "🟣",
    "walking": "🚶",
    "dlr": "🚈",
    "national-rail": "🚂",
}


_walk_cache = {}


def get_walking_time(lat, lon, label):
    """Return walking minutes from lat/lon to the destination. Cached by label."""
    if label in _walk_cache:
        return _walk_cache[label]

    from_coord = f"{lat},{lon}"
    url = f"https://api.tfl.gov.uk/Journey/JourneyResults/{from_coord}/to/{TO_POSTCODE}"
    params = {"mode": "walking", "maxJourneys": 1}
    if TFL_APP_KEY:
        params["app_key"] = TFL_APP_KEY

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        journeys = r.json().get("journeys", [])
        result = journeys[0].get("duration") if journeys else None
    except Exception:
        result = None

    _walk_cache[label] = result
    return result


def process_legs(legs):
    """
    Process journey legs:
    - Records first bus and overground departure times.
    - If a bus leg is within WALK_THRESHOLD_MINS of the destination, replaces it
      (and all subsequent legs) with a single synthetic walk leg.
    Returns (display_legs, key_times) where key_times is {"bus": "HH:MM", "overground": "HH:MM"}.
    """
    key_times = {}
    display_legs = []

    for leg in legs:
        mode_id = leg.get("mode", {}).get("id", "")
        dep_time = parse_time(leg.get("departureTime", ""))

        if mode_id == "bus" and "bus" not in key_times and dep_time != "?":
            key_times["bus"] = dep_time
        if mode_id == "overground" and "overground" not in key_times and dep_time != "?":
            key_times["overground"] = dep_time

        if mode_id == "bus":
            dep = leg.get("departurePoint", {})
            lat, lon = dep.get("lat"), dep.get("lon")
            dep_name = dep.get("commonName", "")
            if lat is not None and lon is not None:
                walk_mins = get_walking_time(lat, lon, dep_name)
                if walk_mins is not None and walk_mins < WALK_THRESHOLD_MINS:
                    # Drop the preceding walk-to-bus-stop leg to avoid two walks in a row
                    if display_legs and display_legs[-1].get("mode", {}).get("id") == "walking":
                        display_legs.pop()
                    display_legs.append({"_synthetic": True, "walk_mins": walk_mins})
                    break

        display_legs.append(leg)

    return display_legs, key_times


def get_journeys():
    url = f"https://api.tfl.gov.uk/Journey/JourneyResults/{FROM_POSTCODE}/to/{TO_POSTCODE}"
    params = {
        "time": ARRIVAL_TIME,
        "timeIs": "Arriving",
        "date": datetime.now().strftime("%Y%m%d"),
        "maxJourneys": MAX_JOURNEYS,
    }
    if TFL_APP_KEY:
        params["app_key"] = TFL_APP_KEY

    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("journeys", [])


def get_overground_journey():
    """Fetch the overground route via Acton Central to Highbury & Islington (walk + overground only)."""
    url = f"https://api.tfl.gov.uk/Journey/JourneyResults/{FROM_POSTCODE}/to/{TO_POSTCODE}"
    params = {
        "time": ARRIVAL_TIME,
        "timeIs": "Arriving",
        "date": datetime.now().strftime("%Y%m%d"),
        "maxJourneys": 3,
        "mode": "overground,walking",
    }
    if TFL_APP_KEY:
        params["app_key"] = TFL_APP_KEY
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        for j in r.json().get("journeys", []):
            legs = j.get("legs", [])
            if any(leg.get("mode", {}).get("id") == "overground" for leg in legs):
                # Relabel overground arrival to Highbury & Islington (same line, one stop further)
                for leg in legs:
                    if leg.get("mode", {}).get("id") == "overground":
                        if leg.get("instruction", {}).get("summary", ""):
                            leg["instruction"]["summary"] = leg["instruction"]["summary"].rsplit(" to ", 1)[0] + " to Highbury & Islington"
                return j
    except Exception:
        pass
    return None


def parse_time(dt_str):
    if not dt_str:
        return "?"
    try:
        return datetime.fromisoformat(dt_str).strftime("%H:%M")
    except ValueError:
        return dt_str[11:16]


def format_mode(mode_id):
    icon = MODE_ICONS.get(mode_id, "🔹")
    label = mode_id.replace("-", " ").title()
    return f"{icon} {label}"


def render_journey(lines, journey, label):
    dep = parse_time(journey.get("startDateTime", ""))
    arr = parse_time(journey.get("arrivalDateTime", ""))
    duration = journey.get("duration", "?")
    legs = journey.get("legs", [])

    display_legs, key_times = process_legs(legs)

    journey_disruptions = []
    leg_lines = []

    for leg in display_legs:
        if leg.get("_synthetic"):
            leg_lines.append(f"   {format_mode('walking')}: Walk to destination ({leg['walk_mins']} min)")
            continue
        mode_id = leg.get("mode", {}).get("id", "")
        instruction = leg.get("instruction", {}).get("summary", "")
        leg_disruptions = leg.get("disruptions", [])
        leg_line = f"   {format_mode(mode_id)}: {instruction}"
        if leg_disruptions:
            leg_line += "  ⚠️"
            for d in leg_disruptions:
                journey_disruptions.append(d.get("description", "Disruption reported"))
        leg_lines.append(leg_line)

    status = "⚠️  DISRUPTIONS" if journey_disruptions else "✅  Good service"
    lines.append(f"{label}  |  Leave {dep}  →  Arrive {arr}  ({duration} min)  |  {status}")

    time_parts = []
    if "bus" in key_times:
        time_parts.append(f"🚌 First bus {key_times['bus']}")
    if "overground" in key_times:
        time_parts.append(f"🚆 Overground {key_times['overground']}")
    if time_parts:
        lines.append(f"   {' | '.join(time_parts)}")

    lines.extend(leg_lines)

    if journey_disruptions:
        lines.append("")
        lines.append("   Disruption details:")
        for d in journey_disruptions:
            lines.append(f"     • {d}")

    lines.append("")


def build_email_body(journeys, overground_journey=None):
    today = datetime.now().strftime("%A %d %B %Y")
    lines = [
        f"TfL Commute Check — {today}",
        f"W12 9DX  →  N5 2EF  |  Target arrival: 08:45",
        "=" * 52,
        "",
    ]

    for i, journey in enumerate(journeys, 1):
        render_journey(lines, journey, f"Option {i}")

    if overground_journey:
        lines.append("─" * 52)
        lines.append("🚆 OVERGROUND OPTION")
        lines.append("")
        render_journey(lines, overground_journey, "Overground route")

    lines.append("=" * 52)
    lines.append("Sent by your TfL Commute Check script.")
    return "\n".join(lines)


def send_email(subject, body):
    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, RECIPIENT, msg.as_string())


def main():
    try:
        journeys = get_journeys()
        overground_journey = get_overground_journey()
    except requests.RequestException as e:
        print(f"TfL API error: {e}")
        if GMAIL_APP_PASSWORD:
            send_email("⚠️ TfL Check Failed", f"Could not reach TfL API:\n{e}")
        return

    if not journeys:
        print("No journey options returned from TfL.")
        return

    body = build_email_body(journeys, overground_journey)
    any_disruption = any(
        leg.get("disruptions")
        for j in journeys
        for leg in j.get("legs", [])
    )
    subject = (
        "⚠️ TfL Alert — Disruptions on your commute"
        if any_disruption
        else "✅ TfL Commute Check — All clear"
    )

    print(body)

    if GMAIL_APP_PASSWORD:
        send_email(subject, body)
        print("\nEmail sent to", RECIPIENT)
    else:
        print("\n[Email not sent — set GMAIL_APP_PASSWORD in .env to enable]")


if __name__ == "__main__":
    main()
