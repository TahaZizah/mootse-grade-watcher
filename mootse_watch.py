#!/usr/bin/env python3
"""
Mootse grade watcher.

Checks every gradebook item in every course you're enrolled in (including the
"Élément manuel" grades that never show up on the overview page and never
trigger a Moodle notification), and pushes a phone notification via ntfy
whenever a grade appears or changes.

Usage:
  python mootse_watch.py --get-token   # one-time: get your Moodle API token
  python mootse_watch.py               # run a check (what GitHub Actions does)

Environment variables:
  MOODLE_TOKEN   (required) token from --get-token
  NTFY_TOPIC     (required) your secret ntfy topic name
  MOODLE_URL     default https://mootse.telecom-st-etienne.fr
  NTFY_SERVER    default https://ntfy.sh
  STATE_FILE     default state.json (next to this script)

No third-party packages needed.
"""

import getpass
import hashlib
import hmac
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MOODLE_URL = os.environ.get("MOODLE_URL", "https://mootse.telecom-st-etienne.fr").rstrip("/")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
MAX_SEPARATE_NOTIFICATIONS = 6  # more changes than this at once → one summary push


class MoodleError(Exception):
    def __init__(self, errorcode, message):
        super().__init__(f"{errorcode}: {message}")
        self.errorcode = errorcode


# ---------------------------------------------------------------- HTTP helpers

def _post(url, data=None, json_body=None, timeout=30):
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def ws(token, function, **params):
    """Call a Moodle web service function through the REST endpoint."""
    payload = {"wstoken": token, "wsfunction": function, "moodlewsrestformat": "json", **params}
    result = json.loads(_post(f"{MOODLE_URL}/webservice/rest/server.php", data=payload))
    if isinstance(result, dict) and "exception" in result:
        raise MoodleError(result.get("errorcode", "unknown"), result.get("message", ""))
    return result


def notify(topic, title, message, click=None, tags=("mortar_board",), priority=5):
    body = {"topic": topic, "title": title, "message": message,
            "tags": list(tags), "priority": priority}
    if click:
        body["click"] = click
    try:
        _post(NTFY_SERVER + "/", json_body=body)
    except urllib.error.URLError as e:
        print(f"[warn] ntfy push failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------- grade logic

def clean(text):
    """Strip HTML (e.g. the ✓ 'Réussi' icon Moodle puts in some grades)."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def fmt_grade(item):
    grade = clean(item.get("gradeformatted"))
    gmax = item.get("grademax")
    if grade and grade != "-" and gmax not in (None, ""):
        try:
            gmax = f"{float(gmax):g}"
        except (TypeError, ValueError):
            pass
        return f"{grade} / {gmax}"
    return grade


def fetch_grades(token):
    """Return {key: {course, courseid, item, grade, total}} for every graded item."""
    site = ws(token, "core_webservice_get_site_info")
    userid = site["userid"]
    courses = ws(token, "core_enrol_get_users_courses", userid=userid)

    grades = {}
    for course in courses:
        cid = course["id"]
        cname = course.get("fullname") or course.get("shortname") or str(cid)
        try:
            report = ws(token, "gradereport_user_get_grade_items", courseid=cid, userid=userid)
        except MoodleError as e:
            # Some courses (info pages etc.) have no gradebook access; skip them.
            print(f"[skip] a course: {e.errorcode}", file=sys.stderr)
            continue

        for ug in report.get("usergrades", []):
            items = ug.get("gradeitems", [])
            total = next((fmt_grade(i) for i in items if i.get("itemtype") == "course"), "")
            for item in items:
                # Course/category totals change whenever an item changes;
                # only track the real items so you get one push per new grade.
                if item.get("itemtype") in ("course", "category"):
                    continue
                grade = fmt_grade(item)
                if not grade or grade.startswith("-"):
                    continue
                name = (item.get("itemname") or "").strip() or item.get("itemmodule") or "Grade"
                grades[f"{cid}:{item['id']}"] = {
                    "course": cname,
                    "courseid": cid,
                    "item": name,
                    "grade": grade,
                    "total": total if total and not total.startswith("-") else "",
                }
    return grades


def fp(key, value, secret):
    """Keyed fingerprint so the public state file reveals nothing about grades."""
    return hmac.new(secret.encode(), f"{key}\x00{value}".encode(), hashlib.sha256).hexdigest()[:24]


def hashed_state(grades, secret):
    return {fp(k, "", secret): fp(k, g["grade"], secret) for k, g in grades.items()}


def diff(old_state, grades, secret):
    """old_state: {key_fp: grade_fp}. Returns [(kind, grade_info)]."""
    changes = []
    for key, cur in grades.items():
        prev = old_state.get(fp(key, "", secret))
        if prev is None:
            changes.append(("new", cur))
        elif prev != fp(key, cur["grade"], secret):
            changes.append(("changed", cur))
    return changes


def report_url(courseid):
    return f"{MOODLE_URL}/grade/report/user/index.php?id={courseid}"


def push_changes(topic, changes):
    if len(changes) > MAX_SEPARATE_NOTIFICATIONS:
        lines = [f"• {c['course']} — {c['item']}: {c['grade']}" for _, c in changes]
        notify(topic, f"{len(changes)} new/updated grades on Mootse", "\n".join(lines),
               click=f"{MOODLE_URL}/grade/report/overview/index.php")
        return
    for kind, c in changes:
        if kind == "new":
            title = f"New grade: {c['course']}"
            msg = f"{c['item']}: {c['grade']}"
        else:
            title = f"Grade updated: {c['course']}"
            msg = f"{c['item']}: now {c['grade']}"
        if c.get("total"):
            msg += f"\nCourse total: {c['total']}"
        notify(topic, title, msg, click=report_url(c["courseid"]))


# ---------------------------------------------------------------- entry points

def get_token():
    print(f"Getting a mobile-app token from {MOODLE_URL}")
    print("Your password is sent only to Mootse and is not stored anywhere.\n")
    username = input("Mootse username: ").strip()
    password = getpass.getpass("Mootse password: ")
    raw = _post(f"{MOODLE_URL}/login/token.php",
                data={"username": username, "password": password, "service": "moodle_mobile_app"})
    result = json.loads(raw)
    if "token" not in result:
        sys.exit(f"Failed: {result.get('error') or result}")
    print("\nYour token (save it as the MOODLE_TOKEN secret, keep it private):\n")
    print(result["token"])


def check():
    token = os.environ.get("MOODLE_TOKEN")
    topic = os.environ.get("NTFY_TOPIC")
    if not token or not topic:
        sys.exit("MOODLE_TOKEN and NTFY_TOPIC must be set.")

    saved = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    try:
        new = fetch_grades(token)
    except MoodleError as e:
        if e.errorcode in ("invalidtoken", "accessexception") and not saved.get("expired_notified"):
            notify(topic, "Mootse watcher: token expired",
                   "Run `python mootse_watch.py --get-token` again and update the MOODLE_TOKEN secret.",
                   tags=("warning",), priority=3)
            saved["expired_notified"] = True  # alert once, not every 10 minutes
            STATE_FILE.write_text(json.dumps(saved, indent=1, sort_keys=True), encoding="utf-8")
        raise

    secret = token
    check_fp = fp("__check__", "", secret)
    # First run, or token changed (fingerprints no longer comparable): re-baseline quietly.
    first_run = saved.get("check") != check_fp
    old_state = saved.get("items", {})
    new_state = {"check": check_fp, "items": hashed_state(new, secret)}

    if first_run:
        if not saved:
            courses = len({g["courseid"] for g in new.values()})
            notify(topic, "Mootse watcher is on",
                   f"Tracking {len(new)} grades across {courses} courses. "
                   "You'll get a push whenever one appears or changes.",
                   tags=("white_check_mark",), priority=3)
        print(f"Baseline saved: {len(new)} grades.")
    else:
        changes = diff(old_state, new, secret)
        if changes:
            push_changes(topic, changes)
        print(f"{len(changes)} change(s); {len(new)} grades tracked.")

    if new_state != saved:
        STATE_FILE.write_text(json.dumps(new_state, indent=1, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    if "--get-token" in sys.argv:
        get_token()
    else:
        check()
