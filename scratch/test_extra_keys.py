import os, httpx
from dotenv import load_dotenv
load_dotenv()

GROQ = os.getenv("GROQ_API_KEY", "")
NVIDIA = os.getenv("NVIDIA_API_KEY", "")

print("=== GROQ TEST ===")
if GROQ:
    try:
        r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                       headers={"Authorization": f"Bearer {GROQ}"},
                       json={"model": "llama-3.1-70b-versatile",
                             "messages": [{"role": "user", "content": "Hi"}]},
                       timeout=10)
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            print("Response:", r.json()["choices"][0]["message"]["content"])
        else:
            print("Error:", r.text)
    except Exception as e:
        print("Error:", e)

print("\n=== NVIDIA TEST ===")
if NVIDIA:
    try:
        r = httpx.post("https://integrate.api.nvidia.com/v1/chat/completions",
                       headers={"Authorization": f"Bearer {NVIDIA}"},
                       json={"model": "meta/llama-3.1-405b-instruct",
                             "messages": [{"role": "user", "content": "Hi"}]},
                       timeout=10)
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            print("Response:", r.json()["choices"][0]["message"]["content"])
        else:
            print("Error:", r.text)
    except Exception as e:
        print("Error:", e)
