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
  HC_PING_URL    (optional) healthchecks.io ping URL; it alerts you if checks stop
  MOODLE_URL     default https://mootse.telecom-st-etienne.fr
  NTFY_SERVER    default https://ntfy.sh
  STATE_FILE     default state.json (next to this script)

Reliability rules:
  * Network errors and 5xx answers are retried a few times before giving up.
  * A grade is only recorded as "seen" once its push was accepted by ntfy, so a
    failed push is retried on the next run instead of being lost.
  * Seen grades are never forgotten, so a course that fails to load once can't
    cause duplicate pushes when it comes back.

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
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MOODLE_URL = os.environ.get("MOODLE_URL", "https://mootse.telecom-st-etienne.fr").rstrip("/")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
HC_PING_URL = os.environ.get("HC_PING_URL", "").strip().rstrip("/")
MAX_SEPARATE_NOTIFICATIONS = 6  # more changes than this at once -> one summary push
FAIL_ALERT_AFTER = 3            # consecutive failed runs before warning (when no HC_PING_URL)
RETRIES = 3
RETRY_DELAY = float(os.environ.get("RETRY_DELAY", "3"))
REPO_URL = "https://github.com/TahaZizah/mootse-grade-watcher"


class MoodleError(Exception):
    def __init__(self, errorcode, message):
        super().__init__(f"{errorcode}: {message}")
        self.errorcode = errorcode


# ---------------------------------------------------------------- HTTP helpers

def _request(url, data=None, json_body=None, text_body=None, timeout=20):
    if json_body is not None:
        body, ctype = json.dumps(json_body).encode("utf-8"), "application/json"
    elif text_body is not None:
        body, ctype = text_body.encode("utf-8"), "text/plain; charset=utf-8"
    else:
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        ctype = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _retrying(fn):
    """Run fn(), retrying transient failures (network errors, 5xx, 429, garbled JSON)."""
    for attempt in range(1, RETRIES + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            err = e
        if attempt == RETRIES:
            raise err
        print(f"[retry] attempt {attempt} failed ({type(err).__name__}), retrying", file=sys.stderr)
        time.sleep(RETRY_DELAY * attempt)


def ws(token, function, **params):
    """Call a Moodle web service function through the REST endpoint."""
    payload = {"wstoken": token, "wsfunction": function, "moodlewsrestformat": "json", **params}
    result = _retrying(lambda: json.loads(
        _request(f"{MOODLE_URL}/webservice/rest/server.php", data=payload)))
    if isinstance(result, dict) and "exception" in result:
        raise MoodleError(result.get("errorcode", "unknown"), result.get("message", ""))
    return result


def notify(topic, title, message, click=None, tags=("mortar_board",), priority=5):
    """Send a push. Returns True if ntfy accepted it."""
    body = {"topic": topic, "title": title, "message": message,
            "tags": list(tags), "priority": priority}
    if click:
        body["click"] = click
    try:
        _retrying(lambda: _request(NTFY_SERVER + "/", json_body=body))
        return True
    except Exception as e:  # noqa: BLE001 - a failed push must never crash the run here
        print(f"[warn] ntfy push failed: {type(e).__name__}", file=sys.stderr)
        return False


def hc_ping(suffix="", text=""):
    """Report to healthchecks.io (if configured). Never raises."""
    if not HC_PING_URL:
        return
    try:
        _retrying(lambda: _request(HC_PING_URL + suffix, text_body=text[:1000], timeout=10))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] healthchecks ping failed: {type(e).__name__}", file=sys.stderr)


# ---------------------------------------------------------------- state

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def fp(key, value, secret):
    """Keyed fingerprint so the public state file reveals nothing about grades."""
    return hmac.new(secret.encode(), f"{key}\x00{value}".encode(), hashlib.sha256).hexdigest()[:24]


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
    if not courses:
        raise RuntimeError("Mootse returned no courses")

    grades, loaded = {}, 0
    for course in courses:
        cid = course["id"]
        cname = course.get("fullname") or course.get("shortname") or str(cid)
        try:
            report = ws(token, "gradereport_user_get_grade_items", courseid=cid, userid=userid)
        except MoodleError as e:
            if e.errorcode in ("invalidtoken", "accessexception"):
                raise
            # Some courses (info pages etc.) have no gradebook access; skip them.
            print(f"[skip] a course: {e.errorcode}", file=sys.stderr)
            continue
        loaded += 1

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
    if loaded == 0:
        raise RuntimeError("no gradebook could be loaded")
    return grades


def diff(old_items, grades, secret):
    """Returns [(kind, key, grade_info)] for grades that are new or changed."""
    changes = []
    for key, cur in grades.items():
        prev = old_items.get(fp(key, "", secret))
        if prev is None:
            changes.append(("new", key, cur))
        elif prev != fp(key, cur["grade"], secret):
            changes.append(("changed", key, cur))
    return changes


def report_url(courseid):
    return f"{MOODLE_URL}/grade/report/user/index.php?id={courseid}"


def push_changes(topic, changes):
    """Push the changes. Returns the set of keys whose push was accepted."""
    if len(changes) > MAX_SEPARATE_NOTIFICATIONS:
        lines = [f"• {c['course']} — {c['item']}: {c['grade']}" for _, _, c in changes]
        ok = notify(topic, f"{len(changes)} new/updated grades on Mootse", "\n".join(lines),
                    click=f"{MOODLE_URL}/grade/report/overview/index.php")
        return {key for _, key, _ in changes} if ok else set()
    delivered = set()
    for kind, key, c in changes:
        if kind == "new":
            title = f"New grade: {c['course']}"
            msg = f"{c['item']}: {c['grade']}"
        else:
            title = f"Grade updated: {c['course']}"
            msg = f"{c['item']}: now {c['grade']}"
        if c.get("total"):
            msg += f"\nCourse total: {c['total']}"
        if notify(topic, title, msg, click=report_url(c["courseid"])):
            delivered.add(key)
    return delivered


# ---------------------------------------------------------------- entry points

def get_token():
    print(f"Getting a mobile-app token from {MOODLE_URL}")
    print("Your password is sent only to Mootse and is not stored anywhere.\n")
    username = input("Mootse username: ").strip()
    password = getpass.getpass("Mootse password: ")
    raw = _request(f"{MOODLE_URL}/login/token.php",
                   data={"username": username, "password": password, "service": "moodle_mobile_app"})
    result = json.loads(raw)
    if "token" not in result:
        sys.exit(f"Failed: {result.get('error') or result}")
    print("\nYour token (save it as the MOODLE_TOKEN secret, keep it private):\n")
    print(result["token"])


def check(token, topic):
    """One check. Returns a short summary string; raises on failure."""
    saved = load_state()
    try:
        grades = fetch_grades(token)
    except MoodleError as e:
        if e.errorcode in ("invalidtoken", "accessexception") and not saved.get("expired_notified"):
            notify(topic, "Mootse watcher: token expired",
                   "Get a new Mootse token and update the MOODLE_TOKEN secret "
                   f"(steps in the README: {REPO_URL}#renewing).",
                   tags=("warning",), priority=4, click=f"{REPO_URL}#renewing")
            saved["expired_notified"] = True  # alert once, not every run
            save_state(saved)
        raise

    secret = token
    check_fp = fp("__check__", "", secret)
    # First run, or token changed (fingerprints no longer comparable): re-baseline quietly.
    first_run = saved.get("check") != check_fp
    current = {fp(k, "", secret): fp(k, g["grade"], secret) for k, g in grades.items()}
    undelivered = 0

    if first_run:
        items = current
        if not saved:
            courses = len({g["courseid"] for g in grades.values()})
            notify(topic, "Mootse watcher is on",
                   f"Tracking {len(grades)} grades across {courses} courses. "
                   "You'll get a push whenever one appears or changes.",
                   tags=("white_check_mark",), priority=3)
        summary = f"Baseline saved: {len(grades)} grades."
    else:
        old_items = saved.get("items", {})
        items = dict(old_items)  # never drop seen grades (see module docstring)
        changes = diff(old_items, grades, secret)
        delivered = push_changes(topic, changes) if changes else set()
        for _, key, _ in changes:
            if key in delivered:
                items[fp(key, "", secret)] = current[fp(key, "", secret)]
        undelivered = len(changes) - len(delivered)
        summary = f"{len(changes)} change(s), {undelivered} undelivered; {len(grades)} grades tracked."

    new_state = {"check": check_fp, "items": items}
    if new_state != saved:
        save_state(new_state)
    if undelivered:
        raise RuntimeError(f"{undelivered} push(es) failed; they will be retried next run")
    return summary


def main():
    token = os.environ.get("MOODLE_TOKEN")
    topic = os.environ.get("NTFY_TOPIC")
    if not token or not topic:
        sys.exit("MOODLE_TOKEN and NTFY_TOPIC must be set.")

    previous_fails = load_state().get("fails", 0)
    try:
        summary = check(token, topic)
    except Exception as e:
        reason = f"{type(e).__name__}: {e}" if not isinstance(e, urllib.error.URLError) \
            else f"{type(e).__name__} (network)"
        state = load_state()
        state["fails"] = state.get("fails", 0) + 1
        save_state(state)
        if HC_PING_URL:
            hc_ping("/log", f"Check failed: {reason}")  # healthchecks alerts if no success for 30 min
        elif state["fails"] == FAIL_ALERT_AFTER and not state.get("expired_notified"):
            notify(topic, "Mootse watcher: checks are failing",
                   f"The last {FAIL_ALERT_AFTER} checks failed ({type(e).__name__}). "
                   "Grades may be missed until this is fixed.",
                   tags=("warning",), priority=4, click=f"{REPO_URL}/actions")
        raise

    print(summary)
    hc_ping("", summary)
    if previous_fails >= FAIL_ALERT_AFTER and not HC_PING_URL:
        notify(topic, "Mootse watcher is back", "Checks are working again.",
               tags=("white_check_mark",), priority=3)


if __name__ == "__main__":
    if "--get-token" in sys.argv:
        get_token()
    else:
        main()
