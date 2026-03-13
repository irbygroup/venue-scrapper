from datetime import datetime, timezone
from typing import Optional


def days_until_event(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        event_dt = datetime.fromisoformat(date_str.replace("Z", "")).replace(tzinfo=timezone.utc)
        delta = (event_dt - datetime.now(timezone.utc)).days
        return max(0, delta)
    except Exception:
        return None


def classify_thread(activities: list) -> dict:
    """Classify activities into useful signals."""
    our_messages   = [a for a in activities if a["ActivityTypeCd"] == "provplnr"]
    their_messages = [a for a in activities if a["ActivityTypeCd"] == "plnrprov"]

    we_replied = len(our_messages) > 0

    first_our_time = our_messages[0]["DateTime"] if our_messages else None
    they_replied_to_us = any(
        a["DateTime"] > first_our_time
        for a in their_messages
        if first_our_time and a["DateTime"]
    )

    hours_since_our_msg = None
    last_our_msg_text   = None
    last_their_msg_text = None
    if our_messages:
        last_our = max(our_messages, key=lambda a: a["DateTime"] or "")
        last_our_msg_text = last_our["ResponseText"]
        try:
            dt = datetime.fromisoformat(last_our["DateTime"]).replace(tzinfo=timezone.utc)
            hours_since_our_msg = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            pass

    if their_messages:
        last_theirs = max(their_messages, key=lambda a: a["DateTime"] or "")
        last_their_msg_text = last_theirs["ResponseText"]

    return {
        "we_replied":           we_replied,
        "they_replied_to_us":   they_replied_to_us,
        "hours_since_our_msg":  hours_since_our_msg,
        "last_our_message":     last_our_msg_text,
        "last_their_message":   last_their_msg_text,
        "our_message_count":    len(our_messages),
        "their_message_count":  len(their_messages),
    }


def compute_urgency(days_until: Optional[int], they_replied: bool,
                    hours_since_our: Optional[float]) -> tuple[str, list]:
    reasons = []
    if they_replied:
        reasons.append("they replied — awaiting your response")
        return "HIGH", reasons
    if days_until is not None and days_until <= 10:
        reasons.append(f"event in {days_until} days")
        return "HIGH", reasons
    if days_until is not None and days_until <= 30:
        reasons.append(f"event in {days_until} days")
        urgency = "MEDIUM"
    else:
        urgency = "LOW"
    if hours_since_our and hours_since_our > 4:
        reasons.append(f"read our message, no reply in {hours_since_our:.0f}h")
        urgency = max(urgency, "MEDIUM", key=lambda x: {"HIGH": 2, "MEDIUM": 1, "LOW": 0}[x])
    return urgency, reasons


def build_thread_view(activities: list) -> list:
    """Clean thread for display — skip noise."""
    skip = {"ResponseRank", "LeadPurchased", "ReferralViewed"}
    type_map = {
        "LeadReceived":    "inquiry",
        "ReferralReceived":"inquiry",
        "provplnr":        "our_reply",
        "plnrprov":        "their_reply",
        "ReadMsgs":        "read_receipt",
        "NoInterest":      "no_interest",
    }
    return [
        {
            "from":  a["Sender"] or "system",
            "type":  type_map.get(a["ActivityTypeCd"], a["ActivityTypeCd"]),
            "text":  a["ResponseText"],
            "at":    a["DateTime"],
        }
        for a in activities
        if a["ActivityTypeCd"] not in skip
    ]


def build_lead_detail(l_row, ld_row, activities: list) -> dict:
    """Assemble full lead detail from DB rows + activities."""
    event_date  = (ld_row["DatePossible1"] if ld_row else None) or l_row["EventDate"]
    d_until     = days_until_event(event_date)
    signals     = classify_thread(activities)
    urgency, urgency_reasons = compute_urgency(
        d_until, signals["they_replied_to_us"], signals["hours_since_our_msg"]
    )

    contact = {}
    event   = {}
    meta    = {}

    if ld_row:
        contact = {
            "name":         ld_row["RequestorName"],
            "phone":        ld_row["RequestorPhone"],
            "email":        ld_row["RequestorEmailAddress"],
            "location":     ld_row["DirectLeadLocation"],
            "contact_pref": ld_row["RequestorContactPref"],
        }
        event = {
            "type":          ld_row["EventType"] or l_row["EventType"],
            "date":          event_date.split("T")[0] if event_date else None,
            "days_until":    d_until,
            "guests":        ld_row["AttendeeCount"] or l_row["AttendeeCount"],
            "budget":        ld_row["BudgetValue"],
            "duration":      ld_row["Duration"],
            "date_flexible": bool(ld_row["DateFlexible"]),
            "notes":         ld_row["InformationRequested"],
            "food":          (
                "Not served"      if not ld_row["FoodRequired"]      else
                "Provided by venue"    if ld_row["VenueProvidesFood"]     else
                "Outside caterer" if ld_row["CatererProvidesFood"]   else
                "Self-provided"
            ),
        }
    else:
        contact = {"name": l_row["PlannerName"]}
        event   = {
            "type": l_row["EventType"], "date": l_row["EventDate"],
            "days_until": d_until, "guests": l_row["AttendeeCount"],
        }

    meta = {
        "status":         l_row["LeadStatus"],
        "source":         l_row["Source"],
        "venue":          l_row["ProviderName"],
        "received_at":    l_row["EmailSentDttm"],
        "last_activity":  l_row["LastActivityDttm"],
    }

    return {
        "event_id":        l_row["EventId"],
        "contact":         contact,
        "event":           event,
        "meta":            meta,
        "urgency":         urgency,
        "urgency_reasons": urgency_reasons,
        "thread_signals":  signals,
        "thread":          build_thread_view(activities),
    }


def classify_change(event_id: str, signals: dict, cur) -> str:
    """Classify what changed: new_lead | replied_to_us | read_no_reply | updated"""
    cur.execute(
        'SELECT "DetailScrapedAt" FROM eventective_leads WHERE "EventId"=%s', (event_id,)
    )
    row = cur.fetchone()
    if not row or not row["DetailScrapedAt"]:
        return "new_lead"
    if signals["they_replied_to_us"]:
        return "replied_to_us"
    if signals["we_replied"] and not signals["they_replied_to_us"]:
        return "read_no_reply"
    return "updated"
