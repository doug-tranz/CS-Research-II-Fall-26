import requests, json

resp = requests.post("http://localhost:11434/api/chat", json={
    "model": "llama3.1:8b",   # <-- your model here
    "messages": [
        {"role": "system", "content": "Respond with only a SQL transaction. No prose."},
        {"role": "user", "content":
            "Schema:\n"
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, "
            "balance INTEGER NOT NULL CHECK (balance >= 0));\n\n"
            "Task: transfer 50 from account 1 to account 2."},
    ],
    "stream": False,
    "options": {"temperature": 0},
})

print(json.dumps(resp.json(), indent=2))