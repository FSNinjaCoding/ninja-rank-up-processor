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

# Attendance since last level passed. A student who has passed Stage N and has
# attended MORE than this many classes since is taking longer than expected.
# Key 0 covers students who have not passed any stage yet.
ATTENDANCE_THRESHOLDS = {0: 12, 1: 12, 2: 16, 3: 20, 4: 24, 5: 28, 6: 32, 7: 36, 8: 40, 9: 44, 10: 48}

# How far back a skill mark has to fall to count as recent. The attendance CSV's own
# date range defines the window; without it, this many days back from the eval report.
SIGNOFF_WINDOW_DAYS = 30

# Group 1 sits above Group 2 above Group 3 inside each class time slot.
GROUP_ORDER = {"Group 1": 1, "Group 2": 2, "Group 3": 3}
GROUP_ORDER_DEFAULT = 4

# Students on the student list but not on any roll sheet have no class to act on,
# so they are left out of the report. Flip to False to include them.
REQUIRE_CLASS = True

# Row ordering inside a single class time slot.
PRIORITY_COMPLETE = 0
PRIORITY_ONE_AWAY = 1
PRIORITY_STRUGGLING = 2
PRIORITY_NO_ATTENDANCE = 3


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


def parse_attendance_csv(file_obj):
    """iClassPro's Student Attendance Report: Student, Date, Timeslot, Event Name,
    Status, Excused - one row per student per class occurrence. Returns
    ({student: [dates present]}, window_start, window_end). 'Present (left early)'
    counts as present; absences are ignored."""
    df = pd.read_csv(file_obj, encoding='utf-8-sig')
    cols = {c.strip().lower(): c for c in df.columns}
    name_col = cols.get('student')
    date_col = cols.get('date')
    status_col = cols.get('status')
    if not (name_col and date_col and status_col):
        return {}, None, None
    df = df[df[status_col].astype(str).str.strip().str.lower().str.startswith('present')].copy()
    df['_date'] = pd.to_datetime(df[date_col], format='%m/%d/%Y', errors='coerce').dt.date
    df = df[df['_date'].notna()]
    if df.empty:
        return {}, None, None
    df['_name'] = df[name_col].map(clean_name)
    attended = {n: sorted(g['_date']) for n, g in df.groupby('_name')}
    return attended, df['_date'].min(), df['_date'].max()


def parse_roll_sheet(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    data = []
    headers = soup.find_all('div', class_='full-width-header')
    if not headers:
        return pd.DataFrame()
    for header in headers:
        ns = header.find('span')
        cnr = ns.get_text(strip=True) if ns else header.get_text(separator=" ", strip=True)
        ccn = abbreviate_class_name(cnr)
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
    df = pd.DataFrame(data)
    if not df.empty:
        df = df.sort_values('Current Level', ascending=False).drop_duplicates(subset=["Student Name"], keep='first')
    return df


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
    """Flags a student who has attended more classes since passing their last stage
    than the stage's expected pace. Returns (status_text, classes_over_threshold).
    Note: iClassPro leaves the attendance column blank for students who have not
    passed a stage yet, so the stage-0 threshold only fires if that count is filled in."""
    threshold = ATTENDANCE_THRESHOLDS.get(last_passed)
    if threshold is None or attendance is None:
        return None, 0
    if attendance > threshold:
        label = "class" if attendance == 1 else "classes"
        stage_label = f"Stage {last_passed}" if last_passed else "starting"
        return f"Overdue - {attendance} {label} since {stage_label}", attendance - threshold
    return None, 0


def evaluate_progress_status(last_passed, signoff_info, attended, window_start, has_attendance_data):
    """Every student, every stage, same test: was any skill marked - a 1, 2 or 3 -
    inside the window? If yes, nothing to flag. If no, the flag depends on whether
    they were even here: a student who attended is not getting evaluated, a student
    who did not attend has stopped coming.

    Exception: a student who has never passed a stage AND has not attended is a signup
    that never got going, not a student who stalled - they are left off the report
    entirely. Returns (status_text, priority, severity).
    """
    last_mark = signoff_info.get('last_mark') if signoff_info else None
    if last_mark and last_mark >= window_start:
        return None, None, 0
    if has_attendance_data and not attended:
        if not last_passed:
            return None, None, 0
        return "No attendance in last 30 days", PRIORITY_NO_ATTENDANCE, 0
    classes = len(attended) if attended else 0
    label = "class" if classes == 1 else "classes"
    note = f" ({classes} {label} attended)" if classes else ""
    return f"No skill updates in last 30 days{note}", PRIORITY_STRUGGLING, classes


def build_results(df_roll, df_list, evals_dict, signoff_dict=None, report_date=None,
                  attendance_map=None, window_start=None):
    signoff_dict = signoff_dict or {}
    report_date = report_date or date.today()
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
            last_passed, signoff_dict.get(s_name), attended, window_start, attendance_map is not None)
        if not rank_status and not att_status and not sign_status:
            continue
        if REQUIRE_CLASS and class_name == "Unknown Class":
            continue

        # One row per student; every flag that fires is joined into the one status.
        status = " | ".join(p for p in (rank_status, att_status, sign_status) if p)
        priority = rank_priority if rank_priority is not None else (
            sign_priority if sign_priority is not None else PRIORITY_STRUGGLING)
        age_str = f" ({age})" if age else ""
        day, day_num, sort_time, _ = parse_class_info(class_name)
        results.append({"Student Name": f"{s_name}{age_str}", "Group": group,
                        "Class Name": class_name, "Status": status,
                        "Sort Day": day, "Sort Day Num": day_num, "Sort Time": sort_time,
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


def export_to_google_sheets(df):
    if "gcp_service_account" not in st.secrets:
        st.error("Secrets not found!")
        return None
    creds_dict = st.secrets["gcp_service_account"]
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    try:
        ss = client.open(GOOGLE_SHEET_NAME)
    except Exception as e:
        st.error(f"Could not open sheet. Error: {e}")
        return None
    export_df = df[["Student Name", "Group", "Class Name", "Status"]]
    try:
        ws = ss.worksheet("Rank Up Flags")
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title="Rank Up Flags", rows=100, cols=10)
    data_matrix = [export_df.columns.values.tolist()] + export_df.values.tolist()
    ws.update(range_name="A1", values=data_matrix)
    ws.format("A1:D1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}})
    return f"https://docs.google.com/spreadsheets/d/{ss.id}"


st.set_page_config(page_title="Ninja Rank Up Processor 5.2", page_icon="star", layout="wide")
st.title("Ninja Rank Up Processor 5.2")
st.write("Upload the three iClassPro reports to flag students who are ready to rank up or falling behind. "
         "The attendance CSV is optional but sharpens the sign-off flag.")
c1, c2, c3, c4 = st.columns(4)
with c1:
    file_roll = st.file_uploader("1. Roll Sheet", type=['html', 'htm'])
with c2:
    file_list = st.file_uploader("2. Student List", type=['html', 'htm'])
with c3:
    file_eval = st.file_uploader("3. Skill Evaluation", type=['html', 'htm'])
with c4:
    file_att = st.file_uploader("4. Attendance CSV (optional)", type=['csv'])

if file_roll and file_list and file_eval:
    content_roll = file_roll.read().decode("utf-8", errors='ignore')
    content_list = file_list.read().decode("utf-8", errors='ignore')
    content_eval = file_eval.read().decode("utf-8", errors='ignore')
    st.divider()
    with st.spinner('Parsing and Cross-Referencing Stages...'):
        try:
            df_roll = parse_roll_sheet(content_roll)
            df_list = parse_student_list(content_list)
            evals_dict, signoff_dict = parse_skill_evals_v5(content_eval)
            report_date = parse_report_date(content_eval)
            attendance, window_start, window_end = None, None, None
            if file_att is not None:
                attendance, window_start, window_end = parse_attendance_csv(file_att)
                if not attendance:
                    st.warning("Could not read the attendance CSV - falling back to day-based sign-off windows.")
                    attendance, window_start = None, None
            final_df = build_results(df_roll, df_list, evals_dict, signoff_dict,
                                     report_date, attendance, window_start)
            if final_df.empty:
                st.warning("No students met the criteria to rank up.")
            else:
                ready = int((final_df['Priority'] == PRIORITY_COMPLETE).sum())
                close = int((final_df['Priority'] == PRIORITY_ONE_AWAY).sum())
                behind = int(final_df["Status"].str.contains("Overdue").sum())
                stale = int(final_df["Status"].str.contains("No skill updates").sum())
                gone = int(final_df["Status"].str.contains("No attendance").sum())
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
                           f"{stale} with no skill updates, {gone} not attending.")
                st.dataframe(final_df[["Student Name", "Group", "Class Name", "Status"]], use_container_width=True)
                if st.button("Update Master Google Sheet", use_container_width=True):
                    link = export_to_google_sheets(final_df)
                    if link:
                        st.success("Google Sheet Updated Successfully!")
                        style = "background-color:#0083B8;color:white;padding:10px;text-decoration:none;border-radius:5px;display:inline-block;"
                        st.markdown(f'<a href="{link}" target="_blank" style="{style}">OPEN GOOGLE SHEET</a>', unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Detailed Error: {e}")
