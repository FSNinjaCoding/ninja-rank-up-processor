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
    if any(mark in text for mark in ["pass", "complete"]):
        return False
    return True


def parse_pasted_names(text):
    """A pasted list of students, one per line, run through the same name cleanup as
    every report so 'Smith, Jane' and 'Jane Smith' both land on the same student."""
    if not text:
        return set()
    return {clean_name(line) for line in str(text).splitlines() if clean_name(line)}


def parse_attendance_csv(file_obj):
    """iClassPro's Student Attendance Report: Student, Date, Timeslot, Event Name,
    Status, Excused - one row per student per class occurrence. Returns
    ({student: [dates present]}, {student: {class: (times present, last date)}},
    {student: [dates absent]}, window_start, window_end). 'Present (left early)' counts as present; absences are
    ignored. The per-class counts are what decide a student's home class when they
    show up on more than one roster."""
    df = pd.read_csv(file_obj, encoding='utf-8-sig')
    cols = {c.strip().lower(): c for c in df.columns}
    name_col = cols.get('student')
    date_col = cols.get('date')
    status_col = cols.get('status')
    event_col = cols.get('event name')
    if not (name_col and date_col and status_col):
        return {}, {}, {}, None, None
    df = df.copy()
    df['_date'] = pd.to_datetime(df[date_col], format='%m/%d/%Y', errors='coerce').dt.date
    df = df[df['_date'].notna()]
    if df.empty:
        return {}, {}, {}, None, None
    df['_status'] = df[status_col].astype(str).str.strip().str.lower()
    absent_rows = df[df['_status'].str.startswith('absent')].copy()
    df = df[df['_status'].str.startswith('present')].copy()
    if df.empty:
        return {}, {}, {}, None, None
    df['_name'] = df[name_col].map(clean_name)
    attended = {n: sorted(g['_date']) for n, g in df.groupby('_name')}
    by_class = {}
    if event_col:
        df['_class'] = df[event_col].map(abbreviate_class_name)
        for (n, c), g in df.groupby(['_name', '_class']):
            by_class.setdefault(n, {})[c] = (len(g), max(g['_date']))
    absent_rows['_name'] = absent_rows[name_col].map(clean_name)
    absences = {n: sorted(g['_date']) for n, g in absent_rows.groupby('_name')}
    return attended, by_class, absences, df['_date'].min(), df['_date'].max()


def is_skill_class(class_name):
    lowered = str(class_name).lower()
    return not any(p in lowered for p in NON_SKILL_CLASS_PATTERNS)


def parse_roll_sheet(html_content):
    """Returns (rows, saw_details_column). One row per student PER CLASS - a student
    on two rosters appears twice and is resolved later. The flag matters because the
    Details column is the only place the roll sheet carries the last stage passed; an
    export without it silently makes every student look like Stage 0."""
    soup = BeautifulSoup(html_content, 'lxml')
    data = []
    saw_details = False
    headers = soup.find_all('div', class_='full-width-header')
    if not headers:
        return pd.DataFrame(), saw_details
    for header in headers:
        ns = header.find('span')
        cnr = ns.get_text(strip=True) if ns else header.get_text(separator=" ", strip=True)
        ccn = abbreviate_class_name(cnr)
        if not is_skill_class(cnr):
            continue
        table = header.find_next('table', class_='table-roll-sheet')
        if not table:
            continue
        rows = table.find_all('tr')
        if not rows:
            continue
        first = [c.get_text(strip=True).lower() for c in rows[0].find_all(['td', 'th'])]
        name_idx, details_idx = -1, -1
        for idx, ct in enumerate(first):
            if "student" in ct:
                name_idx = idx
            if "detail" in ct:
                details_idx = idx
                saw_details = True
        if name_idx == -1:
            name_idx = 1
        for row in rows[1:]:
            cols = row.find_all(['td', 'th'])
            if len(cols) <= name_idx:
                continue
            raw_name = cols[name_idx].get_text(strip=True)
            skill_level = 0
            if details_idx != -1 and details_idx < len(cols):
                detail_text = cols[details_idx].get_text(separator=" ", strip=True)
            else:
                detail_text = row.get_text(separator=" ", strip=True)
            sm = ROLL_STAGE_RE.search(detail_text)
            if sm:
                skill_level = int(sm.group(1))
            if raw_name and len(raw_name) > 1 and "student" not in raw_name.lower():
                data.append({"Student Name": clean_name(raw_name),
                             "Current Level": skill_level, "Class Name": ccn})
    return pd.DataFrame(data), saw_details


def resolve_student_classes(df_roll, class_attendance=None):
    """One row per student. A student on more than one roster - a makeup, a second
    class, a leftover enrollment - is filed under the class they actually attend most
    in the attendance window, falling back to the most recent attendance and then to
    document order. The stage kept is the highest seen on any roster."""
    if df_roll.empty:
        return df_roll
    class_attendance = class_attendance or {}
    rows = []
    for name, group in df_roll.groupby("Student Name", sort=False):
        level = int(group["Current Level"].max())
        if len(group) == 1:
            chosen = group.iloc[0]["Class Name"]
        else:
            seen = class_attendance.get(name, {})

            def rank(class_name):
                count, last = seen.get(class_name, (0, None))
                return (count, last or date.min)

            chosen = max(list(group["Class Name"]), key=rank)
        rows.append({"Student Name": name, "Current Level": level, "Class Name": chosen})
    return pd.DataFrame(rows)


def parse_student_list(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    data = []
    for table in soup.find_all('table'):
        if table.find('table'):
            continue
        rows = [r for r in table.find_all('tr') if r.find_parent('table') == table]
        if not rows:
            continue
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(['td', 'th']) if c.find_parent('tr') == rows[0]]
        name_idx, key_idx, age_idx, att_idx = 1, 4, 3, 2
        for i, h in enumerate(headers):
            if "student name" in h:
                name_idx = i
            elif "keyword" in h:
                key_idx = i
            elif "age" in h:
                age_idx = i
            elif "attendance" in h:
                att_idx = i
        for row in rows[1:]:
            cols = [c for c in row.find_all(['td', 'th']) if c.find_parent('tr') == row]

            def get_val(i):
                return cols[i].get_text(separator=" ", strip=True) if i < len(cols) else ""

            raw_name = get_val(name_idx)
            keywords_raw = get_val(key_idx).lower()
            age_raw = get_val(age_idx)
            att_raw = get_val(att_idx)
            gm = re.search(r'(group\s*[1-3])', keywords_raw)
            ck = gm.group(0).capitalize() if gm else "No Group"
            if raw_name and len(raw_name) > 1:
                data.append({"Student Name": clean_name(raw_name), "Group": ck,
                             "Age": age_raw, "Attendance": att_raw})
    df = pd.DataFrame(data)
    if not df.empty:
        df = df.drop_duplicates(subset=["Student Name"])
    return df


def parse_skill_evals_v5(html_content):
    """Returns ({student: {stage: {'total': n, 'incomplete': n}}},
                {student: {'last_signoff': date|None, 'last_mark': date|None}}).

    Skills are collected per (student, stage) keyed by SKILL NAME, so a student who
    shows up on two eval printouts (two classes) is not counted twice. If the same
    skill is marked complete on one sheet and blank on another, the completion wins.

    Every marked cell carries a timestamp, so the newest one doubles as the date the
    student last had something signed off: 'last_signoff' tracks full 3s only,
    'last_mark' tracks any score so a student with only 1s and 2s still has a clock.
    """
    soup = BeautifulSoup(html_content, 'lxml')
    skill_map = {}
    signoff_map = {}
    current_global_stage = None
    for element in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'div', 'table']):
        if element.name != 'table':
            text = element.get_text(separator=" ", strip=True)
            if len(text) < 150:
                m = EVAL_STAGE_RE.search(text)
                if m:
                    current_global_stage = int(m.group(1))
            continue
        table = element
        table_stage = current_global_stage
        rows = table.find_all('tr', recursive=False)
        if not rows:
            rows = [r for r in table.find_all('tr') if r.find_parent('table') == table]
        if len(rows) < 2:
            continue
        for row in rows[:3]:
            m = EVAL_STAGE_RE.search(row.get_text(separator=" ", strip=True))
            if m:
                table_stage = int(m.group(1))
                break
        students, student_row_idx = [], -1
        for i, row in enumerate(rows[:5]):
            cols = [c for c in row.find_all(['td', 'th']) if c.find_parent('tr') == row]
            if len(cols) > 2 and (not cols[0].has_attr('colspan') or int(cols[0].get('colspan', 1)) == 1):
                if re.search(r'[a-zA-Z]', cols[1].get_text(separator=" ", strip=True)):
                    students = [clean_name(c.get_text(separator=" ", strip=True)) for c in cols[1:]]
                    if any(len(s) > 2 for s in students):
                        student_row_idx = i
                        break
        if not students or student_row_idx == -1:
            continue
        current_stage = table_stage
        for row in rows[student_row_idx + 1:]:
            cols = [c for c in row.find_all(['td', 'th']) if c.find_parent('tr') == row]
            if not cols:
                continue
            if len(cols) == 1:
                m = EVAL_STAGE_RE.search(cols[0].get_text(separator=" ", strip=True))
                if m:
                    current_stage = int(m.group(1))
                continue
            skill_name = cols[0].get_text(separator=" ", strip=True)
            sl = skill_name.lower()
            skip = ['total', 'average', 'overall', 'score', 'printout', 'date', 'passed', 'note', 'comment']
            if not sl or any(w in sl for w in skip):
                continue
            # Stage comes ONLY from the header (current_stage), never the skill name.
            row_stage = current_stage
            if row_stage is None:
                continue
            scores = cols[1:]
            for idx, s_name in enumerate(students):
                if not s_name or idx >= len(scores):
                    continue
                cell_text = scores[idx].get_text(separator=" ", strip=True)
                complete = not is_skill_incomplete(cell_text)
                skills = skill_map.setdefault(s_name, {}).setdefault(row_stage, {})
                skills[sl] = skills.get(sl, False) or complete
                seen = signoff_map.setdefault(s_name, {'last_signoff': None, 'last_mark': None})
                score, marked_on = parse_cell_score_date(cell_text)
                if marked_on:
                    if seen['last_mark'] is None or marked_on > seen['last_mark']:
                        seen['last_mark'] = marked_on
                    if score == 3 and (seen['last_signoff'] is None or marked_on > seen['last_signoff']):
                        seen['last_signoff'] = marked_on
    student_evals = {}
    for s_name, stages in skill_map.items():
        for stage, skills in stages.items():
            student_evals.setdefault(s_name, {})[stage] = {
                'total': len(skills),
                'incomplete': sum(1 for done in skills.values() if not done)}
    return student_evals, signoff_map


def evaluate_rank_status(stages, last_passed):
    """First stage above the student's current level that is finished but unmarked,
    otherwise the first one that is exactly one skill short. Two or more skills
    short is no longer reported."""
    if not stages:
        return None, None
    best_status, best_priority = None, None
    for target_lvl in sorted(stages.keys()):
        if last_passed > 0 and target_lvl <= last_passed:
            continue
        ed = stages[target_lvl]
        if ed['total'] <= 0:
            continue
        if ed['incomplete'] == 0:
            return f"Stage {target_lvl} complete (not marked)", PRIORITY_COMPLETE
        if ed['incomplete'] == 1 and best_status is None:
            best_status, best_priority = f"1 skill away (Stage {target_lvl})", PRIORITY_ONE_AWAY
    return best_status, best_priority


def evaluate_attendance_status(last_passed, attendance):
    """Flags a student who has attended more classes than the goal for the stage they
    are working toward. Returns (status_text, classes_over_goal).
    Note: iClassPro leaves the attendance column blank for students who have not passed
    a stage yet, so the Stage 1 goal only fires once that count is filled in."""
    goal = ATTENDANCE_GOALS.get(last_passed)
    if goal is None or attendance is None:
        return None, 0
    if attendance > goal:
        over = attendance - goal
        label = "class" if over == 1 else "classes"
        return f"+{over} {label} over (Stage {last_passed + 1} Goal = {goal})", over
    return None, 0


def evaluate_progress_status(signoff_info, attended, window_start, has_attendance_data):
    """Every student, every stage, same test: was any skill marked - a 1, 2 or 3 -
    inside the window? If yes, nothing to flag. If no, the flag depends on whether
    they were even here: a student who attended is not getting evaluated, a student
    who did not attend has stopped coming.

    A student with fewer than MIN_CLASSES_FOR_UPDATE_FLAG classes in the window is left
    off entirely - too few classes to expect an evaluation, which also covers students
    who stopped coming or never got going.
    Returns (status_text, priority, severity).
    """
    last_mark = signoff_info.get('last_mark') if signoff_info else None
    if last_mark and last_mark >= window_start:
        return None, None, 0
    classes = len(attended) if attended else 0
    if has_attendance_data and classes < MIN_CLASSES_FOR_UPDATE_FLAG:
        return None, None, 0
    label = "class" if classes == 1 else "classes"
    note = f" ({classes} {label})" if classes else ""
    return f"Last Skill {SIGNOFF_WINDOW_DAYS}+ days{note}", PRIORITY_STRUGGLING, classes


def build_results(df_roll, df_list, evals_dict, signoff_dict=None, report_date=None,
                  attendance_map=None, window_start=None, absence_map=None,
                  window_end=None, no_photo=None):
    signoff_dict = signoff_dict or {}
    absence_map = absence_map or {}
    no_photo = no_photo or set()
    report_date = report_date or date.today()
    # "Week prior" is the last 7 days of the attendance window, which covers one
    # occurrence of a weekly class however far into the week the report is pulled.
    recent_cutoff = (window_end or report_date) - timedelta(days=PRIOR_WEEK_DAYS - 1)
    if window_start is None:
        window_start = report_date - timedelta(days=SIGNOFF_WINDOW_DAYS)
    merged = pd.merge(df_roll, df_list, on="Student Name", how="outer")
    names = list(dict.fromkeys(list(merged["Student Name"]) + list(evals_dict.keys())))
    results = []
    for s_name in names:
        if not isinstance(s_name, str) or not s_name:
            continue
        student_info = merged[merged['Student Name'] == s_name]
        last_passed, group, class_name, age, attendance = 0, "No Group", "Unknown Class", "", None
        if not student_info.empty:
            row = student_info.iloc[0]
            last_passed = int(row.get('Current Level', 0)) if pd.notna(row.get('Current Level')) else 0
            group = row.get('Group', 'No Group') if pd.notna(row.get('Group')) else "No Group"
            class_name = row.get('Class Name', 'Unknown Class') if pd.notna(row.get('Class Name')) else "Unknown Class"
            age = extract_digits(row.get('Age', ''))
            attendance = to_int_or_none(row.get('Attendance', ''))

        rank_status, rank_priority = evaluate_rank_status(evals_dict.get(s_name), last_passed)
        att_status, att_over = evaluate_attendance_status(last_passed, attendance)
        attended = attendance_map.get(s_name, []) if attendance_map is not None else []
        sign_status, sign_priority, days_over = evaluate_progress_status(
            signoff_dict.get(s_name), attended, window_start, attendance_map is not None)
        photo_status = NO_PHOTO_LABEL if s_name in no_photo else None
        if not rank_status and not att_status and not sign_status and not photo_status:
            continue
        if REQUIRE_CLASS and class_name == "Unknown Class":
            continue

        # One row per student. Flags read in fixed order: overdue stage, stale skills,
        # the rank-up note (stage complete, or one skill away), then missing photo.
        status = " | ".join(p for p in (att_status, sign_status, rank_status, photo_status) if p)
        priority = rank_priority if rank_priority is not None else (
            sign_priority if sign_priority is not None else
            (PRIORITY_NO_PHOTO if not att_status else PRIORITY_STRUGGLING))
        age_str = f" ({age})" if age else ""
        day, day_num, sort_time, time_str = parse_class_info(class_name)
        results.append({"Student Name": f"{s_name}{age_str}", "Group": group,
                        "Class Name": class_name, "Status": status,
                        "Sort Day": day, "Sort Day Num": day_num, "Sort Time": sort_time,
                        "Class Time": time_str,
                        "Absent Prior Week": any(d >= recent_cutoff for d in absence_map.get(s_name, [])),
                        "Group Rank": GROUP_ORDER.get(group, GROUP_ORDER_DEFAULT),
                        "Priority": priority, "Days Over": days_over, "Over By": att_over})
    df = pd.DataFrame(results)
    if df.empty:
        return df
    # Order: weekday (Mon->Sun), then time of day, then Group 1/2/3 inside that class,
    # then rank-ups ahead of the behind-pace flags, most stalled first.
    df = df.drop_duplicates()
    return df.sort_values(by=['Sort Day Num', 'Sort Time', 'Group Rank', 'Priority',
                              'Days Over', 'Over By', 'Student Name'],
                          ascending=[True, True, True, True, False, False, True])


def build_day_blocks(df, day_code):
    """Lays out one weekday tab: a header row per class time (day + start time, with
    the three check-off columns), the students in that slot, then a blank spacer.
    Column C reads "Absent last wk" when the student missed their class in the week
    prior - context for why nothing got signed off - and is blank otherwise.
    Two classes running at the same time share one block, which is how the manager
    reads the floor. Returns (values, header_row_indexes, student_row_spans)."""
    day_df = df[df['Sort Day'] == day_code]
    values, header_rows, student_spans = [], [], []
    if day_df.empty:
        return values, header_rows, student_spans
    for _, block in day_df.groupby('Sort Time', sort=True):
        time_str = str(block.iloc[0].get('Class Time') or "").strip()
        label = f"{DAY_TAB_NAMES.get(day_code, day_code)} {time_str}".strip()
        header_rows.append(len(values))
        values.append([label, "", "", ""] + CHECK_COLUMNS)
        first_student = len(values)
        for _, row in block.iterrows():
            values.append([row['Student Name'], row['Group'],
                           PRIOR_ABSENCE_LABEL if row.get('Absent Prior Week') else "",
                           row['Status'], "", "", ""])
        student_spans.append((first_student, len(values) - 1))
        values.append(["", "", "", "", "", "", ""])
    return values, header_rows, student_spans


def _reset_requests(sheet_id):
    """ws.clear() wipes values but leaves last week's merges, grey fills, borders and
    checkboxes behind, so the tab has to be reset properly before it is rewritten."""
    whole_sheet = {"sheetId": sheet_id}
    return [
        {"unmergeCells": {"range": whole_sheet}},
        {"repeatCell": {"range": whole_sheet, "cell": {"userEnteredFormat": {}},
                        "fields": "userEnteredFormat"}},
        {"setDataValidation": {"range": whole_sheet}},
    ]


def _day_tab_requests(sheet_id, values, header_rows, student_spans):
    """Formatting for one weekday tab, as Sheets API requests: reset old formatting,
    merged grey headers, checkboxes under Resolved/Absent/Aware, borders, widths."""
    reqs = []
    for width, start, end in ((190, 0, 1), (90, 1, 2), (95, 2, 3), (430, 3, 4), (70, 4, 7)):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": start, "endIndex": end},
            "properties": {"pixelSize": width}, "fields": "pixelSize"}})
    for r in header_rows:
        row_range = {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                     "startColumnIndex": 0, "endColumnIndex": 7}
        reqs += [
            {"mergeCells": {"range": dict(row_range, endColumnIndex=4), "mergeType": "MERGE_ROWS"}},
            {"repeatCell": {"range": row_range,
                            "cell": {"userEnteredFormat": {
                                "backgroundColor": HEADER_GREY,
                                "horizontalAlignment": "CENTER",
                                "textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,textFormat)"}},
        ]
    for start, end in student_spans:
        block = {"sheetId": sheet_id, "startRowIndex": start, "endRowIndex": end + 1,
                 "startColumnIndex": 0, "endColumnIndex": 7}
        checks = dict(block, startColumnIndex=4, endColumnIndex=7)
        reqs += [
            {"setDataValidation": {"range": checks,
                                   "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True}}},
            {"repeatCell": {"range": checks,
                            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                            "fields": "userEnteredFormat.horizontalAlignment"}},
            # Status can run long, so it reads left aligned like the name and group.
            {"repeatCell": {"range": dict(block, startColumnIndex=0, endColumnIndex=4),
                            "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT"}},
                            "fields": "userEnteredFormat.horizontalAlignment"}},
        ]
    for r in header_rows:
        outline = {"sheetId": sheet_id, "startRowIndex": r, "endRowIndex": r + 1,
                   "startColumnIndex": 0, "endColumnIndex": 7}
        reqs.append({"updateBorders": {"range": outline,
                                       "top": {"style": "SOLID"}, "bottom": {"style": "SOLID"},
                                       "left": {"style": "SOLID"}, "right": {"style": "SOLID"},
                                       "innerVertical": {"style": "SOLID"}}})
    for start, end in student_spans:
        block = {"sheetId": sheet_id, "startRowIndex": start, "endRowIndex": end + 1,
                 "startColumnIndex": 0, "endColumnIndex": 7}
        reqs.append({"updateBorders": {"range": block,
                                       "top": {"style": "SOLID"}, "bottom": {"style": "SOLID"},
                                       "left": {"style": "SOLID"}, "right": {"style": "SOLID"},
                                       "innerHorizontal": {"style": "SOLID"},
                                       "innerVertical": {"style": "SOLID"}}})
    return reqs


def _get_or_create_worksheet(ss, title, rows, cols):
    try:
        ws = ss.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return ss.add_worksheet(title=title, rows=max(rows, 100), cols=max(cols, 8))
    if ws.row_count < rows or ws.col_count < cols:
        ws.resize(rows=max(rows, ws.row_count), cols=max(cols, ws.col_count))
    return ws


def export_day_tabs(ss, df):
    """One tab per weekday that has students, laid out for printing. Returns the
    tab names written."""
    written, requests = [], []
    for code, title in DAY_TAB_NAMES.items():
        values, header_rows, student_spans = build_day_blocks(df, code)
        if not values:
            continue
        ws = _get_or_create_worksheet(ss, title, len(values) + 20, 9)
        # Reset merges/fills/borders/checkboxes from last week. Sent on its own because
        # unmergeCells errors on a tab that has no merges yet - a brand new tab, or one
        # that was rebuilt without them - and that must not take the rest down with it.
        try:
            ss.batch_update({"requests": _reset_requests(ws.id)})
        except Exception:
            pass
        ws.clear()
        ws.update(range_name="A1", values=values)
        requests += _day_tab_requests(ws.id, values, header_rows, student_spans)
        written.append(title)
    if requests:
        ss.batch_update({"requests": requests})
    return written


def export_to_google_sheets(df):
    if "gcp_service_account" not in st.secrets:
        st.error("Secrets not found!")
        return None, [], None
    creds_dict = st.secrets["gcp_service_account"]
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    try:
        ss = client.open(GOOGLE_SHEET_NAME)
    except Exception as e:
        st.error(f"Could not open sheet. Error: {e}")
        return None, [], None
    export_df = df[["Student Name", "Group", "Class Name", "Status"]]
    try:
        ws = ss.worksheet("Rank Up Flags")
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title="Rank Up Flags", rows=100, cols=10)
    data_matrix = [export_df.columns.values.tolist()] + export_df.values.tolist()
    ws.update(range_name="A1", values=data_matrix)
    ws.format("A1:D1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}})
    link = f"https://docs.google.com/spreadsheets/d/{ss.id}"
    # The main tab is already saved at this point. If the day tabs blow up, say so and
    # still hand back the link rather than losing the whole run to an exception.
    try:
        tabs = export_day_tabs(ss, df)
    except Exception as e:
        return link, [], f"Main tab updated, but the day tabs failed: {e}"
    return link, tabs, None


REPORT_HOWTO = {
    "roll": ("Roll Sheet", [
        "Reports (top bar)",
        "Classes (side bar)",
        "CLA-4 Roll Sheets",
        "Group Processor preset",
        "Start / End Date = Mon-Fri of the current week",
        "HTML button at the bottom",
        'Open the HTML page, right click, "Save as" (keep the default name)',
    ]),
    "list": ("Student List", [
        "Reports (top bar)",
        "Students (side bar)",
        "Custom Student List",
        "Group Processor preset",
        "Date = today",
        "HTML button at the bottom",
        'Open the HTML page, right click, "Save as" (keep the default name)',
    ]),
    "eval": ("Skill Evaluation", [
        "Classes (top bar)",
        "Smart Filters: Rank up processor",
        "Select all classes",
        "Generate Report (page icon at the bottom)",
        "Class Evaluation Forms",
        "HTML button at the bottom",
        'Open the HTML page, right click, "Save as" (keep the default name)',
    ]),
    "attendance": ("Attendance", [
        "Students (top bar)",
        "Smart Filters: Rank up processor",
        "Select all students",
        "Generate Report (page icon at the bottom)",
        "Student Attendance Report",
        "Start Date = 30 days before today, End Date = today",
        "CSV button at the bottom",
        "Download CSV",
    ]),
}


def show_howto(key):
    """A click-to-open cheat sheet under each uploader. st.popover needs a recent
    Streamlit, so fall back to an expander rather than breaking the upload screen."""
    title, steps = REPORT_HOWTO[key]
    body = f"**{title} - where to find it in iClassPro**\n\n" + "\n".join(
        f"{i}. {step}" for i, step in enumerate(steps, 1))
    try:
        with st.popover("How to pull this report"):
            st.markdown(body)
    except AttributeError:
        with st.expander("How to pull this report"):
            st.markdown(body)


st.set_page_config(page_title="Ninja Rank Up Processor 6.2", page_icon="star", layout="wide")
st.title("Ninja Rank Up Processor 6.2")
st.write("Upload the iClassPro reports to flag students who are ready to rank up or falling behind.")
c1, c2, c3, c4 = st.columns(4)
with c1:
    file_roll = st.file_uploader("1. Roll Sheet", type=['html', 'htm'])
    show_howto("roll")
with c2:
    file_list = st.file_uploader("2. Student List", type=['html', 'htm'])
    show_howto("list")
with c3:
    file_eval = st.file_uploader("3. Skill Evaluation", type=['html', 'htm'])
    show_howto("eval")
with c4:
    file_att = st.file_uploader("4. Attendance CSV", type=['csv'])
    show_howto("attendance")

def read_upload(uploaded):
    """Streamlit hands back the same file object on every rerun - and clicking a button
    is a rerun. Without the rewind the second read returns nothing, the parse comes back
    empty, and the export silently has no data to write."""
    uploaded.seek(0)
    return uploaded.read()


photo_text = st.text_area(
    "Students missing a profile photo - paste one name per line (optional)",
    height=110, placeholder="Baylor Treme\nBrooks Owen\nEaston Earp")

if file_roll and file_list and file_eval:
    content_roll = read_upload(file_roll).decode("utf-8", errors='ignore')
    content_list = read_upload(file_list).decode("utf-8", errors='ignore')
    content_eval = read_upload(file_eval).decode("utf-8", errors='ignore')
    st.divider()
    with st.spinner('Parsing and Cross-Referencing Stages...'):
        try:
            df_roll_raw, saw_details = parse_roll_sheet(content_roll)
            if not df_roll_raw.empty and not saw_details:
                st.error("This roll sheet has no **Details** column, which is where the last "
                         "stage passed is stored. Every student would come through as no stage, "
                         "so completed stages get reported as 'not marked'. Re-export the roll "
                         "sheet with Details included (the saved Group Processor report).")
                st.stop()
            df_list = parse_student_list(content_list)
            evals_dict, signoff_dict = parse_skill_evals_v5(content_eval)
            report_date = parse_report_date(content_eval)
            attendance, class_attendance, absences = None, {}, {}
            window_start, window_end = None, None
            if file_att is not None:
                file_att.seek(0)
                (attendance, class_attendance, absences,
                 window_start, window_end) = parse_attendance_csv(file_att)
                if not attendance:
                    st.warning("Could not read the attendance CSV - skill updates will be "
                               "checked over the last 30 days without attendance context.")
                    attendance, class_attendance, absences, window_start = None, {}, {}, None
            df_roll = resolve_student_classes(df_roll_raw, class_attendance)
            no_photo = parse_pasted_names(photo_text)
            final_df = build_results(df_roll, df_list, evals_dict, signoff_dict,
                                     report_date, attendance, window_start,
                                     absences, window_end, no_photo)
            if no_photo:
                # A pasted name that matches nobody would silently do nothing, so say so
                # rather than let the manager assume the student was flagged.
                known = set(df_roll["Student Name"]) | set(df_list["Student Name"])
                missing = sorted(n for n in no_photo if n not in known)
                if missing:
                    st.warning("No student found for: " + ", ".join(missing))
            if final_df.empty:
                st.warning("No students met the criteria to rank up.")
            else:
                ready = int((final_df['Priority'] == PRIORITY_COMPLETE).sum())
                close = int((final_df['Priority'] == PRIORITY_ONE_AWAY).sum())
                behind = int(final_df["Status"].str.contains("Overdue").sum())
                stale = int(final_df["Status"].str.contains("Last Skill").sum())
                if window_start:
                    st.caption(f"Eval report date {report_date:%m/%d/%Y} - attendance window "
                               f"{window_start:%m/%d/%Y} to {window_end:%m/%d/%Y}, "
                               f"{len(attendance)} students with at least one class attended.")
                else:
                    st.caption(f"Eval report date {report_date:%m/%d/%Y} - no attendance CSV, "
                               f"skill updates measured over the last {SIGNOFF_WINDOW_DAYS} days "
                               f"and attendance not checked.")
                st.success(f"{len(final_df)} students flagged - {ready} stage complete, "
                           f"{close} one skill away, {behind} overdue on attendance, "
                           f"{stale} with no recent skill update.")
                st.dataframe(final_df[["Student Name", "Group", "Class Name", "Status"]], use_container_width=True)
                if st.button("Update Master Google Sheet", use_container_width=True):
                    link, tabs, warning = export_to_google_sheets(final_df)
                    if link:
                        st.session_state["sheet_link"] = link
                        written = ", ".join(tabs) if tabs else "no day tabs"
                        st.success(f"Google Sheet updated - Rank Up Flags plus {written}.")
                    if warning:
                        st.warning(warning)
                if st.session_state.get("sheet_link"):
                    link = st.session_state["sheet_link"]
                    try:
                        st.link_button("OPEN GOOGLE SHEET", link, use_container_width=True)
                    except AttributeError:
                        style = "background-color:#0083B8;color:white;padding:10px;text-decoration:none;border-radius:5px;display:inline-block;"
                        st.markdown(f'<a href="{link}" target="_blank" style="{style}">OPEN GOOGLE SHEET</a>', unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Detailed Error: {e}")
