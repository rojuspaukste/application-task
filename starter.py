#!/usr/bin/env python3
"""Alex's morning handover helper. Python 3, no packages.

Run: python starter.py data/evidence/01_initial_notes_S1-S8.txt [later_notes.txt ...] [--csv data/outputs/NAME] [--json]

Input: one or more text files of Alex's notes, pasted from the task site. Each note is a
line "S<number>", a title line, a type line, then the note text - or, for updates, a line
"S<number> <title>" followed by the note text. A later update can be saved as another file
and passed after the first one. A note with no time on the site can carry "[assumed HH:MM]";
the timeline marks such times as assumed.

Output:
  1. Summary table - each request, its status and the suggested next action.
  2. Timeline - the extracted events ordered by time, each with the note it came from.
  3. Details - open questions, things to watch and all suggested actions per request.
  4. Sentences the rules did not use - so a person reads them instead of the tool guessing.
With --csv data/outputs/NAME the summary and timeline are also saved as NAME_summary.csv and
NAME_timeline.csv (open in Excel).

Built on the Head Aero starter and keeps its promise: it never invents approval, delivery
or ownership. Fixed rules only; anything the notes do not state stays Unknown.
It makes no network calls, sends no messages and changes no input files.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
           "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
TIME = r"\d{1,2}:\d{2}"
SEP = r"\s*[·–-]\s*"                  # notes separate fields with " · "
REQUEST_ID = r"\b(?!M-)[A-Z]-\d+\b"    # M-... ids are messages, not requests
NOT_NAMED = "not named in the notes"

RULES = [
    "Only the offer version marked current counts; archived versions are ignored.",
    "An offer update that replaces the current offer becomes the current offer; the old one is marked replaced.",
    "A conditional quantity counts only when a note says its condition is met.",
    "Only 'delivered' proves a message arrived; 'accepted' is not delivery and 'bounced' is a failed attempt.",
    "A delivery problem needs a question only when nobody is coordinator; otherwise it is the coordinator's next action.",
    "The latest timed ownership record decides the coordinator; 'moved off' with no successor means nobody.",
    "A cached summary refreshed before the newest record is out of date and is not trusted.",
    "Alex cannot approve quantities or assign coordinators (S1), so those actions go to someone else as a question.",
]
LIMITATIONS = [
    "Reads notes phrased like S1-S10. Any other sentence is listed as 'not used', never guessed.",
    "Times carry no date; all events are treated as the same day (Monday 9 November, S1).",
    "Notes without a time on the site (S9, S10) were given '[assumed HH:MM]' times after the last record; they are marked (assumed).",
    "Message content is not in the notes; a delivered message sent after the current offer is assumed to carry it.",
]


def number(word):
    word = word.lower()
    return int(word) if word.isdigit() else NUMBERS.get(word)


def hhmm(value):
    hours, minutes = value.split(":")
    return f"{int(hours):02d}:{minutes}"


# ---------------------------------------------------------------- 1. read the notes

def read_notes(paths):
    """Split pasted text into notes: {'id': 'S4', 'title': ..., 'type': ..., 'text': ...}."""
    notes = []
    for path in paths:
        current = None
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if m := re.fullmatch(r"(S\d+)(?:\s+(.+))?", line):   # "S4" or "S9 Current offer update · K-104"
                current = {"id": m[1], "lines": [m[2]] if m[2] else [], "inline_title": bool(m[2])}
                notes.append(current)
            elif current is not None and line:
                current["lines"].append(line)
    result = []
    for n in notes:
        lines, skip = n["lines"], 1 if n["inline_title"] else 2   # "S9 <title>" notes have no type line
        result.append({"id": n["id"], "title": lines[0] if lines else "",
                       "type": "" if n["inline_title"] or len(lines) < 2 else lines[1],
                       "text": " ".join(lines[skip:])})
    return result


# ---------------------------------------------------------------- 2. extract facts

def new_request(request_id):
    return {"id": request_id, "customer": None, "asked": None, "request_source": None,
            "offers": [], "messages": {}, "owners": [], "summaries": []}


def start_update(r, time, src):
    """An offer update without a version: the current offer becomes 'replaced', the update becomes current."""
    previous = [o for o in r["offers"] if o["status"] == "current"]
    base = previous[-1] if previous else None
    for o in previous:
        o["status"] = f"replaced by {src}"
    offer = {"name": f"{base['name']} update" if base else "update", "time": time, "status": "current",
             "approved": (base["approved"] or 0) + base["conditional"] if base else None,
             "conditional": 0, "condition": None, "condition_status": None, "condition_met": False, "source": src}
    r["offers"].append(offer)
    return offer


def extract(notes):
    """Turn note sentences into facts per request. Returns (requests, unused sentences, notes with assumed times)."""
    requests, unused, assumed = {}, [], set()

    def req(request_id):
        return requests.setdefault(request_id, new_request(request_id))

    for note in notes:
        src = note["id"]
        if re.search(rf"\[assumed {TIME}\]", note["text"]):
            assumed.add(src)
        title_ids = re.findall(REQUEST_ID, note["title"])
        context = title_ids[0] if title_ids else None   # request the note is about
        offer = message = summary = None                 # record currently being read
        for sentence in re.split(r"(?<=\.)\s+(?=[A-Z0-9])", note["text"]):
            s = sentence.strip().rstrip(".")
            ids = re.findall(REQUEST_ID, s)
            context = ids[0] if ids else context
            times = re.findall(TIME, s)
            at = hhmm(times[0]) if times else (offer["time"] if offer else None)
            used = False

            # Request queue: "Request K-104: Cedar Air asks for 3 service kits"
            if m := re.search(rf"Request ({REQUEST_ID}): (.+?) asks for (\w+) (?:service )?kits?", s):
                r = req(m[1])
                r["customer"], r["asked"], r["request_source"] = m[2], number(m[3]), src
                used = True

            # Offer record header: "10:20 · K-104 · version 2"
            if m := re.fullmatch(rf"({TIME}){SEP}({REQUEST_ID}){SEP}version (\d+)", s):
                offer = {"time": hhmm(m[1]), "name": f"v{m[3]}", "status": None, "approved": None,
                         "conditional": 0, "condition": None, "condition_status": None,
                         "condition_met": False, "source": src}
                req(m[2])["offers"].append(offer)
                context, used = m[2], True
            if offer and (m := re.search(r"Record status: (\w+)", s)):
                offer["status"], used = m[1].lower(), True
            if offer and (m := re.search(r"\b(\w+) kits? approved", s)):
                offer["approved"], used = number(m[1]), True
            if offer and (m := re.search(r"\b(\w+) (?:\w+ )?kits? may be added after (?:an? )?(?:separate )?(.+)$", s)):
                offer["conditional"], offer["condition"], used = number(m[1]), m[2].lower(), True

            # Offer update without a version (S9): "... is now unconditionally approved", "Three kits are approved in total"
            if context and re.search(r"unconditionally approved|approved in total", s):
                if not (offer and offer["source"] == src):
                    offer = start_update(req(context), at, src)
                if m := re.search(r"\b(\w+) kits? (?:are|is) approved in total", s):
                    offer["approved"] = number(m[1])
                used = True
            if re.search(r"replaces the earlier", s):   # the update handlers above already replace the old record
                used = True

            # Delivery log: "K-104, message M-104: 10:25 accepted ...; 10:27 bounced because ..."
            if m := re.search(rf"({REQUEST_ID}), message (M-\d+):", s):
                context, message = m[1], m[2]
            if message:
                for t, event, why in re.findall(rf"({TIME}) (accepted|delivered|bounced)(?:[^;]*?because ([^;]+))?", s):
                    req(context)["messages"].setdefault(message, []).append(
                        {"time": hhmm(t), "event": event, "why": why, "source": src})
                    used = True
            if re.search(r"No later event is recorded for M-\d+", s):
                used = True

            # Ownership
            if m := re.search(rf"named ([A-Z]\w+) for ({REQUEST_ID})", s):   # capital: a name, not "named coordinator"
                req(m[2])["owners"].append({"time": at, "name": m[1], "action": "assigned", "note": "", "source": src})
                used = True
            if m := re.search(r"([A-Z]\w+) moved off", s):
                req(context)["owners"].append({"time": at, "name": m[1], "action": "removed", "note": "", "source": src})
                used = True
            if m := re.search(rf"([A-Z]\w+) (?:remains|is) (?:now )?the (?:named |assigned )?coordinator(?: for ({REQUEST_ID}))?", s):
                req(m[2] or context)["owners"].append({"time": at, "name": m[1], "action": "assigned", "note": "", "source": src})
                used = True

            # Cached summary: "Generated at 09:10: K-104 · three kits · Morgan · ready"
            if m := re.search(rf"Generated at ({TIME}): ({REQUEST_ID}){SEP}(\w+) kits?{SEP}(\w+){SEP}(\w+)", s):
                summary = {"time": hhmm(m[1]), "refresh": hhmm(m[1]), "qty": number(m[3]),
                           "coordinator": m[4], "status": m[5].lower(), "source": src}
                req(m[2])["summaries"].append(summary)
                used = True

            # "Label: value" lines that complete the record above them
            if not used and (m := re.fullmatch(r"([A-Z][a-z]+(?: [a-z]+)*): (.+)", s)):
                label, value = m[1].lower(), m[2].strip()
                if offer and label == offer["condition"]:
                    offer["condition_status"] = value
                    offer["condition_met"] = value.lower() in ("received", "confirmed", "met", "yes")
                    used = True
                elif label == "successor" and context in requests and requests[context]["owners"]:
                    last = requests[context]["owners"][-1]
                    if value.lower().startswith("not"):
                        last["note"] = f"successor {value}"
                    else:
                        req(context)["owners"].append({"time": last["time"], "name": value, "action": "assigned",
                                                       "note": "successor", "source": src})
                    used = True
                elif label == "last refresh" and summary:
                    summary["refresh"], used = hhmm(value), True

            if not used:
                unused.append((src, sentence.strip()))
    return requests, unused, assumed


# ---------------------------------------------------------------- 3. apply the rules

def assess(r):
    """Status, open questions, warnings and suggested actions for one request."""
    questions, watch, actions = [], [], []   # actions: (what, who, source)

    # Who acts next? The latest timed ownership record wins; untimed ones must agree with it.
    timed = sorted((o for o in r["owners"] if o["time"]), key=lambda o: o["time"])
    untimed = [o for o in r["owners"] if not o["time"]]
    last = timed[-1] if timed else (untimed[-1] if untimed else None)
    coordinator = last["name"] if last and last["action"] == "assigned" else None
    for o in untimed:
        if timed and o["action"] == "assigned" and o["name"] != coordinator:
            questions.append(f"Coordinator records disagree: {o['source']} names {o['name']}, "
                             f"latest timed record gives {coordinator or 'nobody'} ({last['source']})")
    if not coordinator:
        why = (f"{last['name']} moved off at {last['time']}" + (f", {last['note']}" if last["note"] else "")
               if last else "no coordinator recorded")
        src = last["source"] if last else "-"
        questions.append(f"No coordinator: {why} ({src})")
        actions.append(("Name a coordinator for this request",
                        f"Whoever assigns coordinators ({NOT_NAMED}); Alex can only ask", src))
    doer = coordinator or "New coordinator, once named"

    # What is current? Exactly one offer must be marked current.
    current = [o for o in r["offers"] if o["status"] == "current"]
    offer, confirmed = (current[0], current[0]["approved"] or 0) if len(current) == 1 else (None, None)
    if offer is None:
        srcs = ", ".join(o["source"] for o in r["offers"]) or "-"
        questions.append(("No offer version is marked current" if not current
                          else "Several offer versions are marked current") + f" ({srcs})")
        actions.append(("Confirm which offer version is current",
                        f"Quantity approver ({NOT_NAMED}); Alex can only ask", srcs))
    else:
        pending = offer["conditional"] if offer["conditional"] and not offer["condition_met"] else 0
        if offer["conditional"] and offer["condition_met"]:
            confirmed += offer["conditional"]
        if pending:
            watch.append(f"{pending} more kit(s) only after {offer['condition']} "
                         f"({offer['condition_status'] or 'status unknown'}) ({offer['source']})")
            actions.append((f"Get the {offer['condition']} for the extra {pending} kit(s)", doer, offer["source"]))
        if r["asked"] is not None and confirmed + pending < r["asked"]:
            questions.append(f"Asked for {r['asked']}, current offer covers {confirmed + pending} ({offer['source']})")
            actions.append(("Decide how to handle the missing kit(s)",
                            f"Quantity approver ({NOT_NAMED}); Alex can only ask", offer["source"]))
    for o in r["offers"]:
        if o["status"] != "current":
            watch.append(f"Ignore offer {o['name']} ({o['approved']} kits): {o['status']} ({o['source']})")

    # What happened? Only a delivery after the current offer proves the customer has it.
    latest = {mid: max(events, key=lambda e: e["time"]) for mid, events in r["messages"].items()}
    received = [mid for mid, e in latest.items()
                if e["event"] == "delivered" and (offer is None or e["time"] >= offer["time"])]
    for mid, e in latest.items():
        if mid in received:
            continue
        if e["event"] == "delivered":
            text = f"{mid} was delivered at {e['time']}, before current offer {offer['name']} - customer may hold an older version"
        elif e["event"] == "bounced":
            text = f"{mid} bounced at {e['time']} ({e['why'] or 'no reason given'}); no later attempt recorded"
        else:
            text = f"{mid} only accepted at {e['time']}; delivery not recorded, so receipt is Unknown"
        # A delivery problem needs a question only when nobody owns the request; otherwise it is the coordinator's job.
        (watch if received or coordinator else questions).append(f"{text} ({e['source']})")
    msg_srcs = ", ".join(sorted({e["source"] for e in latest.values()})) or "-"
    if not r["messages"]:
        (watch if coordinator else questions).append("No message to the customer is recorded")
    if offer and not received:
        verb = "re-send" if r["messages"] else "send"
        actions.append((f"Check the customer's address and {verb} the current offer ({confirmed} kit(s))", doer, msg_srcs))

    # Is the cached summary still true?
    record_times = [o["time"] for o in r["offers"]] + [o["time"] for o in r["owners"] if o["time"]] + \
                   [e["time"] for events in r["messages"].values() for e in events]
    newest = max(record_times, default=None)
    for s in r["summaries"]:
        diffs = []
        if confirmed is not None and s["qty"] != confirmed:
            diffs.append(f"{s['qty']} kits (current offer: {confirmed})")
        if s["coordinator"] != coordinator:
            diffs.append(f"{s['coordinator']} (coordinator now: {coordinator or 'nobody'})")
        if s["status"] == "ready" and questions:
            diffs.append("'ready' (open questions exist)")
        if (newest and s["refresh"] < newest) or diffs:
            watch.append(f"Cached summary refreshed {s['refresh']} is out of date (records until {newest})"
                         + (f"; it says {', '.join(diffs)}" if diffs else "") + f" ({s['source']})")
            actions.append(("Do not use the cached summary; refresh it", f"Report owner ({NOT_NAMED})", s["source"]))

    status = "Needs a question" if questions else "Can move forward"
    if not questions and received:
        actions.insert(0, (f"Follow up with {r['customer']} on the delivered offer for {confirmed} kit(s)",
                           coordinator, msg_srcs))
    sources = sorted({x["source"] for x in r["offers"] + r["owners"] + r["summaries"]}
                     | {e["source"] for events in r["messages"].values() for e in events}
                     | ({r["request_source"]} if r["request_source"] else set()), key=lambda x: int(x[1:]))
    if r["asked"] is None:
        questions.append("No request record found for this ID")
        status = "Needs a question"
    return {"id": r["id"], "customer": r["customer"] or "Unknown", "asked": r["asked"], "status": status,
            "coordinator": coordinator, "confirmed": confirmed, "questions": questions, "watch": watch,
            "actions": actions, "sources": sources}


# ---------------------------------------------------------------- 4. timeline and report

def timeline(requests):
    rows = []
    for r in requests.values():
        if r["asked"] is not None:
            rows.append((None, r["id"], f"{r['customer']} asks for {r['asked']} kit(s)", r["request_source"]))
        for o in r["offers"]:
            text = f"Offer {o['name']} ({o['status'] or 'status unknown'}): {o['approved']} kit(s) approved"
            if o["conditional"]:
                text += f", +{o['conditional']} after {o['condition']} ({o['condition_status'] or 'status unknown'})"
            rows.append((o["time"], r["id"], text, o["source"]))
        for mid, events in r["messages"].items():
            for e in events:
                rows.append((e["time"], r["id"], f"Message {mid} {e['event']}" + (f": {e['why']}" if e["why"] else ""), e["source"]))
        for o in r["owners"]:
            verb = "named coordinator" if o["action"] == "assigned" else "moved off as coordinator"
            rows.append((o["time"], r["id"], f"{o['name']} {verb}" + (f"; {o['note']}" if o["note"] else ""), o["source"]))
        for s in r["summaries"]:
            rows.append((s["time"], r["id"], f"Cached summary says: {s['qty']} kits, {s['coordinator']}, {s['status']} "
                                             f"(last refresh {s['refresh']})", s["source"]))
    return sorted(rows, key=lambda row: (row[0] is not None, row[0] or "", row[1]))


def table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    return lines + ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]


def report(results, events, unused, assumed, paths):
    out = ["# Morning handover", f"From: {', '.join(paths)}. Times in UTC.", "", "## 1. Summary"]
    out += table(["Request", "Customer", "Status", "Who acts next", "Suggested next action", "Sources"],
                 [(r["id"], r["customer"], f"**{r['status']}**", r["coordinator"] or "Nobody assigned",
                   f"{r['actions'][0][0]} - {r['actions'][0][1]}" if r["actions"] else "-", ", ".join(r["sources"]))
                  for r in results])
    out += ["", "## 2. Timeline (earliest first)"]
    out += table(["Time", "Request", "Event", "Source"],
                 [((t or "time not stated") + (" (assumed)" if t and src in assumed else ""), rid, text, src)
                  for t, rid, text, src in events])
    out += ["", "## 3. Details"]
    for r in results:
        out += ["", f"### {r['id']} - {r['customer']} - {r['status']}"]
        if r["questions"]:
            out += ["Open questions:"] + [f"- {q}" for q in r["questions"]]
        if r["watch"]:
            out += ["Watch out:"] + [f"- {w}" for w in r["watch"]]
        out += ["Suggested actions:"] + table(["#", "Action", "Who", "Source"],
                                              [(i, a, w, s) for i, (a, w, s) in enumerate(r["actions"], 1)])
    out += ["", "## 4. Sentences the rules did not use (read these yourself)"]
    out += [f"- {src}: {text}" for src, text in unused] or ["- none"]
    out += ["", "## Rules used"] + [f"- {rule}" for rule in RULES]
    out += ["", "## Limitations"] + [f"- {limit}" for limit in LIMITATIONS]
    return "\n".join(out)


def write_csv(prefix, results, events, assumed):
    """Save the summary table and the timeline as PREFIX_summary.csv and PREFIX_timeline.csv."""
    summary_path, timeline_path = Path(f"{prefix}_summary.csv"), Path(f"{prefix}_timeline.csv")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig: Excel reads it correctly
        writer = csv.writer(f)
        writer.writerow(["Request", "Customer", "Status", "Who acts next", "Suggested actions (first = next)",
                         "Open questions", "Watch out", "Sources"])
        for r in results:
            writer.writerow([r["id"], r["customer"], r["status"], r["coordinator"] or "Nobody assigned",
                             "\n".join(f"{i}. {a} - {who} ({src})" for i, (a, who, src) in enumerate(r["actions"], 1)),
                             "\n".join(r["questions"]), "\n".join(r["watch"]), ", ".join(r["sources"])])
    with open(timeline_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Time (UTC)", "Time assumed", "Request", "Event", "Source"])
        for t, rid, text, src in events:
            writer.writerow([t or "time not stated", "yes" if t and src in assumed else "no", rid, text, src])
    return summary_path, timeline_path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("notes", nargs="+", help="Text file(s) with Alex's notes, oldest first")
    parser.add_argument("--json", action="store_true", help="Print JSON in the starter's result format instead")
    parser.add_argument("--csv", metavar="PREFIX", help="Also save PREFIX_summary.csv and PREFIX_timeline.csv")
    args = parser.parse_args()
    try:
        notes = read_notes(args.notes)
    except OSError as error:
        print("Cannot read the notes: " + str(error), file=sys.stderr)
        return 1
    if not notes:
        print("No notes found. Each note must start with a line like 'S1'.", file=sys.stderr)
        return 1
    requests, unused, assumed = extract(notes)
    results = [assess(r) for r in sorted(requests.values(), key=lambda r: r["id"])]
    sys.stdout.reconfigure(encoding="utf-8")
    if args.json:
        print(json.dumps({
            "schema_version": 1,
            "results": [{"case_id": r["id"], "status": r["status"].lower().replace(" ", "_"),
                         "next_action": r["actions"][0][0] if r["actions"] else None,
                         "open_questions": r["questions"], "watch": r["watch"],
                         "evidence": r["sources"], "limitations": LIMITATIONS} for r in results],
            "timeline": [{"time": t, "time_assumed": src in assumed, "request": rid, "event": text, "source": src}
                         for t, rid, text, src in timeline(requests)],
            "unused_sentences": [{"source": src, "text": text} for src, text in unused],
        }, indent=2, ensure_ascii=False))
    else:
        print(report(results, timeline(requests), unused, assumed, args.notes))
    if args.csv:
        for path in write_csv(args.csv, results, timeline(requests), assumed):
            print(f"Saved {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
