import html as html_lib
import json
import os
import re
import subprocess
import sys
import time

import requests

# The old uiucapi library no longer works: the course explorer's XML API stopped
# including each section's open/closed status. This version reads the status from
# the Course Explorer web page instead (the same icon you see next to each CRN).
#
# Alerts go to two places:
#  1. a macOS pop-up window that stays on screen until you click it (the notify-py
#     library's notifications were being silently blocked by macOS)
#  2. your phone, through the free ntfy app (https://ntfy.sh): install it and
#     subscribe to your topic. Anyone who knows the topic name can read your
#     alerts, so it is NOT stored in this (public) file. It comes from:
#       - the NTFY_TOPIC environment variable (on GitHub: a repository secret), or
#       - a file named .ntfy_topic next to this script (ignored by git)
#
# Two ways to run it:
#   python3 main.py          keeps running on your Mac, checking every 5 minutes
#   python3 main.py --once   checks once and exits (used by GitHub Actions, see
#                            .github/workflows/check-classes.yml)

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_topic():
    if os.environ.get("NTFY_TOPIC"):
        return os.environ["NTFY_TOPIC"].strip()
    try:
        with open(os.path.join(HERE, ".ntfy_topic")) as f:
            return f.read().strip()
    except OSError:
        return ""


PHONE_TOPIC = _load_topic()

# Where tapping the notification takes you
REGISTER_URL = "https://apps.uillinois.edu/selfservice"

PAGE_URL = "https://courses.illinois.edu/schedule/{year}/{season}/{subject}/{number}"
XML_URL = "https://courses.illinois.edu/cisapp/explorer/schedule/{year}/{season}/{subject}/{number}.xml"
# Identify this as a small class-alert bot (not a browser) so site admins can see what it is
HEADERS = {"User-Agent": "uiuc-class-checker/1.0 (+https://github.com/sltran3/uiuc-class-checker)"}

_course_titles = {}


def clean(fragment):
    """HTML fragment -> plain text on one line."""
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment))
    return re.sub(r"\s+", " ", text).strip()


def get_course_title(course, year, season):
    """e.g. "BADM 320" -> "Principles of Marketing" (looked up once, then remembered)."""
    if course not in _course_titles:
        subject, number = course.split()
        try:
            xml = requests.get(XML_URL.format(year=year, season=season, subject=subject, number=number),
                               headers=HEADERS, timeout=30).text
            match = re.search(r"<label>(.*?)</label>", xml)
            _course_titles[course] = html_lib.unescape(match.group(1)) if match else ""
        except requests.RequestException:
            return ""
    return _course_titles[course]


def get_section_statuses(course, year, season):
    """Return {crn: section_info} for a course, where section_info is a dict with
    status, type, section, time, days, location, instructor and details (all the row's text)."""
    subject, number = course.split()
    url = PAGE_URL.format(year=year, season=season, subject=subject, number=number)
    page = requests.get(url, headers=HEADERS, timeout=30).text
    sections = {}
    for row in page.split("<tr")[1:]:
        status = re.search(r'aria-label="Section ([^"]+)"', row)
        crn = re.search(r"<div>(\d{5})</div>", row)
        if not (status and crn):
            continue
        cells = [clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        # table columns: ..., CRN, Type, Section, Time, Day(s), Location, Instructor, ...
        i = cells.index(crn.group(1)) if crn.group(1) in cells else -1
        col = lambda k: cells[i + k] if i >= 0 and i + k < len(cells) else ""
        sections[int(crn.group(1))] = {
            "status": status.group(1),
            "type": col(1),
            "section": col(2),
            "time": col(3),
            "days": col(4),
            "location": col(5),
            "instructor": col(6),
            "details": clean(row),
            "url": url,
        }
    return sections


def alert(heading, lines, url):
    """Send the alert to your phone and, when running on a Mac, show a pop-up."""
    send_to_phone(heading, lines, url)
    if sys.platform == "darwin":
        show_mac_popup(heading, lines, url)


def send_to_phone(heading, lines, url):
    """Push notification through ntfy. Tapping it opens the registration page."""
    if not PHONE_TOPIC:
        print("    (no ntfy topic set, so no phone notification)")
        return
    try:
        requests.post("https://ntfy.sh", timeout=30, json={
            "topic": PHONE_TOPIC,
            "title": heading,
            "message": "\n".join(line for line in lines if line),
            "priority": 5,  # max priority: shows up even on a busy lock screen
            "tags": ["rotating_light"],
            "click": REGISTER_URL,
            "actions": [
                {"action": "view", "label": "Register", "url": REGISTER_URL},
                {"action": "view", "label": "Course Explorer", "url": url},
            ],
        }).raise_for_status()
    except requests.RequestException as e:
        print(f"    !!! couldn't send phone notification ({e})")


def show_mac_popup(heading, lines, url):
    """Pop-up window that stays until you click it. Runs in the background
    so the checker keeps going while the pop-up is open."""
    script = [
        "on run argv",
        "set r to display dialog (item 2 of argv) with title (item 1 of argv) "
        'buttons {"OK", "Course Explorer", "Register"} default button "Register" with icon caution',
        'if button returned of r is "Register" then open location (item 4 of argv)',
        'if button returned of r is "Course Explorer" then open location (item 3 of argv)',
        "end run",
    ]
    args = ["osascript"]
    for line in script:
        args += ["-e", line]
    subprocess.Popen(args + [heading, "\n".join(lines), url, REGISTER_URL],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def describe(name, crn, info, title):
    """The text shown in the pop-up for one section."""
    lines = [f"{name}{' – ' + title if title else ''}"]
    kind = " ".join(x for x in (info["type"], info["section"]) if x)
    lines.append(f"{kind} · CRN {crn}" if kind else f"CRN {crn}")
    when = " ".join(x for x in (info["days"], info["time"]) if x and x != "n.a.")
    if when and "ARRANGED" not in when:
        lines.append(when)
    if info["instructor"]:
        lines.append(f"Instructor: {info['instructor']}")
    lines.append("")
    lines.append(f"Status: {info['status']}")
    return lines


year = 2027
season = "spring"  # "spring", "fall", "summer" or "winter"

# double and triple check that the CRN is correct
# enter the the courses in this format ("SUBJECT NUMBER", CRN)
# EX: courses_you_want  = [("CS 233",63735), ("CS 233", 63733), ("CS 361", 66305)]
courses_you_want = [("ADV 360", 75532), ("ADV 360", 75533), ("PLPA 200", 37737)]

# Restrictions that block you from a section. For these CRNs you're only notified
# once the section is open AND this text is gone from its Course Explorer listing.
# EX: blocking_restrictions = {55843: "Restricted to Gies College of Business"}
blocking_restrictions = {}

# --once mode remembers which sections it already notified you about in this file,
# so you get one alert when a section opens instead of one every check.
STATE_FILE = os.path.join(HERE, "notified.json")


def check(already_notified):
    """Check every section once, alerting about newly opened ones.
    already_notified (a set of CRNs) is updated in place."""
    print(time.strftime("%Y-%m-%d %H:%M:%S %Z"), "checking...")
    for course_name in sorted({name for name, _ in courses_you_want}):
        try:
            sections = get_section_statuses(course_name, year, season)
        except requests.RequestException as e:
            print(f"  {course_name}: couldn't reach course explorer ({e}), will retry")
            continue

        for name, crn in courses_you_want:
            if name != course_name:
                continue
            if crn not in sections:
                print(f"  {name} CRN {crn}: NOT FOUND - double check the CRN and semester")
                continue
            info = sections[crn]
            status = info["status"]

            restriction = blocking_restrictions.get(crn)
            still_blocked = restriction is not None and restriction.lower() in info["details"].lower()
            label = f"  {name} {info['section']} (CRN {crn}): {status}"
            print(label + (f" - still '{restriction}'" if still_blocked else ""))

            if status.lower().startswith("open") and not still_blocked:
                # only notify once each time a section opens, not every check
                if crn not in already_notified:
                    lines = describe(name, crn, info, get_course_title(name, year, season))
                    if restriction:
                        lines.append(f"No longer “{restriction}”!")
                    alert(f"{name} {info['section']} is OPEN", lines, info["url"])
                    print(f"    >>> alert sent for {name} CRN {crn}")
                    already_notified.add(crn)
            else:
                already_notified.discard(crn)


# Press the green button in the gutter to run the script.
if __name__ == '__main__':
    if "--once" in sys.argv:
        try:
            with open(STATE_FILE) as f:
                notified = set(json.load(f))
        except (OSError, ValueError):
            notified = set()
        check(notified)
        with open(STATE_FILE, "w") as f:
            json.dump(sorted(notified), f)
    else:
        notified = set()
        while (True):
            check(notified)
            # Enter the time interval that the code searches for the class in second
            # 4 hours = 60 * 60 * 4
            # 1 hour = 60 * 60
            # 5 minute = 60 * 5
            # 10 sec = 10
            time.sleep(60 * 5)
