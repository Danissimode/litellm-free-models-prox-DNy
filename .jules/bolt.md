## 2024-06-24 - Fast path parsing in probe aggregation
**Learning:** In Python, `json.loads` and `datetime.fromisoformat` are slow on scale (e.g. 200k+ rows). Using basic string slicing (`line.startswith(...)` and `line.find(...)`) and comparing ISO 8601 string dates directly instead of converting to datetime objects is up to 5x faster when filtering out a majority of lines.
**Action:** When filtering log files based on ISO 8601 timestamps, prefer string slicing and string comparison to bypass JSON and datetime parsing for rows that are ignored.
