import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re

GOOGLE_SHEET_NAME = "Ninja_Rank_Up_Output"
st.set_page_config(page_title="Ninja Rank Up Processor 4.4", page_icon="star", layout="wide")

# Stage is read ONLY from section/table headers ("Stage N"/"Level N"), never from
# a skill name. Skill names contain tokens like "S3"/"S2"; matching a bare "s" against
# them mis-filed skills into the wrong stage (the 4.2 "1 skill away" bug).
EVAL_STAGE_RE = re.compile(r'\b(?:stage|level)\s*0*(\d+)\b', re.IGNORECASE)
ROLL_STAGE_RE = re.compile(r'\b(?:stage|level|s)[-\s]?0*(\d+)\b', re.IGNORECASE)
DAY_ORDER = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


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
    sort_time normalizes afternoon hours so a day sorts 1:10, 2:20, 3:40, 4:50, 6:00."""
    if not isinstance(class_name, str) or class_name == "Not Found":
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
        name_idx, key_idx, age_idx = 1, 4, 3
        for i, h in enumerate(headers):
            if "student name" in h:
                name_idx = i
            elif "keyword" in h:
                key_idx = i
            elif "age" in h:
                age_idx = i
        for row in rows[1:]:
            cols = [c for c in row.find_all(['td', 'th']) if c.find_parent('tr') == row]

            def get_val(i):
                return cols[i].get_text(separator=" ", strip=True) if i < len(cols) else ""

            raw_name = get_val(name_idx)
            keywords_raw = get_val(key_idx).lower()
            age_raw = get_val(age_idx)
            gm = re.search(r'(group\s*[1-3])', keywords_raw)
            ck = gm.group(0).capitalize() if gm else "No Group"
            if raw_name and len(raw_name) > 1:
                data.append({"Student Name": clean_name(raw_name), "Group": ck, "Age": age_raw})
    df = pd.DataFrame(data)
    if not df.empty:
        df = df.drop_duplicates(subset=["Student Name"])
    return df


def parse_skill_evals_v5(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    student_evals = {}
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
                inc = is_skill_incomplete(scores[idx].get_text(separator=" ", strip=True))
                student_evals.setdefault(s_name, {}).setdefault(row_stage, {'total': 0, 'incomplete': 0})
                student_evals[s_name][row_stage]['total'] += 1
                if inc:
                    student_evals[s_name][row_stage]['incomplete'] += 1
    return student_evals


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
    df = df.sort_values(by=['Sort Day Num', 'Sort Time', 'Incomplete'])
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


st.title("Ninja Rank Up Processor 4.4")
st.write("Upload all three files to flag students who have completed their target stage.")
c1, c2, c3 = st.columns(3)
with c1:
    file_roll = st.file_uploader("1. Roll Sheet", type=['html', 'htm'])
with c2:
    file_list = st.file_uploader("2. Student List", type=['html', 'htm'])
with c3:
    file_eval = st.file_uploader("3. Skill Evaluation", type=['html', 'htm'])

if file_roll and file_list and file_eval:
    content_roll = file_roll.read().decode("utf-8", errors='ignore')
    content_list = file_list.read().decode("utf-8", errors='ignore')
    content_eval = file_eval.read().decode("utf-8", errors='ignore')
    st.divider()
    with st.spinner('Parsing and Cross-Referencing Stages...'):
        try:
            df_roll = parse_roll_sheet(content_roll)
            df_list = parse_student_list(content_list)
            evals_dict = parse_skill_evals_v5(content_eval)
            merged_df = pd.merge(df_roll, df_list, on="Student Name", how="outer")
            results = []
            for s_name, stages in evals_dict.items():
                student_info = merged_df[merged_df['Student Name'] == s_name]
                last_passed, group, class_name, age = 0, "No Group", "Unknown Class", ""
                if not student_info.empty:
                    row = student_info.iloc[0]
                    last_passed = int(row.get('Current Level', 0)) if pd.notna(row.get('Current Level')) else 0
                    group = row.get('Group', 'No Group')
                    class_name = row.get('Class Name', 'Unknown Class')
                    age = extract_digits(row.get('Age', ''))
                best_status, best_inc = None, 999
                for target_lvl in sorted(stages.keys()):
                    if last_passed > 0 and target_lvl <= last_passed:
                        continue
                    ed = stages[target_lvl]
                    total, inc = ed['total'], ed['incomplete']
                    if total > 0:
                        if inc == 0:
                            best_status = f"Stage {target_lvl} complete (not marked)"
                            best_inc = 0
                            break
                        elif inc <= 2 and best_status is None:
                            best_status = (f"1 skill away (Stage {target_lvl})" if inc == 1
                                           else f"{inc} skills away (Stage {target_lvl})")
                            best_inc = inc
                if best_status:
                    age_str = f" ({age})" if age else ""
                    day, day_num, sort_time, _ = parse_class_info(class_name)
                    results.append({"Student Name": f"{s_name}{age_str}", "Group": group,
                                    "Class Name": class_name, "Status": best_status,
                                    "Sort Day": day, "Sort Day Num": day_num,
                                    "Sort Time": sort_time, "Incomplete": best_inc})
            final_df = pd.DataFrame(results)
            if final_df.empty:
                st.warning("No students met the criteria to rank up.")
            else:
                final_df = final_df.drop_duplicates()
                # Order: weekday (Mon->Sun), then time of day, then closeness to ranking up.
                final_df = final_df.sort_values(by=['Sort Day Num', 'Sort Time', 'Incomplete'])
                st.success(f"Found {len(final_df)} students ready or nearly ready to rank up!")
                st.dataframe(final_df[["Student Name", "Group", "Class Name", "Status"]], use_container_width=True)
                if st.button("Update Master Google Sheet", use_container_width=True):
                    link = export_to_google_sheets(final_df)
                    if link:
                        st.success("Google Sheet Updated Successfully!")
                        style = "background-color:#0083B8;color:white;padding:10px;text-decoration:none;border-radius:5px;display:inline-block;"
                        st.markdown(f'<a href="{link}" target="_blank" style="{style}">OPEN GOOGLE SHEET</a>', unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Detailed Error: {e}")
