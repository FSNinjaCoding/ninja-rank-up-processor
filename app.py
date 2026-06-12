import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re

# --- CONFIGURATION ---
GOOGLE_SHEET_NAME = "Ninja_Rank_Up_Output"

st.set_page_config(page_title="Ninja Rank Up Processor 2.0", page_icon="⭐", layout="wide")

# --- HELPER FUNCTIONS ---

def clean_name(name):
    """Standardizes names and fixes 'Last, First' formatting."""
    if not isinstance(name, str): return ""
    clean = re.sub(r'\s+', ' ', name).replace(u'\xa0', ' ').strip()
    if ',' in clean:
        parts = clean.split(',')
        if len(parts) == 2:
            clean = f"{parts[1].strip()} {parts[0].strip()}"
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

# --- PARSING LOGIC ---

def parse_roll_sheet(html_content):
    """Extracts Student Name, Class Name, and Current Level (Last Passed)."""
    soup = BeautifulSoup(html_content, 'lxml')
    data = []
    headers = soup.find_all('div', class_='full-width-header')
    
    if not headers: return pd.DataFrame()

    for header in headers:
        name_span = header.find('span')
        class_name_raw = name_span.get_text(strip=True) if name_span else header.get_text(separator=" ", strip=True)
        current_class_name = abbreviate_class_name(class_name_raw)
        
        table = header.find_next('table', class_='table-roll-sheet')
        next_header = header.find_next('div', class_='full-width-header')
        
        if table and next_header:
            h_line = next_header.sourceline
            t_line = table.sourceline
            if h_line is not None and t_line is not None and h_line < t_line:
                continue 

        if not table: continue
        rows = table.find_all('tr')
        if not rows: continue
        
        first_row_cols = [c.get_text(strip=True) for c in rows[0].find_all(['td', 'th'])]
        name_idx, detail_idx = 1, 3 
        
        for idx, col_text in enumerate(first_row_cols):
            if "Student" in col_text: name_idx = idx
            if "Details" in col_text: detail_idx = idx
            
        for row in rows[1:]:
            cols = row.find_all(['td', 'th'])
            def get_val(i): return cols[i].get_text(strip=True) if i < len(cols) else ""
            
            raw_name = get_val(name_idx)
            details_text = get_val(detail_idx).lower()
            
            # Extract Level (e.g. "s2")
            skill_level = 0
            skill_match = re.search(r's([0-9]|10)\b', details_text)
            if skill_match: 
                skill_level = int(skill_match.group(1))
            
            if raw_name and len(raw_name) > 1 and "Student" not in raw_name:
                data.append({
                    "Student Name": clean_name(raw_name),
                    "Current Level": skill_level,
                    "Class Name": current_class_name
                })

    df = pd.DataFrame(data)
    if not df.empty: df = df.drop_duplicates(subset=["Student Name"], keep='first')
    return df

def parse_student_list(html_content):
    """Extracts Group and Age."""
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

def parse_skill_evals_v2(html_content):
    """Maps every student to a dictionary of {Level: {total_skills: X, incomplete_skills: Y}}"""
    soup = BeautifulSoup(html_content, 'lxml')
    student_evals = {} 
    tables = soup.find_all('table')
    
    for table in tables:
        if table.find('table'): continue 
        
        rows = [r for r in table.find_all('tr') if r.find_parent('table') == table]
        if len(rows) < 2: continue
            
        header_cols = [c for c in rows[0].find_all(['td', 'th']) if c.find_parent('tr') == rows[0]]
        if len(header_cols) < 2: continue
            
        students = [clean_name(c.get_text(separator=" ", strip=True)) for c in header_cols[1:]]
        
        # Look for table's overarching level in nearby text
        table_level = None
        curr = table.previous_element
        count = 0
        while curr and count < 50:
            if isinstance(curr, str) and curr.strip():
                match = re.search(r'\b(?:stage|level|s)[-\s]?0*(\d+)\b', curr.strip(), re.IGNORECASE)
                if match:
                    table_level = int(match.group(1))
                    break
            curr = curr.previous_element
            count += 1
            
        for row in rows[1:]:
            cols = [c for c in row.find_all(['td', 'th']) if c.find_parent('tr') == row]
            if len(cols) < 2: continue
                
            skill_name = cols[0].get_text(separator=" ", strip=True)
            
            # Identify the level of this specific skill
            row_level = table_level
            lvl_match = re.search(r'\b(?:stage|level|s)[-\s]?0*(\d+)\b', skill_name, re.IGNORECASE)
            if lvl_match:
                row_level = int(lvl_match.group(1))
                
            if row_level is None:
                continue # We skip scores if we can't identify what stage they belong to
                
            scores = cols[1:] 
            for idx, s_name in enumerate(students):
                if not s_name: continue
                if idx < len(scores):
                    score_text = scores[idx].get_text(separator=" ", strip=True)
                    if not score_text or score_text == "-":
                        score = 0
                    else:
                        match = re.search(r'(\d)', score_text)
                        score = int(match.group(1)) if match else 0
                        
                    if s_name not in student_evals:
                        student_evals[s_name] = {}
                    if row_level not in student_evals[s_name]:
                        student_evals[s_name][row_level] = {'total': 0, 'incomplete': 0}
                        
                    student_evals[s_name][row_level]['total'] += 1
                    if score < 3:
                        student_evals[s_name][row_level]['incomplete'] += 1
                        
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

    # Sort Logic
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

st.title("⭐ Ninja Rank Up Processor 2.0")
st.write("Upload all three files to cross-reference a student's **Current Level** with their **Target Evaluation Scores**.")

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
            # 1. Parse Data
            df_roll = parse_roll_sheet(content_roll)
            df_list = parse_student_list(content_list)
            evals_dict = parse_skill_evals_v2(content_eval)
            
            if df_roll.empty or df_list.empty:
                st.warning("⚠️ Could not read Roll Sheet or Student List.")
            else:
                # 2. Merge Base Data
                merged_df = pd.merge(df_roll, df_list, on="Student Name", how="left")
                merged_df["Group"] = merged_df["Group"].fillna("No Group")
                
                # 3. Analyze Target Skills
                results = []
                for _, row in merged_df.iterrows():
                    s_name = row['Student Name']
                    last_passed = row['Current Level']
                    target_lvl = last_passed + 1 # We only care about the NEXT stage
                    
                    if s_name in evals_dict and target_lvl in evals_dict[s_name]:
                        eval_data = evals_dict[s_name][target_lvl]
                        total = eval_data['total']
                        inc = eval_data['incomplete']
                        
                        if total > 0:
                            if inc == 0:
                                status = "Stage complete (not marked)"
                            elif inc <= 3:
                                status = f"{inc} skills away"
                            else:
                                continue # Ignore students missing >3 skills
                                
                            # Format Name with Age
                            age_val = extract_digits(row.get('Age', ''))
                            age_str = f" ({age_val})" if age_val else ""
                            display_name = f"{s_name}{age_str}"
                            
                            # Parse Time for Sorting
                            day, sort_time, time_str = parse_class_info(row['Class Name'])
                            
                            results.append({
                                "Student Name": display_name,
                                "Group": row['Group'],
                                "Class Name": row['Class Name'],
                                "Status": status,
                                "Sort Day": day,
                                "Sort Time": sort_time,
                                "Incomplete": inc # Hidden column for sorting
                            })
                
                final_df = pd.DataFrame(results)
                
                if final_df.empty:
                    st.success("No students are currently within 3 skills of ranking up.")
                else:
                    st.success(f"Found {len(final_df)} students ready or nearly ready to rank up!")
                    st.dataframe(final_df[["Student Name", "Group", "Class Name", "Status"]], use_container_width=True)
                    
                    if st.button("Update Master Google Sheet", use_container_width=True):
                        link = export_to_google_sheets(final_df)
                        if link:
                            st.success("Google Sheet Updated Successfully!")
                            st.markdown(f'<a href="{link}" target="_blank" style="background-color:#0083B8;color:white;padding:10px;text-decoration:none;border-radius:5px;display:inline-block;">OPEN GOOGLE SHEET ⬈</a>', unsafe_allow_html=True)
                            
        except Exception as e:
            st.error(f"Detailed Error: {e}")
