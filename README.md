# Autonomous Job Scraper & AI Matcher

A standalone module extracted from the Rohith Builds Admin Control Center. It autonomously queries search engines, grabs junior/fresher development jobs from LinkedIn Guest feeds, Naukri & Indeed listings via gateways, and evaluates role fit using Groq AI before committing them to a local SQLite database.

## Installation & Execution

1. Navigate to the project folder:
   ```bash
   cd d:/job-scraper-agent
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Start the Flask application:
   ```bash
   python app.py
   ```

4. Open the control console in your browser:
   [http://127.0.0.1:8001](http://127.0.0.1:8001)
