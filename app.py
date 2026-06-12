import streamlit as st
import pandas as pd
from bs4 import BeautifulSoup
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import re

# --- CONFIGURATION ---
GOOGLE_SHEET_NAME = "Ninja_Rank_Up_Output"

st.set_page_config(page_title="Ninja Rank Up Processor 1.0", page_icon="⭐", layout="wide")

# --- HELPER FUNCTIONS ---

def clean_name(name):
    """Standardizes names (Title Case, no extra spaces) for accurate merging."""
    if not isinstance(name, str): return ""
    clean = re.sub(r'\s+', ' ', name).replace(u'\xa0', ' ').strip()
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
    """Extracts Day and Time for sorting purposes."""
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

# --- PARSING LOGIC ---

def parse_student_list(html_content):
    """Pulls the Student Name and Group Keyword from the Custom Student List HTML."""
    soup = BeautifulSoup(html_content, 'lxml')
    data = []
    tables = soup.find_all('table')
    
    for table in tables:
        rows = table.find_all('tr')
        if not rows: continue
        headers = [c.get_text(strip=True).lower() for c in rows[0].find_all(['td', 'th'])]
        
        name_idx, key_idx = 1, 4 # Defaults based on standard reports
        for i, h in enumerate(headers):
            if "student name" in h: name_idx = i
            elif "keyword" in h: key_idx = i

        for row in rows[1:]:
            cols = row.find_all(['td', 'th'])
            def get_val(i): return cols[i].get_text(strip=True) if i < len(cols) else ""
            
            raw_name = get_val(name_idx)
            keywords_raw = get_val(key_idx).lower()
            
            group_match = re.search(r'(group\s*[1-3])', keywords_raw)
            clean_keyword = group_match.group(0).capitalize() if group_match else "No Group"

            if raw_name and len(raw_name) > 1:
                data.append({
                    "Student Name": clean_name(raw_name),
                    "Group": clean_keyword
                })
    
    df = pd.DataFrame(data)
    if not df.empty: df = df.drop_duplicates(subset=["Student Name"])
    return df

def parse_skill_evals(html_content):
    """Scans the matrix to count incomplete skills for each student."""
    soup = BeautifulSoup(html_content, 'lxml')
    data = []
    
    tables = soup.find_all('table')
    
    for table in tables:
        # iClassPro usually places the class name in an element directly preceding the table
        prev_tag = table.find_previous_sibling(['h2', 'h3', 'div', 'span'])
        class_name_raw = prev_tag.get_text(strip=True) if prev_tag else "Unknown Class"
        class_name = abbreviate_class_name(class_name_raw)
        
        rows = table.find_all('tr')
        if not rows or len(rows) < 2:
            continue
            
        # First row contains the Student Names
        header_cols = rows[0].find_all(['th', 'td'])
        if len(header_cols) < 2:
            continue
            
        students = []
        for col in header_cols[1:]: # Skip index 0 (which is the "Skills" label column)
            students.append(col.get_text(strip=True))
            
        # Initialize a dictionary to track how many skills are < 3 for each student
        student_tracker = {name: 0 for name in students if name}
        
        # Loop through the actual skills rows
        for row in rows[1:]:
            cols = row.find_all(['td', 'th'])
            if len(cols) < 2:
                continue
                
            scores = cols[1:] # The star/number values
            
            for idx, name in enumerate(students):
                if not name: continue
                if idx < len(scores):
                    score_text = scores[idx].get_text(strip=True)
                    
                    # Extract numeric value. If empty/blank, it counts as 0.
                    match = re.search(r'(\d)', score_text)
                    score = int(match.group(1)) if match else 0
                    
                    # If score is less than 3, it is an incomplete skill row
                    if score < 3:
                        student_tracker[name] += 1
                        
        # Filter for students with 3 or fewer incomplete skills
        for name, incomplete_count in student_tracker.items():
            if incomplete_count <= 3:
                if incomplete_count == 0:
                    status = "All Skills Complete (Check Stage Complete Mark)"
                else:
                    status = f"{incomplete_count} Skills Away"
                    
                data.append({
                    "Student Name": clean_name(name),
                    "Class Name": class_name,
                    "Incomplete Skills": incomplete_count,
                    "Status": status
                })
                
    return pd.DataFrame(data)

# --- GOOGLE SHEETS EXPORT ---

def export_to_google_sheets(df):
    """Pushes a clean, flat list of flagged students to Google Sheets."""
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
        st.error(f"Could not open sheet. Ensure a sheet named '{GOOGLE_SHEET_NAME}' exists and is shared with your service account email. Error: {e}")
        return None

    # Sort data logically: By Day, then Time, then Status
    df = df.sort_values(by=['Sort Day', 'Sort Time', 'Incomplete Skills'])
    export_df = df[["Student Name", "Group", "Class Name", "Status"]]

    try:
        ws = ss.worksheet("Rank Up Flags")
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title="Rank Up Flags", rows=100, cols=10)

    # Convert DataFrame to 2D List for Google Sheets
    data_matrix = [export_df.columns.values.tolist()] + export_df.values.tolist()
    
    ws.update(range_name="A1", values=data_matrix)
    
    # Simple Header Formatting
    ws.format("A1:D1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}})
    
    return f"https://docs.google.com/spreadsheets/d/{ss.id}"

# --- MAIN UI ---

st.title("⭐ Ninja Rank Up Processor 1.0")
st.write("Upload the **Class Evaluation Form HTML** and the **Custom Student List HTML** to flag students who are 3 or fewer skills away from completing their stage.")

col1, col2 = st.columns(2)
with col1:
    file_eval = st.file_uploader("Upload Class Evaluation Form (HTML)", type=['html', 'htm'])
with col2:
    file_list = st.file_uploader("Upload Custom Student List (HTML)", type=['html', 'htm'])

if file_eval and file_list:
    content_eval = file_eval.read().decode("utf-8", errors='ignore')
    content_list = file_list.read().decode("utf-8", errors='ignore')
    
    st.divider()
    
    with st.spinner('Parsing Evaluation Matrices...'):
        try:
            # 1. Parse Data
            df_evals = parse_skill_evals(content_eval)
            df_students = parse_student_list(content_list)
            
            if df_evals.empty:
                st.warning("⚠️ No students found within 3 skills of ranking up, or unable to read the Evaluation table format.")
            else:
                # 2. Merge Data
                merged_df = pd.merge(df_evals, df_students, on="Student Name", how="left")
                merged_df["Group"] = merged_df["Group"].fillna("Unknown")
                
                # 3. Add Sortable Time Columns
                merged_df[['Sort Day', 'Sort Time', 'Time Str']] = merged_df['Class Name'].apply(
                    lambda x: pd.Series(parse_class_info(x))
                )
                
                # Display Results in Streamlit
                st.success(f"Found {len(merged_df)} students ready or nearly ready to level up!")
                st.dataframe(merged_df[["Student Name", "Group", "Class Name", "Status"]], use_container_width=True)
                
                # 4. Google Sheets Push
                if st.button("Update Master Google Sheet", use_container_width=True):
                    link = export_to_google_sheets(merged_df)
                    if link:
                        st.success("Google Sheet Updated Successfully!")
                        st.markdown(f'<a href="{link}" target="_blank" style="background-color:#0083B8;color:white;padding:10px;text-decoration:none;border-radius:5px;display:inline-block;">OPEN GOOGLE SHEET ⬈</a>', unsafe_allow_html=True)
                        
        except Exception as e:
            st.error(f"Detailed Error: {e}")
