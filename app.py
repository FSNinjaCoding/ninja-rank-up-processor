import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re

# --- CONFIGURATION ---
GOOGLE_SHEET_NAME = "Ninja_Rank_Up_Output"

# VERSION UPDATE: 2.3
st.set_page_config(page_title="Ninja Rank Up Processor 2.3", page_icon="⭐", layout="wide")

# --- HELPER FUNCTIONS ---

def clean_name(name):
    """Standardizes names and safely converts 'Last, First' to 'First Last'."""
    if not isinstance(name, str): return ""
    clean = re.sub(r'\s+', ' ', name).replace(u'\xa0', ' ').strip()
    if ',' in clean:
        parts = clean.split(',')
        if len(parts) == 2:
            clean = f"{parts[1].strip()} {parts[0].strip()}"
    return clean.title()

def abbreviate_class_name(name):
    """Shortens class names to save space."""
    if not isinstance(name, str): return name
    name = re.sub(r'\d{1,2}/\d{1,2}/\d{4}.*', '', name).strip()
    name = name.replace("Homeschool", "HS")
    name = name.replace("Flip Side Ninjas", "FS Ninjas")
    name = name.replace("(Ages ", "(")
    return name

def parse_class_info(class_name):
    """Extracts day and time for clean sorting."""
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
    """Intelligently checks fractions, single digits, and checkmarks."""
    score_text = score_text.lower().strip()
    if not score_text or score_text == "-" or "n/a" in score_text:
        return True
    
    # 1. Fraction check (e.g. 2/3)
    match_frac = re.search(r'(\d)\s*/\s*(\d)', score_text)
    if match_frac:
        return int(match_frac.group(1)) < int(match_frac.group(2))
        
    # 2. Single digit check (Assume 3 is passing)
    match_single = re.search(r'(\d)', score_text)
    if match_single:
        return int(match_single.group(1)) < 3
        
    # 3. Text/Symbol check
    if any(mark in score_text for mark in ["pass", "✔", "✓", "★", "complete"]):
        return False
        
    return True # Default to incomplete if unrecognized

# --- PARSING LOGIC ---

def parse_roll_sheet(html_content):
    """Extracts Student Name, Class Name, and Current Level."""
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
                
            raw_name = cols[name_idx].get_text(strip=True)
            skill_level = 0
            
            # Method 1: Target Details column
            if detail_idx != -1 and detail_idx < len(cols):
                details_text = cols[detail_idx].get_text(strip=True).lower()
                skill_match = re.search(r'\b(?:stage|level|s)[-\s]?0*(\d+)\b', details_text)
                if skill_match: 
                    skill_level = int(skill_match.group(1))
                    
            # Method 2: Target the whole row if details missing
            if skill_level == 0:
                row_text = row.get_text(separator=" ", strip=True).lower()
                skill_match = re.search(r'\b(?:stage|level|s)[-\s]?0*(\d+)\b', row_text)
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
        # Keep the highest level if duplicate names exist
        df = df.sort_values('Current Level', ascending=False).drop_duplicates(subset=["Student Name"], keep='first')
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

def parse_skill_evals_v3(html_content):
    """Maps every student to {Stage: {total: X, incomplete: Y}}"""
    soup = BeautifulSoup(html_content, 'lxml')
    student_evals = {} 
    tables = soup.find_all('table')
    
    for table in tables:
        if table.find('table'): continue 
        
        rows = [r for r in table.find_all('tr') if r.find_parent('table') == table]
        if len(rows) < 2: continue
            
        # 1. Grab overarching table stage if available in headers
        table_stage = None
        prev_tags = table.find_all_previous(['h1', 'h2', 'h3', 'h4', 'div', 'th', 'td', 'b', 'strong'])
        for tag in prev_tags[:20]: # Check last 20 elements
            text = tag.get_text(separator=" ", strip=True)
            match = re.search(r'\b(?:stage|level|s)[-\s]?0*(\d+)\b', text, re.IGNORECASE)
            if match:
                table_stage = int(match.group(1))
                break

        # 2. Dynamically find the exact row containing the Student Names
        students = []
        student_row_idx = -1
        for i, row in enumerate(rows[:5]):
            cols = [c for c in row.find_all(['td', 'th']) if c.find_parent('tr') == row]
            if len(cols) > 1:
                text = cols[0].get_text(strip=True).lower()
                if any(x in text for x in ["skill", "event", "name", "printout"]) or not text:
                    students = [clean_name(c.get_text(separator=" ", strip=True)) for c in cols[1:]]
                    student_row_idx = i
                    break
        
        if not students: continue 
            
        # 3. Process the grades
        current_stage = table_stage
        for row in rows[student_row_idx + 1:]:
            cols = [c for c in row.find_all(['td', 'th']) if c.find_parent('tr') == row]
            if not cols: continue
                
            # If it's a sub-header row marking a new stage (e.g., "Stage 3 - Floor")
            if len(cols) == 1:
                text = cols[0].get_text(separator=" ", strip=True)
                match = re.search(r'\b(?:stage|level|s)[-\s]?0*(\d+)\b', text, re.IGNORECASE)
                if match: current_stage = int(match.group(1))
                continue
                
            # Standard Skill Row
            if len(cols) > 1:
                skill_name = cols[0].get_text(separator=" ", strip=True)
                row_stage = current_stage
                
                # Check if the specific skill overrides the table stage
                lvl_match = re.search(r'\b(?:stage|level|s)[-\s]?0*(\d+)\b', skill_name, re.IGNORECASE)
                if lvl_match: row_stage = int(lvl_match.group(1))
                    
                if row_stage is None: continue 
                    
                scores = cols[1:] 
                for idx, s_name in enumerate(students):
                    if not s_name: continue
                    if idx < len(scores):
                        score_text = scores[idx].get_text(separator=" ", strip=True)
                        inc_status = is_skill_incomplete(score_text)
                                    
                        if s_name not in student_evals:
                            student_evals[s_name] = {}
                        if row_stage not in student_evals[s_name]:
                            student_evals[s_name][row_stage] = {'total': 0, 'incomplete': 0}
                            
                        student_evals[s_name][row_stage]['total'] += 1
                        if inc_status:
                            student_evals[s_name][row_stage]['incomplete'] += 1
                            
    return student_evals

# --- GOOGLE SHEETS EXPORT ---

def export_to_google_sheets(df):
    if "gcp_service_account" not in st.secrets:
        st.error("Secrets not found!")
        return None

    creds_dict = st.secrets["gcp_service_account"]
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_
