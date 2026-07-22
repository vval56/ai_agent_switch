import os
import json

MEMORY_FILE = os.path.join(os.path.dirname(__file__), "../../.agent_memory.json")
MEMORY_FILE = os.path.normpath(MEMORY_FILE)

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"history": [], "current_plan": None}

def save_memory(memory):
    with open(MEMORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)
