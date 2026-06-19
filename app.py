import os
import re
import time
import json
import logging
import threading
import io
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote_plus, unquote
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from groq import Groq
from dotenv import load_dotenv
import pypdf
from werkzeug.utils import secure_filename

# Load env
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "job-scraper-local-secret-2026")

db_url = os.getenv("DATABASE_URL")
if db_url:
    # SQLAlchemy 1.4+ requires "postgresql://" instead of "postgres://"
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///jobs.db"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Job(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    company = db.Column(db.String(255), nullable=False)
    logo_url = db.Column(db.String(512), nullable=True)
    location = db.Column(db.String(255), default="India")
    job_type = db.Column(db.String(100), default="Job")
    category = db.Column(db.String(100), default="Python")
    experience_level = db.Column(db.String(255), default="Freshers")
    salary = db.Column(db.String(100), nullable=True)
    skills = db.Column(db.String(512), nullable=False)
    description = db.Column(db.Text, nullable=False)
    course_match = db.Column(db.Text, nullable=True)
    apply_url = db.Column(db.String(512), unique=True, nullable=False)
    target_batch = db.Column(db.String(100), default="2025, 2026")
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class SearchConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=True)
    target_roles = db.Column(db.String(512), default="Python Developer, AI Engineer, Fullstack Developer")
    target_locations = db.Column(db.String(512), default="Remote, Bangalore, Hybrid")
    resume_text = db.Column(db.Text, nullable=True)
    resume_filename = db.Column(db.String(255), nullable=True)
    groq_api_key = db.Column(db.String(255), nullable=True)

class JobMatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=True)
    job_id = db.Column(db.Integer, db.ForeignKey("job.id", ondelete="CASCADE"))
    fit_score = db.Column(db.Integer, default=0)
    explanation = db.Column(db.Text, nullable=True)
    recruiter_pitch = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    job = db.relationship("Job", backref=db.backref("matches", cascade="all, delete-orphan"))

# Initialize DB tables on startup (compatible with gunicorn/Render)
def _init_db():
    db.create_all()
    
    # Run manual migrations to add user_id column if it doesn't exist
    try:
        from sqlalchemy import text
        bind_engine = db.engine
        if "sqlite" not in str(bind_engine.url):
            # Postgres migrations
            db.session.execute(text('ALTER TABLE search_config ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE;'))
            db.session.execute(text('ALTER TABLE job_match ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE;'))
            db.session.commit()
        else:
            # Sqlite migrations
            try:
                db.session.execute(text('ALTER TABLE search_config ADD COLUMN user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE;'))
                db.session.commit()
            except Exception:
                db.session.rollback()
            try:
                db.session.execute(text('ALTER TABLE job_match ADD COLUMN user_id INTEGER REFERENCES "user"(id) ON DELETE CASCADE;'))
                db.session.commit()
            except Exception:
                db.session.rollback()
    except Exception as e:
        db.session.rollback()
        logging.error(f"Migration error: {e}")

    # Seed admin user
    admin_email = "rohithbuildsofficial@gmail.com"
    admin = User.query.filter_by(email=admin_email).first()
    if not admin:
        admin = User(
            username="admin",
            email=admin_email,
            password_hash=generate_password_hash("admin1234")
        )
        db.session.add(admin)
        db.session.commit()
        
        # Create SearchConfig for admin
        config = SearchConfig(user_id=admin.id)
        db.session.add(config)
        db.session.commit()

with app.app_context():
    _init_db()

# Status globals
SCRAPER_STATUS = {
    "status": "Idle",
    "current_action": "Not running",
    "links_found": 0,
    "processed_count": 0,
    "added_count": 0,
    "last_run": None,
    "logs": [],
    "ai_thoughts": []
}
SCRAPER_STOP_EVENT = threading.Event()
SCRAPER_THREAD = None

# Match status globals
MATCHER_STATUS = {
    "status": "Idle",
    "logs": []
}
MATCHER_THREAD = None

def add_scraper_log(msg):
    timestamp = time.strftime("%H:%M:%S")
    SCRAPER_STATUS["logs"].append(f"[{timestamp}] {msg}")
    if len(SCRAPER_STATUS["logs"]) > 50:
        SCRAPER_STATUS["logs"].pop(0)
    logging.info(f"[Scraper] {msg}")

def add_ai_thought(msg):
    timestamp = time.strftime("%H:%M:%S")
    SCRAPER_STATUS["ai_thoughts"].append(f"[{timestamp}] {msg}")
    if len(SCRAPER_STATUS["ai_thoughts"]) > 50:
        SCRAPER_STATUS["ai_thoughts"].pop(0)

def add_matcher_log(msg):
    timestamp = time.strftime("%H:%M:%S")
    MATCHER_STATUS["logs"].append(f"[{timestamp}] {msg}")
    if len(MATCHER_STATUS["logs"]) > 50:
        MATCHER_STATUS["logs"].pop(0)
    logging.info(f"[Matcher] {msg}")

def call_groq_with_fallback(messages, user_id=None, max_tokens=1000):
    config = SearchConfig.query.filter_by(user_id=user_id).first() if user_id else SearchConfig.query.first()
    api_key = config.groq_api_key if (config and config.groq_api_key) else os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set.")
    client = Groq(api_key=api_key)
    models = ["llama-3.3-70b-versatile", "llama3-70b-8192", "llama-3.1-8b-instant", "llama3-8b-8192"]
    last_err = None
    for model in models:
        try:
            chat_completion = client.chat.completions.create(
                messages=messages,
                model=model,
                temperature=0.3,
                max_tokens=max_tokens
            )
            return chat_completion.choices[0].message.content
        except Exception as e:
            last_err = e
            logging.warning(f"Model {model} failed: {e}")
    raise RuntimeError(f"All Groq models failed. Last error: {last_err}")

def fetch_job_links_from_search(target_platform="all"):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    results = []
    seen_urls = set()

    # 1. Search LinkedIn Guest API
    if target_platform in ("all", "linkedin"):
        linkedin_keywords = [
            'python developer',
            'backend developer',
            'django developer',
            'flask developer',
            'fastapi developer',
            'ai engineer',
            'llm engineer',
            'frontend developer',
            'fullstack developer',
            'web developer',
            'software tester',
            'qa engineer',
            'junior software engineer',
            'software engineer intern',
            'python intern',
            'backend intern',
            'frontend intern',
            'qa intern'
        ]
        add_scraper_log(f"Searching LinkedIn Guest API for {len(linkedin_keywords)} keywords...")
        for kw in linkedin_keywords:
            if SCRAPER_STOP_EVENT.is_set():
                break
            url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={quote_plus(kw)}&location=India&f_TPR=r604800&start=0"
            try:
                res = requests.get(url, headers=headers, timeout=10)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, 'html.parser')
                    for a in soup.find_all('a', href=True):
                        href = a['href']
                        if '/jobs/view/' in href:
                            clean_u = href.split("?")[0].split("#")[0].strip()
                            if clean_u not in seen_urls:
                                seen_urls.add(clean_u)
                                results.append({
                                    "url": clean_u,
                                    "source": "linkedin",
                                    "title": None,
                                    "snippet": None
                                })
                time.sleep(1.5)
            except Exception as e:
                add_scraper_log(f"LinkedIn search error for '{kw}': {e}")

    # 2. Search AOL / Yahoo for Naukri & Indeed
    aol_queries = [
        ('site:naukri.com "job-listings" "python"', 'naukri'),
        ('site:naukri.com "job-listings" "backend"', 'naukri'),
        ('site:naukri.com "job-listings" "ai engineer"', 'naukri'),
        ('site:naukri.com "job-listings" "frontend"', 'naukri'),
        ('site:naukri.com "job-listings" "fullstack"', 'naukri'),
        ('site:naukri.com "job-listings" "tester"', 'naukri'),
        ('site:naukri.com "job-listings" "software engineer"', 'naukri'),
        ('site:indeed.com "viewjob" "python" "india"', 'indeed'),
        ('site:indeed.com "viewjob" "backend" "india"', 'indeed'),
        ('site:indeed.com "viewjob" "ai engineer" "india"', 'indeed'),
        ('site:indeed.com "viewjob" "frontend" "india"', 'indeed'),
        ('site:indeed.com "viewjob" "fullstack" "india"', 'indeed'),
        ('site:indeed.com "viewjob" "tester" "india"', 'indeed'),
        ('site:indeed.com "viewjob" "software engineer" "india"', 'indeed'),
    ]
    
    if target_platform != "all":
        aol_queries = [q for q in aol_queries if q[1] == target_platform]
        
    add_scraper_log(f"Searching AOL/Yahoo for Naukri/Indeed ({len(aol_queries)} queries)...")
    for q_text, source_platform in aol_queries:
        if SCRAPER_STOP_EVENT.is_set():
            break
            
        for page_num in range(1, 4):
            if SCRAPER_STOP_EVENT.is_set():
                break
                
            start_idx = (page_num - 1) * 10 + 1
            
            simple_q = q_text
            if "site:naukri.com" in q_text:
                simple_q = q_text.replace("site:naukri.com", '"naukri.com"')
            elif "site:indeed.com" in q_text:
                simple_q = q_text.replace("site:indeed.com", '"indeed.com"')
                
            strategies = [
                ("AOL Search", f"https://search.aol.com/aol/search?q={quote_plus(q_text)}&b={start_idx}"),
                ("Yahoo Search", f"https://search.yahoo.com/search?q={quote_plus(q_text)}&b={start_idx}")
            ]
            if simple_q != q_text:
                strategies.append(("AOL (Simple)", f"https://search.aol.com/aol/search?q={quote_plus(simple_q)}&b={start_idx}"))
                strategies.append(("Yahoo (Simple)", f"https://search.yahoo.com/search?q={quote_plus(simple_q)}&b={start_idx}"))
                
            res = None
            engine_used = ""
            for name, url in strategies:
                if SCRAPER_STOP_EVENT.is_set():
                    break
                try:
                    res = requests.get(url, headers=headers, timeout=10)
                    if res.status_code == 200:
                        engine_used = name
                        break
                except Exception:
                    pass
                time.sleep(1)
                
            if res and res.status_code == 200:
                try:
                    soup = BeautifulSoup(res.text, 'html.parser')
                    q_links = 0
                    for li in soup.find_all(['li', 'div'], class_=re.compile(r'algo|compListing|dd')):
                        title_el = li.find('h3')
                        link_el = li.find('a', href=True)
                        if title_el and link_el:
                            title = title_el.get_text().strip()
                            href = link_el['href']
                            
                            ru_match = re.search(r'/RU=([^/]+)/', href)
                            actual_url = unquote(ru_match.group(1)) if ru_match else unquote(href)
                            
                            is_naukri = 'naukri.com/job-listings-' in actual_url
                            is_indeed = 'indeed.com/viewjob' in actual_url or 'indeed.com/rc/clk' in actual_url
                            
                            if is_naukri and source_platform == 'naukri':
                                clean_url = actual_url.split("?")[0].split("#")[0].strip()
                                if clean_url not in seen_urls:
                                    seen_urls.add(clean_url)
                                    results.append({"url": clean_url, "source": "naukri", "title": title, "snippet": ""})
                                    q_links += 1
                            elif is_indeed and source_platform == 'indeed':
                                clean_url = actual_url.split("#")[0].strip()
                                if clean_url not in seen_urls:
                                    seen_urls.add(clean_url)
                                    results.append({"url": clean_url, "source": "indeed", "title": title, "snippet": ""})
                                    q_links += 1
                    if q_links > 0:
                        add_scraper_log(f"Found {q_links} candidate links on {engine_used} (Page {page_num})")
                except Exception as e:
                    add_scraper_log(f"Parser error on {engine_used}: {e}")
            time.sleep(1.5)
            
    import random
    random.shuffle(results)
    return results

def job_scraper_thread_loop(target_platform, app_context):
    global SCRAPER_STATUS
    with app_context:
        SCRAPER_STOP_EVENT.clear()
        SCRAPER_STATUS["status"] = "Searching"
        SCRAPER_STATUS["current_action"] = "Searching job sites..."
        SCRAPER_STATUS["logs"] = []
        SCRAPER_STATUS["ai_thoughts"] = []
        
        try:
            links = fetch_job_links_from_search(target_platform)
            SCRAPER_STATUS["links_found"] = len(links)
            add_scraper_log(f"Found {len(links)} candidate links.")
        except Exception as e:
            SCRAPER_STATUS["status"] = "Failed"
            add_scraper_log(f"Search failed: {e}")
            return

        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        for idx, job_item in enumerate(links):
            if SCRAPER_STOP_EVENT.is_set(): break
            url = job_item["url"]
            source = job_item["source"]
            
            SCRAPER_STATUS["status"] = "Crawling"
            SCRAPER_STATUS["current_action"] = f"Processing {idx+1}/{len(links)}: {url}"
            SCRAPER_STATUS["processed_count"] = idx + 1
            
            # Check DB
            existing = Job.query.filter_by(apply_url=url).first()
            if existing:
                add_scraper_log(f"Skip: Already in DB: {url}")
                continue
                
            try:
                add_scraper_log(f"Fetching page details for: {url}")
                res = requests.get(url, headers=headers, timeout=10)
                if res.status_code != 200: continue
                
                soup = BeautifulSoup(res.text, 'html.parser')
                for s in soup(["script", "style", "nav", "footer", "header"]): s.decompose()
                text_content = re.sub(r'\s+', ' ', soup.get_text()).strip()[:4000]
                
                # Check for direct ATS links (lever or greenhouse)
                ats_match = re.search(r'https?://[^\s\'"]*(?:lever\.co|greenhouse\.io)[^\s\'"]*', text_content)
                apply_url = ats_match.group(0).strip().rstrip('.,;:)') if ats_match else url
                SCRAPER_STATUS["status"] = "Parsing"
                add_scraper_log(f"Waiting 5 seconds before calling Groq AI to be polite...")
                time.sleep(5)
                
                system_prompt = """
                You are an AI tech recruiter. Analyze this job text and return JSON ONLY.
                Required Keys:
                - is_fit (boolean: true if IT/software job in India/remote and fresh)
                - title (string)
                - company (string)
                - location (string)
                - job_type ("Job" or "Internship")
                - category ("Python", "Backend", "AI / LLM", "Frontend", "Fullstack", "QA / Testing")
                - experience_level (string)
                - salary (string or null)
                - skills (string list of 3-5 tech skills)
                - description (string)
                - course_match (string)
                - target_batch (string, graduation batches like "2025, 2026" or "Experience")
                """
                
                ai_output = call_groq_with_fallback([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Job Text:\n{text_content}"}
                ])
                
                cleaned = ai_output.strip().strip("`").replace("json", "").strip()
                parsed = json.loads(cleaned)
                
                if not parsed.get("is_fit"):
                    add_scraper_log(f"AI Filter: Not a match for junior role.")
                    time.sleep(10) # 10s cooldown
                    continue
                    
                # Duplication check
                duplicate = Job.query.filter(Job.title.ilike(parsed["title"]), Job.company.ilike(parsed["company"])).first()
                if duplicate:
                    add_scraper_log(f"Duplicate found: {parsed['title']} at {parsed['company']}")
                    time.sleep(10) # 10s cooldown
                    continue
                    
                skills_val = parsed.get("skills", "")
                if isinstance(skills_val, list):
                    skills_val = ", ".join(skills_val)
                elif not skills_val:
                    skills_val = ""

                new_job = Job(
                    title=parsed["title"],
                    company=parsed["company"],
                    location=parsed.get("location", "India"),
                    job_type=parsed.get("job_type", "Job"),
                    category=parsed.get("category", "Python"),
                    experience_level=parsed.get("experience_level", "Freshers"),
                    salary=parsed.get("salary"),
                    skills=skills_val,
                    description=parsed.get("description", ""),
                    course_match=parsed.get("course_match"),
                    apply_url=apply_url,
                    target_batch=parsed.get("target_batch", "2025, 2026")
                )
                db.session.add(new_job)
                db.session.commit()
                SCRAPER_STATUS["added_count"] += 1
                add_scraper_log(f"✅ Added: {new_job.title} at {new_job.company}")
                add_ai_thought(f"Approved: '{new_job.title}' fits criteria.")
                
                # Cooling period to prevent rate limiting
                add_scraper_log("Sleeping 15 seconds (cooling period) before next candidate...")
                time.sleep(15)
                
            except Exception as e:
                add_scraper_log(f"Error processing link: {e}")
                time.sleep(10)
                
        SCRAPER_STATUS["status"] = "Completed"
        SCRAPER_STATUS["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        add_scraper_log("✅ Job scraper run finished.")

def job_matcher_thread_loop(user_id, app_context):
    global MATCHER_STATUS
    with app_context:
        MATCHER_STATUS["status"] = "Running"
        MATCHER_STATUS["logs"] = []
        add_matcher_log("Starting AI match evaluations against candidate resume...")
        
        config = SearchConfig.query.filter_by(user_id=user_id).first()
        if not config or not config.resume_text:
            add_matcher_log("Aborted: No resume uploaded yet.")
            MATCHER_STATUS["status"] = "Failed"
            return
            
        # Get active jobs
        active_jobs = Job.query.filter_by(is_active=True).all()
        add_matcher_log(f"Evaluating {len(active_jobs)} active jobs from DB...")
        
        for idx, job in enumerate(active_jobs):
            # Exclude already matched jobs
            existing_match = JobMatch.query.filter_by(job_id=job.id, user_id=user_id).first()
            if existing_match:
                continue
                
            add_matcher_log(f"Matching role {idx+1}/{len(active_jobs)}: '{job.title}' at {job.company}...")
            
            prompt = f"""
            Analyze the suitability of this candidate's resume for the following job description.
            
            Candidate Resume:
            {config.resume_text}
            
            Job Title: {job.title}
            Company: {job.company}
            Location: {job.location}
            Description:
            {job.description[:2000]}
            
            Return JSON format only:
            {{
              "fit_score": integer between 0 and 100,
              "explanation": "concise explanation of why they fit or do not fit",
              "recruiter_pitch": "a 2-3 sentence personalized outreach email or pitch"
            }}
            """
            try:
                ai_output = call_groq_with_fallback([
                    {"role": "system", "content": "You are a professional AI recruiter. Output JSON only."},
                    {"role": "user", "content": prompt}
                ], user_id=user_id)
                cleaned = ai_output.strip().strip("`").replace("json", "").strip()
                res = json.loads(cleaned)
                
                match = JobMatch(
                    user_id=user_id,
                    job_id=job.id,
                    fit_score=res.get("fit_score", 0),
                    explanation=res.get("explanation", ""),
                    recruiter_pitch=res.get("recruiter_pitch", "")
                )
                db.session.add(match)
                db.session.commit()
                add_matcher_log(f"🎯 Match Evaluated: '{job.title}' -> {match.fit_score}% Score.")
            except Exception as e:
                add_matcher_log(f"Groq matching failed for job {job.id}: {e}")
                
        MATCHER_STATUS["status"] = "Completed"
        add_matcher_log("✅ Resume AI matching completed successfully.")

# Routes
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("matcher_page"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        
        if not username or not email or not password:
            flash("All fields are required.", "error")
            return render_template("register.html")
            
        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("register.html")
            
        existing_user = User.query.filter((User.username == username) | (User.email == email)).first()
        if existing_user:
            flash("Username or email already exists.", "error")
            return render_template("register.html")
            
        new_user = User(
            username=username,
            email=email,
            password_hash=generate_password_hash(password)
        )
        db.session.add(new_user)
        db.session.commit()
        
        # Create SearchConfig for new user
        new_config = SearchConfig(user_id=new_user.id)
        db.session.add(new_config)
        db.session.commit()
        
        flash("Registration successful! You can now log in.", "success")
        return redirect(url_for("login"))
        
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("matcher_page"))
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        
        user = User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid email or password.", "error")
            return render_template("login.html")
            
        login_user(user)
        flash("Logged in successfully!", "success")
        if user.email == "rohithbuildsofficial@gmail.com":
            return redirect(url_for("scraper_page"))
        return redirect(url_for("matcher_page"))
        
    return render_template("login.html")

@app.route("/logout")
def logout():
    logout_user()
    flash("Logged out successfully.", "info")
    return redirect(url_for("index"))

@app.route("/")
def index():
    return render_template("home.html")

@app.route("/jobs")
def jobs_page():
    jobs = Job.query.filter_by(is_active=True).order_by(Job.created_at.desc()).all()
    config = SearchConfig.query.filter_by(user_id=current_user.id).first() if current_user.is_authenticated else None
    matches = JobMatch.query.filter_by(user_id=current_user.id).all() if current_user.is_authenticated else []
    matches_dict = {m.job_id: m for m in matches}
    return render_template(
        "jobs.html",
        jobs=jobs,
        config=config,
        matches_dict=matches_dict
    )

@app.route("/matcher")
@login_required
def matcher_page():
    config = SearchConfig.query.filter_by(user_id=current_user.id).first()
    if not config:
        config = SearchConfig(user_id=current_user.id)
        db.session.add(config)
        db.session.commit()
    matches = JobMatch.query.join(Job).filter(Job.is_active == True, JobMatch.user_id == current_user.id).order_by(JobMatch.fit_score.desc()).all()
    matched_jobs = [{"job": m.job, "match": m} for m in matches]
    return render_template(
        "matcher.html",
        config=config,
        matched_jobs=matched_jobs,
        matcher_status=MATCHER_STATUS
    )

@app.route("/scraper")
def scraper_page():
    admin_user = User.query.filter_by(email="rohithbuildsofficial@gmail.com").first()
    config = SearchConfig.query.filter_by(user_id=admin_user.id).first() if admin_user else SearchConfig.query.first()
    if not config:
        config = SearchConfig()
        db.session.add(config)
        db.session.commit()
    is_admin = current_user.is_authenticated and current_user.email == "rohithbuildsofficial@gmail.com"
    return render_template(
        "scraper.html",
        scraper_status=SCRAPER_STATUS,
        matcher_status=MATCHER_STATUS,
        config=config,
        is_admin=is_admin
    )

# Legacy dashboard (keep for backward compat)
@app.route("/dashboard")
def dashboard():
    return redirect(url_for("jobs_page"))

@app.route("/jobs/scraper/start", methods=["POST"])
def start_scraper():
    if not current_user.is_authenticated or current_user.email != "rohithbuildsofficial@gmail.com":
        return jsonify({"success": False, "error": "Unauthorized. Admin permissions required."}), 403
        
    global SCRAPER_THREAD
    if SCRAPER_THREAD and SCRAPER_THREAD.is_alive():
        return jsonify({"success": False, "error": "Already running"})
    
    data = request.get_json(silent=True) or {}
    platform = data.get("platform", "all")
    
    SCRAPER_THREAD = threading.Thread(
        target=job_scraper_thread_loop, 
        args=(platform, app.app_context()), 
        daemon=True
    )
    SCRAPER_THREAD.start()
    return jsonify({"success": True})

@app.route("/jobs/scraper/stop", methods=["POST"])
def stop_scraper():
    if not current_user.is_authenticated or current_user.email != "rohithbuildsofficial@gmail.com":
        return jsonify({"success": False, "error": "Unauthorized. Admin permissions required."}), 403
        
    SCRAPER_STOP_EVENT.set()
    return jsonify({"success": True})

@app.route("/jobs/scraper/status")
def scraper_status():
    return jsonify(SCRAPER_STATUS)

@app.route("/config/preferences", methods=["POST"])
def save_preferences():
    if not current_user.is_authenticated or current_user.email != "rohithbuildsofficial@gmail.com":
        return jsonify({"success": False, "error": "Unauthorized. Admin permissions required."}), 403
        
    admin_user = User.query.filter_by(email="rohithbuildsofficial@gmail.com").first()
    if not admin_user:
        return jsonify({"success": False, "error": "Admin user not found."}), 500
        
    config = SearchConfig.query.filter_by(user_id=admin_user.id).first()
    if not config:
        config = SearchConfig(user_id=admin_user.id)
        db.session.add(config)
    config.target_roles = request.form.get("target_roles", "")
    config.target_locations = request.form.get("target_locations", "")
    config.groq_api_key = request.form.get("groq_api_key", "")
    db.session.commit()
    return jsonify({"success": True})

@app.route("/config/resume", methods=["POST"])
@login_required
def upload_resume():
    if 'resume' not in request.files:
        return jsonify({"success": False, "message": "No file uploaded"}), 400
    file = request.files['resume']
    if file.filename == '':
        return jsonify({"success": False, "message": "No file selected"}), 400
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({"success": False, "message": "Only PDF files supported"}), 400
        
    try:
        file_bytes = file.read()
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        extracted_text = ""
        for page in reader.pages:
            extracted_text += page.extract_text() or ""
        extracted_text = extracted_text.strip()
        
        if not extracted_text:
            return jsonify({"success": False, "message": "Could not extract text from PDF."}), 400
            
        config = SearchConfig.query.filter_by(user_id=current_user.id).first()
        if not config:
            config = SearchConfig(user_id=current_user.id)
            db.session.add(config)
            
        config.resume_text = extracted_text
        config.resume_filename = secure_filename(file.filename)
        
        # Clear previous match scores for this user
        JobMatch.query.filter_by(user_id=current_user.id).delete()
        db.session.commit()
        
        return jsonify({
            "success": True, 
            "message": "Resume parsed successfully.",
            "filename": config.resume_filename
        })
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/jobs/matcher/start", methods=["POST"])
@login_required
def trigger_matching():
    global MATCHER_THREAD
    if MATCHER_THREAD and MATCHER_THREAD.is_alive():
        return jsonify({"success": False, "error": "Matcher already running"})
        
    MATCHER_THREAD = threading.Thread(target=job_matcher_thread_loop, args=(current_user.id, app.app_context()), daemon=True)
    MATCHER_THREAD.start()
    return jsonify({"success": True})

@app.route("/jobs/matcher/status")
@login_required
def matcher_status():
    return jsonify(MATCHER_STATUS)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001))
    app.run(host="0.0.0.0", port=port, debug=True)
