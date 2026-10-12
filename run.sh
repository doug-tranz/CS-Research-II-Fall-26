rm example_registration.db
sqlite3 -header -column example_registration.db < setup.sql
python3 -m venv myenv
source myenv/bin/activate
python3 semantic_check.py