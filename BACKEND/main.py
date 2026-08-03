from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import pandas as pd
from rapidfuzz import process, fuzz
import random
import io
import sqlite3
import json
import re  # <--- NEW: Imported for password validation

app = FastAPI(title="TradeSync AI API")

# Allow the frontend to communicate with the backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "tradesync.db"

def get_connection():
    # Always connect to local SQLite for Demo (No Postgres needed)
    conn = sqlite3.connect(DB_FILE)
    return conn

def run_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(query, params)
    
    result = None
    if fetchone: result = cursor.fetchone()
    elif fetchall: result = cursor.fetchall()
        
    if commit: conn.commit()
    conn.close()
    return result

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, username TEXT UNIQUE, name TEXT, password TEXT, role TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS outcomes (id TEXT PRIMARY KEY, gstin TEXT, name TEXT, conf REAL, ward INTEGER, status TEXT, assignee TEXT, case_id TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS tasks (case_id TEXT PRIMARY KEY, name TEXT, address TEXT, ward INTEGER, assignedToUsername TEXT, status TEXT, report TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    
    check_q = "SELECT value FROM metadata WHERE key='summary'"
    cursor.execute(check_q)
    if not cursor.fetchone():
        initial_summary = {"flagged": 0, "verified": 0, "cases": 0, "completed": 0, "ward_stats": []}
        cursor.execute("INSERT INTO metadata (key, value) VALUES ('summary', ?)", (json.dumps(initial_summary),))
        conn.commit()
        
    conn.close()

init_db()

# --- AUTH SCHEMAS & MOCK ENDPOINTS ---

class SignupData(BaseModel):
    name: str
    email: str
    username: str
    password: str
    role: str

class LoginData(BaseModel):
    email: str
    password: str

class ForgotPasswordData(BaseModel):
    email: str

@app.post("/api/auth/signup")
def signup(data: SignupData):
    # --- NEW: Strict Password Validation ---
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long.")
    if not re.search(r"[A-Z]", data.password):
        raise HTTPException(status_code=400, detail="Password must contain at least one uppercase letter.")
    if not re.search(r"[0-9]", data.password):
        raise HTTPException(status_code=400, detail="Password must contain at least one number.")
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", data.password):
        raise HTTPException(status_code=400, detail="Password must contain at least one special character.")

    # --- ENHANCED: Duplicate Email / Username Check ---
    existing_user = run_query("SELECT email, username FROM users WHERE email = ? OR username = ?", (data.email, data.username), fetchone=True)
    
    if existing_user:
        if existing_user[0] == data.email: 
            raise HTTPException(status_code=400, detail="This email is already registered. Please log in instead.")
        else: 
            raise HTTPException(status_code=400, detail="This system username is already taken. Try another one.")

    try:
        run_query("INSERT INTO users (email, username, name, password, role) VALUES (?, ?, ?, ?, ?)",
                  (data.email, data.username, data.name, data.password, data.role), commit=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Database registration error.")
    
    user_response = data.model_dump()
    user_response.pop("password", None)
    return {"message": "User created", "user": user_response}

@app.post("/api/auth/login")
def login(data: LoginData):
    row = run_query("SELECT name, username, password, role FROM users WHERE email = ?", (data.email,), fetchone=True)
    if row and row[2] == data.password:
        return {"message": "Login successful", "user": {"name": row[0], "username": row[1], "role": row[3]}}
    raise HTTPException(status_code=401, detail="Invalid email or password")

@app.post("/api/auth/forgot-password")
def forgot_password(data: ForgotPasswordData):
    # DEMO MODE: Silently accept the request and return success to the UI.
    return {"message": "If the email exists, a reset link has been sent."}


# --- SYSTEM ENDPOINTS ---

@app.post("/api/engine/upload_and_run")
async def upload_and_run(gst_file: UploadFile = File(...), gvmc_file: UploadFile = File(...)):
    try:
        gst_df = pd.read_csv(gst_file.file)
        gvmc_df = pd.read_csv(gvmc_file.file)
        
        gst_name_col, gst_id_col, gst_ward_col = "Business_Name", "GSTIN", "Ward_No"
        gvmc_name_col = "Business_Name"
        
        gvmc_names = gvmc_df[gvmc_name_col].dropna().astype(str).tolist()
        outcomes, flagged_count, ward_data = [], 0, {}
        
        for index, row in gst_df.iterrows():
            gst_biz_name = str(row[gst_name_col])
            best_match = process.extractOne(gst_biz_name, gvmc_names, scorer=fuzz.token_sort_ratio)
            
            if best_match:
                match_name, match_score, match_index = best_match
                if match_score < 90:
                    flagged_count += 1
                    try: ward = int(row.get(gst_ward_col, 0))
                    except: ward = 0
                        
                    outcomes.append((
                        f"REC_{random.randint(10000,99999)}", 
                        str(row.get(gst_id_col, "UNKNOWN")),
                        gst_biz_name, round(match_score, 1), ward, "PENDING_APPROVAL", None, None
                    ))
                    
                    if ward not in ward_data:
                        ward_data[ward] = {"ward_no": ward, "flagged": 0, "verified": 0}
                    ward_data[ward]["flagged"] += 1

        run_query("DELETE FROM outcomes", commit=True)
        
        conn = get_connection()
        cursor = conn.cursor()
        cursor.executemany("INSERT INTO outcomes (id, gstin, name, conf, ward, status, assignee, case_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", outcomes)
        conn.commit()
        conn.close()
        
        ward_stats_list = sorted(list(ward_data.values()), key=lambda x: x["ward_no"])
        summary = {"flagged": flagged_count, "verified": 0, "cases": flagged_count, "completed": 0, "ward_stats": ward_stats_list}
        run_query("UPDATE metadata SET value = ? WHERE key = 'summary'", (json.dumps(summary),), commit=True)

        preview_names = [o[2] for o in outcomes[:5]]
        return {"message": "Success", "flagged": flagged_count, "preview": preview_names}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Data processing error: {str(e)}")

class AssignData(BaseModel):
    outcome_id: str
    officer_username: str

@app.post("/api/cases/approve/{outcome_id}")
def approve_case(outcome_id: str):
    case_id = f"CASE_{random.randint(1000, 9999)}"
    run_query("UPDATE outcomes SET status = 'APPROVED', case_id = ? WHERE id = ?", (case_id, outcome_id), commit=True)
    return {"message": "Approved"}

@app.post("/api/cases/assign")
def assign_case(data: AssignData):
    user_row = run_query("SELECT name FROM users WHERE username = ?", (data.officer_username,), fetchone=True)
    officer_name = user_row[0] if user_row else "Inspector"
    out_row = run_query("SELECT name, ward FROM outcomes WHERE id = ?", (data.outcome_id,), fetchone=True)
    
    if out_row:
        name, ward = out_row[0], out_row[1]
        run_query("UPDATE outcomes SET status = 'ASSIGNED', assignee = ? WHERE id = ?", (officer_name, data.outcome_id), commit=True)
        case_id = run_query("SELECT case_id FROM outcomes WHERE id = ?", (data.outcome_id,), fetchone=True)[0]
        
        run_query("DELETE FROM tasks WHERE case_id = ?", (case_id,), commit=True)
        run_query("INSERT INTO tasks (case_id, name, address, ward, assignedToUsername, status, report) VALUES (?, ?, ?, ?, ?, 'ASSIGNED', NULL)",
                  (case_id, name, f"Ward {ward} District", ward, data.officer_username), commit=True)
        return {"message": "Assigned"}
    raise HTTPException(status_code=404)

class ReportData(BaseModel):
    case_id: str
    ward: int
    outcome: str
    gps: str
    photo_base64: str

@app.post("/api/field/submit")
def submit_report(data: ReportData):
    report_dict = {"outcome": data.outcome, "gps": data.gps, "photo_base64": data.photo_base64}
    run_query("UPDATE tasks SET status = 'AWAITING_SUPERVISOR', report = ? WHERE case_id = ?", 
              (json.dumps(report_dict), data.case_id), commit=True)
    return {"message": "Sent for Review"}

@app.post("/api/supervisor/verify/{case_id}")
def verify_report(case_id: str):
    task_row = run_query("SELECT ward FROM tasks WHERE case_id = ?", (case_id,), fetchone=True)
    target_ward = task_row[0] if task_row else 0
    
    run_query("UPDATE tasks SET status = 'COMPLETED' WHERE case_id = ?", (case_id,), commit=True)
    
    summary_raw = run_query("SELECT value FROM metadata WHERE key = 'summary'", fetchone=True)[0]
    summary = json.loads(summary_raw)
    
    summary["verified"] += 1
    summary["cases"] = max(0, summary["cases"] - 1)
    summary["completed"] += 1
    for w in summary["ward_stats"]:
        if w["ward_no"] == target_ward: w["verified"] += 1
            
    run_query("UPDATE metadata SET value = ? WHERE key = 'summary'", (json.dumps(summary),), commit=True)
    return {"message": "Task Closed and Verified"}

@app.get("/api/data/export")
def export_data():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM outcomes", conn)
    conn.close()
    stream = io.StringIO()
    df.to_csv(stream, index=False)
    response = StreamingResponse(iter([stream.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=GVMC_AI_Audit_Report.csv"
    return response

@app.get("/api/data/summary")
def get_summary():
    res = run_query("SELECT value FROM metadata WHERE key = 'summary'", fetchone=True)
    return json.loads(res[0]) if res else {}

@app.get("/api/data/outcomes")
def get_outcomes():
    rows = run_query("SELECT id, gstin, name, conf, ward, status, assignee, case_id FROM outcomes", fetchall=True)
    return [{"id": r[0], "gstin": r[1], "name": r[2], "conf": r[3], "ward": r[4], "status": r[5], "assignee": r[6], "case_id": r[7]} for r in (rows or [])]

@app.get("/api/data/users")
def get_users():
    rows = run_query("SELECT name, username, role FROM users", fetchall=True)
    return [{"name": r[0], "username": r[1], "role": r[2]} for r in (rows or [])]

@app.get("/api/data/tasks")
def get_tasks():
    rows = run_query("SELECT case_id, name, address, ward, assignedToUsername, status, report FROM tasks", fetchall=True)
    return [{
        "case_id": r[0], "name": r[1], "address": r[2], "ward": r[3], 
        "assignedToUsername": r[4], "status": r[5], "report": json.loads(r[6]) if r[6] else None
    } for r in (rows or [])]

# Auto-start the server on port 8080 to avoid conflicts
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080)
    