import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re
from datetime import date, timedelta

GOOGLE_SHEET_NAME = "Ninja_Rank_Up_Output"

# Stage is read ONLY from section/table headers ("Stage N"/"Level N"), never from
# a skill name. Skill names contain tokens like "S3"/"S2"; matching a bare "s" against
# them mis-filed skills into the wrong stage (the 4.2 "1 skill away" bug).
EVAL_STAGE_RE = re.compile(r'\b(?:stage|level)\s*0*(\d+)\b', re.IGNORECASE)
ROLL_STAGE_RE = re.compile(r'\b(?:stage|level|s)[-\s]?0*(\d+)\b', re.IGNORECASE)
DAY_ORDER = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}

# Classes a student is expected to need to reach their NEXT stage, keyed by the last
# stage they passed. Attending more than this without passing means they are overdue,
# and the goal is named for the stage they are working toward - a student who last
# passed Stage 1 is working on Stage 2 with a goal of 16. Stage 10 is the top of the
# system, so a student who has passed it has no goal and never flags.
ATTENDANCE_GOALS = {0: 12, 1: 16, 2: 20, 3: 24, 4: 28, 5: 32, 6: 36, 7: 40, 8: 44, 9: 48}

# A student needs at least this many classes inside the window before a missing skill
# update counts as a problem - one class is not enough to expect a coach to have
# evaluated them.
MIN_CLASSES_FOR_UPDATE_FLAG = 2

# How far back "the week prior" reaches when marking the absence column on day tabs.
PRIOR_WEEK_DAYS = 7

# How far back a skill mark has to fall to count as recent. The attendance CSV's own
# date range defines the window; without it, this many days back from the eval report.
SIGNOFF_WINDOW_DAYS = 30

# Group 1 sits above Group 2 above Group 3 inside each class time slot.
GROUP_ORDER = {"Group 1": 1, "Group 2": 2, "Group 3": 3}
GROUP_ORDER_DEFAULT = 4

# Students on the student list but not on any roll sheet have no class to act on,
# so they are left out of the report. Flip to False to include them.
REQUIRE_CLASS = True

# Roll sheet exports can be pulled with every program included. Only skill-based
# classes belong in this report - open gym, makeup tokens and team training are not
# where stages get signed off, and a student on those rosters would otherwise get
# filed under the wrong class.
NON_SKILL_CLASS_PATTERNS = ("open gym", "makeup token", "legacy", "ninja team")

# One printable tab per weekday, plus the three sign-off columns the manager checks
# off by hand on the printout.
DAY_TAB_NAMES = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday",
                 "Thu": "Thursday", "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}
CHECK_COLUMNS = ["Resolved", "Absent", "Aware"]
PRIOR_ABSENCE_LABEL = "Absent last wk"

# Pasted straight from iClassPro, one student per line. Reported last on the row.
NO_PHOTO_LABEL = "No Photo"
HEADER_GREY = {"red": 0.85, "green": 0.85, "blue": 0.85}

# Row ordering inside a single class time slot.
PRIORITY_COMPLETE = 0
PRIORITY_ONE_AWAY = 1
PRIORITY_STRUGGLING = 2
PRIORITY_NO_PHOTO = 3


CELL_SCORE_DATE_RE = re.compile(r'(?P<score>\d)\s+(?P<date>\d{4}-\d{2}-\d{2})')
REPORT_DATE_RE = re.compile(r'(\d{1,2})/(\d{1,2})/(\d{4})')


def parse_report_date(html_content):
    """The eval printout is titled 'Skill Evaluation Printout Sheets for 08/11/2026'.
    Sign-off ages are measured from that date so re-running an older export still
    gives the same answers. Falls back to today if the title is missing."""
    m = re.search(r'<title[^>]*>(.*?)</title>', html_content[:20000], re.IGNORECASE | re.DOTALL)
    if m:
        d = REPORT_DATE_RE.search(m.group(1))
        if d:
            try:
                return date(int(d.group(3)), int(d.group(1)), int(d.group(2)))
            except ValueError:
                pass
    return date.today()


def parse_cell_score_date(text):
    """Cells read '3 2026-07-30 18:44:14'. Returns (score, date) or (None, None)."""
    m = CELL_SCORE_DATE_RE.search(str(text))
    if not m:
        return None, None
    try:
        return int(m.group('score')), date.fromisoformat(m.group('date'))
    except ValueError:
        return None, None


def clean_name(name):
    if not isinstance(name, str):
        return ""
    name = re.sub(r'\(\d+\)', '', name)
    name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
    if ',' in name:
        parts = name.split(',')
        if len(parts) == 2:
            name = f"{parts[1].strip()} {parts[0].strip()}"
    name = re.sub(r"[^a-zA-Z\s\-']", '', name)
    return re.sub(r'\s+', ' ', name).strip().title()


def abbreviate_class_name(name):
    if not isinstance(name, str):
        return name
    name = re.sub(r'\d{1,2}/\d{1,2}/\d{4}.*', '', name).strip()
    name = name.replace("Homeschool", "HS")
    name = name.replace("Flip Side Ninjas", "FS Ninjas")
    name = name.replace("(Ages ", "(")
    return name


def parse_class_info(class_name):
    """Returns (day, day_num, sort_time, time_str). day_num orders Mon->Sun;
    sort_time normalizes afternoon hours so a day sorts 1:10, 2:20, 3:40, 4:50, 6:00.
    Anything unparsable sorts to the bottom (day_num 99 / sort_time 9999)."""
    if not isinstance(class_name, str) or class_name in ("Not Found", "Unknown Class"):
        return "Lost", 99, 9999, ""
    dm = re.search(r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b', class_name, re.IGNORECASE)
    day = dm.group(1).title() if dm else "Lost"
    day_num = DAY_ORDER.get(day, 99)
    tm = re.search(r'(\d{1,2}):(\d{2})', class_name)
    if tm:
        hour = int(tm.group(1))
        minute = int(tm.group(2))
        if hour < 8:
            hour += 12
        return day, day_num, hour * 100 + minute, f"{tm.group(1)}:{tm.group(2)}"
    return day, day_num, 9999, ""


def extract_digits(val):
    m = re.search(r'\d+', str(val))
    return m.group() if m else ""


def to_int_or_none(val):
    m = re.search(r'\d+', str(val)) if val is not None else None
    return int(m.group()) if m else None


def is_skill_incomplete(score_text):
    """Complete only if score is 3. Blank/1/2 = incomplete. Cells look like
    '3 2026-04-24 13:35:22'; the leading single digit is the score."""
    text = str(score_text).lower().strip()
    if not text or text == "-" or text.isspace() or "n/a" in text:
        return True
    mf = re.search(r'(\d+)\s*/\s*(\d+)', text)
    if mf:
        return int(mf.group(1)) < int(mf.group(2))
    ms = re.search(r'(?<!/)\b(\d)\b(?!/)', text)
    if ms:
        return int(ms.group(1)) < 3
