import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re

# --- CONFIGURATION ---
GOOGLE_SHEET_NAME = "Ninja_Rank_Up_Output"

# VERSION UPDATE: 3.2
st.set_page_config(page_title="Ninja Rank Up Processor 3.2", page_icon="⭐", layout="wide")

# --- HELPER FUNCTIONS ---

def clean_name(name):
    """Standardizes names, fixes commas, and removes numbers/dates."""
    if not isinstance(name, str): return ""
    clean = re.sub(r'\s+', ' ', name).replace(u'\xa0', ' ').strip()
    
    # Remove random numbers in parenthesis (e.g. "Lucas Arenhart (7)") 
    # to ensure it matches exactly across sheets.
    clean = re.sub(r'\(\d+\)', '', clean).strip()

    if ',' in clean:
        parts = clean.split(',')
        if len(parts) == 2:
            clean = f"{parts[1].strip()} {parts[0].strip()}"
            
    # Sometimes iClassPro mashes names together like "ElijahAbide" in the Eval sheet.
    # We use a regex trick to insert a space before capital letters (excluding the first).
    clean = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', clean)
            
    return clean.title()

def abbreviate_class_name(name):
    if not isinstance(name, str): return name
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
        if hour < 8: hour += 12 
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
    score_text = score_text.lower().strip()
    if not score_text or score_text == "-" or score_text.isspace() or "n/a" in score_text:
        return True
    
    match_frac = re.search(r'(\d+)\s*/\s*(\d+)', score_text)
    if match_frac:
        return int(match_frac.group(1)) < int(match_frac.group(2))
        
    match_single = re.search(r'(\d)', score_text)
    if match_single:
        return int(match_single.group(1)) < 3
        
    if any(mark in score_text for mark in ["pass", "✔", "✓", "★", "complete"]):
        return False
        
    return True

# --- PARSING LOGIC ---

def parse_roll_sheet(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    data = []
    headers = soup.find_all('div', class_='full-width-header')
    
    if not headers: return pd.DataFrame()

    for header in headers:
        name_span = header.find('span')
        class_name_raw = name_span.get_text(strip=True) if name_span else header.get_text(separator=" ", strip=True)
        current_class_name = abbreviate_class_name(class_name_raw)
        
        table = header.find_next('table', class_='table-roll-sheet')
        
        if not table: continue
        rows = table.find_all('tr')
        if not rows: continue
        
        first_row_cols = [c.get_text(strip=True).lower() for c in rows[0].find_all(['td', 'th'])]
        name_idx, detail_idx = -1, -1 
        
        for idx, col_text in enumerate(first_row_cols):
            if "student" in col_text: name_idx = idx
            if "details" in col_text: detail_idx = idx
            
        if name_idx == -1: name_idx = 1 # Fallback
            
        for row in rows[1:]:
            cols = row.find_all(['td', 'th'])
            if len(cols) <= name_idx: continue
                
            raw_name = cols[name_idx].get_text(separator=" ", strip=True)
            
            # The name column often contains random stuff. We just want the bold text or the first line.
            strong_tag = cols[name_idx].find('strong')
            if strong_tag:
                raw_name = strong_tag.get_text(strip=True)
            else:
                raw_name = raw_name.split('\n')[0].strip()

            skill_level = 0
            
            # Use specific details column based on screenshot
            if detail_idx != -1 and detail_idx < len(cols):
                details_text = cols[detail_idx].get_text(separator=" ", strip=True).lower()
                skill_match = re.search(r'\bs([0-9]|10)\b', details_text)
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
        if table.find('table'): continue 
        rows = [r for r in table.find_all('tr') if r.find_parent('table') == table]
        if not rows: continue
        
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(['td', 'th']) if c.find_parent('tr') == rows[0]]
        
        name_idx, key_idx, age_idx = 1, 4, 3 
        for i, h in enumerate(headers):
            if "student name" in h: name_idx = i
            elif "keyword" in h: key_idx = i
            elif "age" in h: age_idx = i

        for row in rows[1:]:
            cols = [c for c in row.find_all(['td', 'th']) if c.find_parent('tr') == row]
            def get_val(i): return cols[i].get_text(separator=" ", strip=True) if i < len(cols) else ""
            
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
    if not df.empty: df = df.drop_duplicates(subset=["Student Name"])
    return df

def parse_skill_evals_v3(text_lines):
    """
    Since the Eval HTML format is breaking BS4, we parse the raw text lines directly.
    This perfectly handles the format shown in the prompt context.
    """
    student_evals = {}
    current_stage = None
    students = []
    
    for i, line in enumerate(text_lines):
        line = line.strip()
        if not line: continue
        
        # 1. Detect Stage from the Title (e.g. "... - Flip Side Ninjas , Stage 1")
        if "Stage" in line and "|" in line:
            match = re.search(r'Stage\s*(\d+)', line, re.IGNORECASE)
            if match:
                current_stage = int(match.group(1))
                students = [] # Reset students for the new table
            continue
            
        # 2. Extract Student Names (e.g. "ElijahAbide (1)")
        # We look for lines containing no spaces and ending in a number in parenthesis
        if current_stage is not None and re.match(r'^[A-Za-z\-]+\s*\(\d+\)$', line):
            raw_name = line.split('(')[0].strip()
            students.append(clean_name(raw_name))
            continue
            
        # 3. Detect Skill Row & Scores
        # If we have a stage, and we have students, and the line looks like a skill description
        if current_stage is not None and len(students) > 0:
            # Skills usually start with letters and have a number at the very end
            if re.match(r'^[A-Za-z\*]+', line):
                # We found a skill. The next N valid lines should be scores/dates.
                scores = []
                j = i + 1
                while j < len(text_lines) and len(scores) < len(students):
                    next_line = text_lines[j].strip()
                    # Skip date lines
                    if re.match(r'^\d{4}-\d{2}-\d{2}', next_line):
                        j += 1
                        continue
                    if next_line: # It's a score!
                        scores.append(next_line)
                    j += 1
                
                # Grade the scores
                for idx, s_name in enumerate(students):
                    if idx < len(scores):
                        inc_status = is_skill_incomplete(scores[idx])
                        
                        if s_name not in student_evals:
                            student_evals[s_name] = {}
                        if current_stage not in student_evals[s_name]:
                            student_evals[s_name][current_stage] = {'total': 0, 'incomplete': 0}
                            
                        student_evals[s_name][current_stage]['total'] += 1
                        if inc_status:
                            student_evals[s_name][current_stage]['incomplete'] += 1
                            
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

st.title("⭐ Ninja Rank Up Processor 3.2")
st.write("Upload all three files to cross-reference a student's **Current Level** with their **Target Evaluation Scores**.")

col1, col2, col3 = st.columns(3)
with col1:
    file_roll = st.file_uploader("1. Roll Sheet", type=['html', 'htm'])
with col2:
    file_list = st.file_uploader("2. Student List", type=['html', 'htm'])
with col3:
    file_eval = st.file_uploader("3. Skill Evaluation", type=['html', 'htm', 'txt'])

if file_roll and file_list and file_eval:
    content_roll = file_roll.read().decode("utf-8", errors='ignore')
    content_list = file_list.read().decode("utf-8", errors='ignore')
    
    # Handle both HTML and Text copy/pastes for Evals just in case
    content_eval = file_eval.read().decode("utf-8", errors='ignore')
    eval_lines = []
    if "<html>" in content_eval.lower() or "<table" in content_eval.lower():
        soup = BeautifulSoup(content_eval, 'lxml')
        eval_lines = soup.get_text(separator='\n').split('\n')
    else:
        eval_lines = content_eval.split('\n')
    
    st.divider()
    
    with st.spinner('Parsing and Cross-Referencing Stages...'):
        try:
            df_roll = parse_roll_sheet(content_roll)
            df_list = parse_student_list(content_list)
            evals_dict = parse_skill_evals_v3(eval_lines)
            
            # OUTER JOIN to ensure no student is dropped due to mismatched names
            merged_df = pd.merge(df_roll, df_list, on="Student Name", how="outer")
            merged_df["Group"] = merged_df["Group"].fillna("No Group")
            merged_df["Class Name"] = merged_df["Class Name"].fillna("Unknown Class")
            
            results = []
            for _, row in merged_df.iterrows():
                s_name = row['Student Name']
                last_passed = int(row.get('Current Level', 0)) if pd.notna(row.get('Current Level')) else 0
                target_lvl = last_passed + 1 # We specifically evaluate for the NEXT stage
                
                if s_name in evals_dict and target_lvl in evals_dict[s_name]:
                    eval_data = evals_dict[s_name][target_lvl]
                    total = eval_data['total']
                    inc = eval_data['incomplete']
                    
                    if total > 0:
                        if inc == 0:
                            status = f"Stage {target_lvl} complete (not marked)"
                        elif inc == 1:
                            status = f"1 skill away (Stage {target_lvl})"
                        elif inc <= 3:
                            status = f"{inc} skills away (Stage {target_lvl})"
                        else:
                            continue 
                            
                        # Format Name with Age
                        age_val = extract_digits(row.get('Age', ''))
                        age_str = f" ({age_val})" if age_val else ""
                        display_name = f"{s_name}{age_str}"
                        
                        day, sort_time, time_str = parse_class_info(row['Class Name'])
                        
                        results.append({
                            "Student Name": display_name,
                            "Group": row['Group'],
                            "Class Name": row['Class Name'],
                            "Status": status,
                            "Sort Day": day,
                            "Sort Time": sort_time,
                            "Incomplete": inc 
                        })
            
            final_df = pd.DataFrame(results)
            
            if final_df.empty:
                st.warning("No students are currently within 3 skills of ranking up for their NEXT stage.")
            else:
                final_df = final_df.drop_duplicates()
                st.success(f"Found {len(final_df)} students ready or nearly ready to rank up!")
                st.dataframe(final_df[["Student Name", "Group", "Class Name", "Status"]], use_container_width=True)
                
                if st.button("Update Master Google Sheet", use_container_width=True):
                    link = export_to_google_sheets(final_df)
                    if link:
                        st.success("Google Sheet Updated Successfully!")
                        st.markdown(f'<a href="{link}" target="_blank" style="background-color:#0083B8;color:white;padding:10px;text-decoration:none;border-radius:5px;display:inline-block;">OPEN GOOGLE SHEET ⬈</a>', unsafe_allow_html=True)
                        
        except Exception as e:
            st.error(f"Detailed Error: {e}")
