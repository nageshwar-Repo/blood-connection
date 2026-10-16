import re
from datetime import date, timedelta
from math import asin, cos, isfinite, radians, sin, sqrt
from os import getenv
from threading import RLock
from time import time
from uuid import uuid4

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

blood_stock = {
    "A+": 10,
    "B+": 15,
    "O+": 8,
    "AB+": 4,
    "A-": 2,
    "B-": 5,
    "O-": 3,
    "AB-": 1,
}

blood_bank = {
    "name": "LifePulse Central Blood Bank",
    "lat": 28.6315,
    "lng": 77.2167,
}
orders = {}
state_lock = RLock()
PREPARATION_SECONDS = 20
RIDER_SPEED_KMH = 24
OPENAI_MODEL = getenv("OPENAI_MODEL", "gpt-3.5-turbo")
STATUS_LABELS = [
    "Request confirmed",
    "Quality checked & packed",
    "Emergency dispatch en route",
    "Arrived at patient site",
]
CAMP_CITIES = [
    {"id": "delhi", "name": "New Delhi", "lat": 28.6139, "lng": 77.2090},
    {"id": "mumbai", "name": "Mumbai", "lat": 19.0760, "lng": 72.8777},
    {"id": "bengaluru", "name": "Bengaluru", "lat": 12.9716, "lng": 77.5946},
    {"id": "hyderabad", "name": "Hyderabad", "lat": 17.3850, "lng": 78.4867},
]
BLOOD_GROUPS = ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"]
RBC_RECIPIENT_COMPATIBILITY = {
    "O-": ["O-"],
    "O+": ["O-", "O+"],
    "A-": ["O-", "A-"],
    "A+": ["O-", "O+", "A-", "A+"],
    "B-": ["O-", "B-"],
    "B+": ["O-", "O+", "B-", "B+"],
    "AB-": ["O-", "A-", "B-", "AB-"],
    "AB+": BLOOD_GROUPS,
}


def upcoming_camps():
    today = date.today()
    schedule = [
        {
            "id": "connaught-place",
            "city_id": "delhi",
            "city": "New Delhi",
            "name": "Central Delhi Community Drive",
            "venue": "Community Hall, Connaught Place",
            "address": "Block A, Connaught Place, New Delhi",
            "days_ahead": 4,
            "time": "09:00-14:00",
            "lat": 28.6322,
            "lng": 77.2197,
        },
        {
            "id": "lodhi-road",
            "city_id": "delhi",
            "city": "New Delhi",
            "name": "Lodhi Road Donor Camp",
            "venue": "India Habitat Centre",
            "address": "Lodhi Road, New Delhi",
            "days_ahead": 8,
            "time": "10:00-15:00",
            "lat": 28.5897,
            "lng": 77.2250,
        },
        {
            "id": "nehru-place",
            "city_id": "delhi",
            "city": "New Delhi",
            "name": "South Delhi Blood Drive",
            "venue": "Nehru Place District Centre",
            "address": "Nehru Place, New Delhi",
            "days_ahead": 12,
            "time": "09:30-14:30",
            "lat": 28.5491,
            "lng": 77.2513,
        },
        {
            "id": "dadar",
            "city_id": "mumbai",
            "city": "Mumbai",
            "name": "Dadar Community Blood Drive",
            "venue": "Shivaji Mandir Auditorium",
            "address": "Dadar West, Mumbai",
            "days_ahead": 5,
            "time": "09:00-14:00",
            "lat": 19.0222,
            "lng": 72.8413,
        },
        {
            "id": "jayanagar",
            "city_id": "bengaluru",
            "city": "Bengaluru",
            "name": "Jayanagar Donor Camp",
            "venue": "Jayanagar 4th Block Community Hall",
            "address": "Jayanagar, Bengaluru",
            "days_ahead": 9,
            "time": "10:00-15:00",
            "lat": 12.9250,
            "lng": 77.5838,
        },
        {
            "id": "lakdikapul",
            "city_id": "hyderabad",
            "city": "Hyderabad",
            "name": "Lakdikapul Blood Donation Drive",
            "venue": "Red Cross Community Centre",
            "address": "Lakdikapul, Hyderabad",
            "days_ahead": 13,
            "time": "09:30-14:30",
            "lat": 17.4022,
            "lng": 78.4625,
        },
    ]
    city_centers = {city["id"]: city for city in CAMP_CITIES}
    for camp in schedule:
        camp["date"] = (today + timedelta(days=camp.pop("days_ahead"))).isoformat()
        city = city_centers[camp["city_id"]]
        camp["city_lat"] = city["lat"]
        camp["city_lng"] = city["lng"]
    return schedule


def distance_in_km(start, end):
    lat1, lon1 = radians(start["lat"]), radians(start["lng"])
    lat2, lon2 = radians(end["lat"]), radians(end["lng"])
    lat_delta = lat2 - lat1
    lon_delta = lon2 - lon1
    value = sin(lat_delta / 2) ** 2 + cos(lat1) * cos(lat2) * sin(lon_delta / 2) ** 2
    return 6371 * 2 * asin(sqrt(value))


def order_snapshot(order):
    elapsed = max(0, time() - order["created_at"])
    delivery_seconds = PREPARATION_SECONDS + order["travel_seconds"]

    if elapsed < 8:
        status_index = 0
    elif elapsed < PREPARATION_SECONDS:
        status_index = 1
    elif elapsed < delivery_seconds:
        status_index = 2
    else:
        status_index = 3

    return {
        "id": order["id"],
        "blood_group": order["blood_group"],
        "patient": order["patient"],
        "blood_bank": blood_bank,
        "distance_km": round(order["distance_km"], 2),
        "elapsed_seconds": round(elapsed, 1),
        "delivery_seconds": round(delivery_seconds, 1),
        "eta_seconds": max(0, round(delivery_seconds - elapsed)),
        "status_index": status_index,
        "status_labels": STATUS_LABELS,
    }


def smart_mock_response(user_message, stock_snapshot, active_order, camps):
    message = user_message.lower()
    groups = re.findall(r"(?<![A-Z0-9])((?:AB|A|B|O)[+-])(?![A-Z0-9])", user_message.upper())

    if any(term in message for term in ("emergency", "bleeding", "unconscious", "accident", "urgent", "help now")):
        return (
            "If someone may be in immediate danger, call your local emergency number and contact the treating hospital now. "
            "Do not wait for this chat or attempt a transfusion outside a licensed clinical setting."
        )

    if any(term in message for term in ("compatib", "blood group", "blood type", "transfusion", "recipient", "donor for")):
        if groups:
            recipient = groups[0]
            donors = ", ".join(RBC_RECIPIENT_COMPATIBILITY[recipient])
            return (
                f"For red blood cells, a recipient with {recipient} is generally compatible with donor groups: {donors}. "
                "This is a simplified ABO/Rh guide only; the blood bank must confirm the component, antibody screen, and crossmatch."
            )
        return "Tell me the recipient's blood group (for example, A+), and I can show the simplified red-cell compatibility guide. A blood bank must confirm every transfusion match."

    if any(term in message for term in ("eligible", "eligibility", "donat", "before donation", "after donation", "weight", "hydration", "prepare", "health condition")):
        if any(term in message for term in ("after", "post-donation", "aftercare", "after donation")):
            return (
                "After donating, rest briefly, drink fluids, have a snack, and avoid strenuous exercise and heavy lifting for the rest of the day. "
                "Tell camp staff if you feel faint or unwell, and follow their instructions."
            )
        return (
            "Common screening guidance is to be an adult (often 18+), meet the local minimum weight (often around 50 kg), and feel well on donation day. "
            "Age, weight, hemoglobin, medicines, recent illness, travel, pregnancy, and medical history can affect eligibility; the local blood service makes the decision. "
            "Before donating, eat a normal meal, drink water, and bring identification."
        )

    if any(term in message for term in ("camp", "drive", "where can i donate", "donation location")):
        camp_text = "; ".join(
            f"{camp['name']} at {camp['venue']}, {camp['city']} on {camp['date']} ({camp['time']})"
            for camp in camps
        )
        return f"Upcoming demo camps: {camp_text}. Check the camp cards for map locations and confirm details with organizers before travelling."

    if any(term in message for term in ("stock", "inventory", "available", "units")):
        stock_text = ", ".join(f"{group}: {count}" for group, count in stock_snapshot.items())
        return f"Current demo blood-bank stock: {stock_text}. Counts can change; confirm availability with the hospital before relying on them."

    if any(term in message for term in ("order", "delivery", "eta", "where is", "tracking")):
        if active_order:
            minutes = (active_order["eta_seconds"] + 59) // 60
            return (
                f"Order {active_order['id'][:8].upper()} is at status: {active_order['status_labels'][active_order['status_index']]}. "
                f"The tracker estimates {minutes} minute(s) remaining. This demo status is simulated."
            )
        return "There is no active delivery attached to this chat. Place a request to start the demo tracker. For a real emergency, call local emergency services and the hospital directly."

    return (
        "I can help with simplified red-cell compatibility, common donor preparation and aftercare, upcoming demo camps, and current demo stock. "
        "For immediate medical emergencies, contact emergency services and a clinician."
    )


@app.get("/")
def home():
    with state_lock:
        stock_snapshot = blood_stock.copy()
    return render_template(
        "index.html",
        stock=stock_snapshot,
        blood_bank=blood_bank,
        camps=upcoming_camps(),
        camp_cities=CAMP_CITIES,
    )


@app.get("/api/stock")
def get_stock():
    with state_lock:
        return jsonify(stock=blood_stock.copy())


@app.get("/api/camps")
def get_camps():
    return jsonify(camps=upcoming_camps(), cities=CAMP_CITIES)


@app.post("/api/orders")
def create_order():
    payload = request.get_json(silent=True) or {}
    blood_group = payload.get("blood_group")

    if not isinstance(blood_group, str) or blood_group not in blood_stock:
        return jsonify(error="Choose a valid blood group."), 400

    try:
        patient = {"lat": float(payload["lat"]), "lng": float(payload["lng"])}
    except (KeyError, TypeError, ValueError):
        return jsonify(error="A valid patient location is required."), 400

    if not all(isfinite(value) for value in patient.values()) or not (
        -90 <= patient["lat"] <= 90 and -180 <= patient["lng"] <= 180
    ):
        return jsonify(error="Patient coordinates are out of range."), 400

    with state_lock:
        if blood_stock[blood_group] < 1:
            return jsonify(error=f"{blood_group} is currently out of stock."), 409

        blood_stock[blood_group] -= 1
        order_id = str(uuid4())
        distance_km = distance_in_km(blood_bank, patient)
        travel_seconds = max(45, distance_km / RIDER_SPEED_KMH * 3600)
        order = {
            "id": order_id,
            "blood_group": blood_group,
            "patient": patient,
            "distance_km": distance_km,
            "travel_seconds": travel_seconds,
            "created_at": time(),
        }
        orders[order_id] = order
        snapshot = order_snapshot(order)
        snapshot["stock"] = blood_stock.copy()

    return jsonify(snapshot), 201


@app.get("/api/orders/<order_id>")
def get_order(order_id):
    with state_lock:
        order = orders.get(order_id)
        if order is None:
            return jsonify(error="Order not found."), 404
        return jsonify(order_snapshot(order))


@app.post("/api/chat")
def emergency_chat():
    payload = request.get_json(silent=True) or {}
    user_message = payload.get("message", "")
    if not isinstance(user_message, str) or not user_message.strip() or len(user_message) > 1000:
        return jsonify(error="Enter a message of 1 to 1000 characters."), 400

    history = payload.get("history", [])
    if not isinstance(history, list):
        return jsonify(error="Chat history must be a list."), 400

    conversation = []
    for item in history[-8:]:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            conversation.append({"role": item["role"], "content": content.strip()[:1000]})

    with state_lock:
        stock_snapshot = blood_stock.copy()
        order_id = payload.get("order_id")
        active_order = orders.get(order_id) if isinstance(order_id, str) else None
        delivery_context = order_snapshot(active_order) if active_order else None
    camps = upcoming_camps()
    fallback = smart_mock_response(user_message, stock_snapshot, delivery_context, camps)
    api_key = getenv("OPENAI_API_KEY")
    if not api_key:
        return jsonify(reply=fallback, source="smart-fallback")

    system_prompt = (
        "You are LifePulse's emergency blood information assistant. Give concise, calm, "
        "plain-language general information. Do not diagnose, prescribe, promise a donor, "
        "or claim an order was dispatched unless the supplied order context confirms it. "
        "For immediate danger, tell the user to contact local emergency services and their "
        "hospital/clinical team now; do not delay care to use this chat. Never advise anyone "
        "to perform a transfusion outside a licensed clinical setting. Blood compatibility "
        "below is a simplified red-blood-cell ABO/Rh guide only; a qualified clinician and "
        "blood bank must determine component selection, antibody screening, and crossmatch. "
        "For red-cell transfusions, a recipient may generally receive: O-: O-; O+: O-, O+; "
        "A-: O-, A-; A+: O-, O+, A-, A+; B-: O-, B-; B+: O-, O+, B-, B+; "
        "AB-: O-, A-, B-, AB-; AB+: all listed ABO/Rh groups. "
        "For donor questions, explain that age, weight, hemoglobin, medical history, medicines, "
        "recent illness/travel, and pregnancy may affect eligibility; rules vary by location "
        "and final screening belongs to the blood service. Encourage a normal meal, water, "
        "rest, and following staff instructions. "
        f"Current demo inventory by blood group: {stock_snapshot}. "
        f"Current order context, if any: {delivery_context or 'No active order provided.'} "
        f"Upcoming demo camps: {camps}. "
        "Only report stock and delivery facts found in this context. State when something is unknown."
    )
    messages = [{"role": "system", "content": system_prompt}, *conversation]
    messages.append({"role": "user", "content": user_message.strip()})

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=20.0, max_retries=1)
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=350,
        )
        answer = completion.choices[0].message.content
        if not answer or not answer.strip():
            raise ValueError("OpenAI returned an empty response")
        return jsonify(reply=answer.strip(), source="openai")
    except Exception as error:
        app.logger.info("Assistant provider unavailable; using smart fallback (%s)", type(error).__name__)
        return jsonify(reply=fallback, source="smart-fallback")


@app.post("/donate")
def donate():
    payload = request.get_json(silent=True) or request.form
    blood_group = payload.get("blood_group")
    with state_lock:
        if not isinstance(blood_group, str) or blood_group not in blood_stock:
            return jsonify(error="Choose a valid blood group."), 400
        blood_stock[blood_group] += 1
        stock_snapshot = blood_stock.copy()
    return jsonify(stock=stock_snapshot)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)