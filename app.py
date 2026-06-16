import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re

# --- CONFIGURATION ---
GOOGLE_SHEET_NAME = "Ninja_Rank_Up_Output"
# VERSION UPDATE: 4.3
st.set_page_config(page_title="Ninja Rank Up Processor 4.3", page_icon="⭐", layout="wide")

# =====================================================================
# STAGE DETECTION REGEXES
# ---------------------------------------------------------------------
# THE BUG (fixed in 4.3): the old code used a single regex that treated
# a BARE letter "s" as a stage marker:  (?:stage|level|s)[-\s]?0*(\d+)
# That regex was also run against SKILL NAMES. Skill names legitimately
# contain tokens like "S3", "S2", "2 Hammers", etc.
# e.g. the Stage-4 skill "Grip Alley - S3 + 2 Hammers" contains "S3",
# so it got re-filed under Stage 3. Students who hadn't started Stage 4
# have a BLANK in that cell, so it was counted as 1 incomplete Stage-3
# skill -> a fully-completed Stage 3 showed as "1 skill away".
#
# FIX: a stage/level is ONLY ever read from a SECTION or TABLE HEADER,
# never from a skill name. EVAL_STAGE_RE requires the literal word
# "stage" or "level" (no bare "s"). The roll-sheet "Details" column
# genuinely uses the "S#" abbreviation, so it keeps its own regex.
# =====================================================================
EVAL_STAGE_RE = re.compile(r'\b(?:stage|level)\s*0*(\d+)\b', re.IGNORECASE)
ROLL_STAGE_RE = re.compile(r'\b(?:stage|level|s)[-\s]?0*(\d+)\b', re.IGNORECASE)


# --- HELPER FUNCTIONS ---
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
    if not isinstance(class_name, str) or class_name == "Not Found":
        return "Lost", 9999, ""

    day_match = re.search(r'\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b', class_name, re.IGNORECASE)
    day = day_match.group(1).title() if day_match else "Lost"

    time_match = re.search(r'(\d{1,2}):(\d{2})', class_name)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        if hour < 8:
            hour += 12
        sort_time = hour * 100 + minute
        time_str = f"{time_match.group(1)}:{time_match.group(2)}"
    else:
        sort_time = 9999
        time_str = ""

    return day, sort_time, time_str


def extract_digits(val):
    match = re.search(r'\d+', str(val))
    return match.group() if match else ""


def is_skill_incomplete(score_text):
    """A skill is complete ONLY if its score is 3 (proficient).
    Blank / 1 / 2 = incomplete. Cells carry a trailing timestamp
    (e.g. '3 2026-04-24 13:35:22'); the leading single digit is the score."""
    text = str(score_text).lower().strip()
    if not text or text == "-" or text.isspace() or "n/a" in text:
        return True
    match_frac = re.search(r'(\d+)\s*/\s*(\d+)', text)
    if match_frac:
        num = int(match_frac.group(1))
        den = int(match_frac.group(2))
        return num < den
    match_single = re.search(r'(?<!/)\b(\d)\b(?!/)', text)
    if match_single:
        return int(match_single.group(1)) < 3
    if any(mark in text for mark in ["pass", "✔", "✓", "★", "complete"]):
        return False
    return True


# --- PARSING LOGIC ---
def parse_roll_sheet(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    data = []
    headers = soup.find_all('div', class_='full-width-header')
    if not headers:
        return pd.DataFrame()
    for header in headers:
        name_span = header.find('span')
        class_name_raw = name_span.get_text(strip=True) if name_span else header.get_text(separator=" ", strip=True)
        current_class_name = abbreviate_class_name(class_name_raw)

        table = header.find_next('table', class_='table-roll-sheet')
        if not table:
            continue
        rows = table.find_all('tr')
        if not rows:
            continue

        first_row_cols = [c.get_text(strip=True).lower() for c in rows[0].find_all(['td', 'th'])]
        name_idx = -1
        details_idx = -1
        for idx, col_text in enumerate(first_row_cols):
            if "student" in col_text:
                name_idx = idx
            if "detail" in col_text:
                details_idx = idx
        if name_idx == -1:
            name_idx = 1

        for row in rows[1:]:
            cols = row.find_all(['td', 'th'])
            if len(cols) <= name_idx:
                continue

            raw_name = cols[name_idx].get_text(strip=True)
            skill_level = 0

            # Read the last-completed stage from the "Details" cell if we found it,
            # otherwise fall back to the whole row. "S2" = completed Stage 2.
            if details_idx != -1 and details_idx < len(cols):
                detail_text = cols[details_idx].get_text(separator=" ", strip=True)
            else:
                detail_text = row.get_text(separator=" ", strip=True)
            skill_match = ROLL_STAGE_RE.search(detail_text)
            if skill_match:
                skill_level = int(skill_match.group(1))

            if raw_name and len(raw_name) > 1 and "student" not in raw_name.lower():
                data.append({
                    "Student Name": clean_name(raw_name),
                    "Current Level": skill_level,
                    "Class Name": current_class_name
                })
    df = pd.DataFrame(data)
    if not df.empty:
        df = df.sort_values('Current Level', ascending=False).drop_duplicates(subset=["Student Name"], keep='first')
    return df


def parse_student_list(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    data = []
    tables = soup.find_all('table')

    for table in tables:
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

            group_match = re.search(r'(group\s*[1-3])', keywords_raw)
            clean_keyword = group_match.group(0).capitalize() if group_match else "No Group"
            if raw_name and len(raw_name) > 1:
                data.append({
                    "Student Name": clean_name(raw_name),
                    "Group": clean_keyword,
                    "Age": age_raw
                })

    df = pd.DataFrame(data)
    if not df.empty:
        df = df.drop_duplicates(subset=["Student Name"])
    return df


def parse_skill_evals_v5(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    student_evals = {}
    current_global_stage = None

    elements = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'div', 'table'])

    for element in elements:
        if element.name != 'table':
            text = element.get_text(separator=" ", strip=True)
            if len(text) < 150:
                match = EVAL_STAGE_RE.search(text)
                if match:
                    current_global_stage = int(match.group(1))
        else:
            table = element
            table_stage = current_global_stage

            rows = table.find_all('tr', recursive=False)
            if not rows:
                rows = [r for r in table.find_all('tr') if r.find_parent('table') == table]

            if len(rows) < 2:
                continue

            # Look for an explicit "Stage N" / "Level N" in the first rows of the table.
            for row in rows[:3]:
                match = EVAL_STAGE_RE.search(row.get_text(separator=" ", strip=True))
                if match:
                    table_stage = int(match.group(1))
                    break

            students = []
            student_row_idx = -1
            for i, row in enumerate(rows[:5]):
                cols = [c for c in row.find_all(['td', 'th']) if c.find_parent('tr') == row]
                if len(cols) > 2:
                    if not cols[0].has_attr('colspan') or int(cols[0].get('colspan', 1)) == 1:
                        cell_text = cols[1].get_text(separator=" ", strip=True)
                        if re.search(r'[a-zA-Z]', cell_text):
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

                # A single-cell row is a stage/level sub-header inside the table.
                if len(cols) == 1:
                    match = EVAL_STAGE_RE.search(cols[0].get_text(separator=" ", strip=True))
                    if match:
                        current_stage = int(match.group(1))
                    continue

                if len(cols) > 1:
                    skill_name = cols[0].get_text(separator=" ", strip=True)
                    skill_lower = skill_name.lower()

                    # Ignore metadata / spacer rows.
                    skip_words = ['total', 'average', 'overall', 'score', 'printout', 'date', 'passed', 'note', 'comment']
                    if not skill_lower or any(word in skill_lower for word in skip_words):
                        continue

                    # CRITICAL: the stage for this skill comes ONLY from the section/
                    # table header (current_stage). We DO NOT parse the skill name for a
                    # stage number — skill names contain "S3", "S2", numbers, etc. that
                    # would mis-file the skill into the wrong stage (this was the 4.2 bug).
                    row_stage = current_stage
                    if row_stage is None:
                        continue

                    scores = cols[1:]
                    for idx, s_name in enumerate(students):
                        if not s_name:
                            continue
                        if idx < len(scores):
                            score_text = scores[idx].get_text(separator=" ", strip=True)
                            is_inc = is_skill_incomplete(score_text)

                            if s_name not in student_evals:
                                student_evals[s_name] = {}
                            if row_stage not in student_evals[s_name]:
                                student_evals[s_name][row_stage] = {'total': 0, 'incomplete': 0}

                            student_evals[s_name][row_stage]['total'] += 1
                            if is_inc:
                                student_evals[s_name][row_stage]['incomplete'] += 1

    return student_evals


# --- GOOGLE SHEETS EXPORT ---
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
    df = df.sort_values(by=['Sort Day', 'Sort Time', 'Incomplete'])
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


# --- MAIN UI ---
st.title("⭐ Ninja Rank Up Processor 4.3")
st.write("Upload all three files to flag students who have completed their target stage.")
col1, col2, col3 = st.columns(3)
with col1:
    file_roll = st.file_uploader("1. Roll Sheet", type=['html', 'htm'])
with col2:
    file_list = st.file_uploader("2. Student List", type=['html', 'htm'])
with col3:
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
                last_passed = 0
                group = "No Group"
                class_name = "Unknown Class"
                age = ""

                if not student_info.empty:
                    row = student_info.iloc[0]
                    last_passed = int(row.get('Current Level', 0)) if pd.notna(row.get('Current Level')) else 0
                    group = row.get('Group', 'No Group')
                    class_name = row.get('Class Name', 'Unknown Class')
                    age = extract_digits(row.get('Age', ''))
                best_status = None
                best_inc = 999

                # Check stages strictly sequentially (Stage 1, then Stage 2...)
                for target_lvl in sorted(stages.keys()):
                    if last_passed > 0 and target_lvl <= last_passed:
                        continue

                    eval_data = stages[target_lvl]
                    total = eval_data['total']
                    inc = eval_data['incomplete']

                    if total > 0:
                        # Priority 1: Did they completely finish a stage?
                        if inc == 0:
                            best_status = f"Stage {target_lvl} complete (not marked)"
                            best_inc = 0
                            break  # INSTANT STOP! This prevents higher stages from overwriting it.

                        # Priority 2: Lock in the first level they are close to finishing
                        elif inc <= 2:
                            if best_status is None:
                                if inc == 1:
                                    best_status = f"1 skill away (Stage {target_lvl})"
                                else:
                                    best_status = f"{inc} skills away (Stage {target_lvl})"
                                best_inc = inc

                if best_status:
                    age_str = f" ({age})" if age else ""
                    display_name = f"{s_name}{age_str}"
                    day, sort_time, _ = parse_class_info(class_name)

                    results.append({
                        "Student Name": display_name,
                        "Group": group,
                        "Class Name": class_name,
                        "Status": best_status,
                        "Sort Day": day,
                        "Sort Time": sort_time,
                        "Incomplete": best_inc
                    })

            final_df = pd.DataFrame(results)

            if final_df.empty:
                st.warning("No students met the criteria to rank up.")
            else:
                final_df = final_df.drop_duplicates()
                final_df = final_df.sort_values(by=['Sort Day', 'Sort Time', 'Incomplete'])
                st.success(f"Found {len(final_df)} students ready or nearly ready to rank up!")
                st.dataframe(final_df[["Student Name", "Group", "Class Name", "Status"]], use_container_width=True)

                if st.button("Update Master Google Sheet", use_container_width=True):
                    link = export_to_google_sheets(final_df)
                    if link:
                        st.success("Google Sheet Updated Successfully!")
                        st.markdown(f'<a href="{link}" target="_blank" style="background-color:#0083B8;color:white;padding:10px;text-decoration:none;border-radius:5px;display:inline-block;">OPEN GOOGLE SHEET ⬈</a>', unsafe_allow_html=True)

        except Exception as e:
            st.error(f"Detailed Error: {e}")
