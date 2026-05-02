import os, httpx
from dotenv import load_dotenv

load_dotenv()

MIMO_KEY = os.getenv("MIMO_API_KEY")
MIMO_MODEL = os.getenv("MIMO_MODEL", "mimo-v2.5-pro")

def test_mimo():
    print(f"Testing Mimo with model: {MIMO_MODEL}")
    if not MIMO_KEY:
        print("Error: MIMO_API_KEY not found in .env")
        return

    url = "https://api.xiaomimimo.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {MIMO_KEY}"}
    body = {
        "model": MIMO_MODEL,
        "messages": [{"role": "user", "content": "Return the word 'SUCCESS' if you can read this."}],
        "max_tokens": 10
    }
    
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=15)
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            print(f"Response: {r.json()['choices'][0]['message']['content']}")
        else:
            print(f"Error Body: {r.text}")
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    test_mimo()
