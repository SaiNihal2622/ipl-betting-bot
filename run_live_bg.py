import subprocess
import sys
import os
import time

def main():
    print("[*] Initializing Background IPL Trader (Cloudbet Only)...")
    
    # Environment setup
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    
    # Detached process flag for Windows
    DETACHED_PROCESS = 0x00000008
    
    log_path = os.path.abspath("ipl_live.log")
    
    print(f"[*] Logging to: {log_path}")
    
    # Start the bot
    try:
        with open(log_path, "a", encoding="utf-8") as log_file:
            p = subprocess.Popen(
                [sys.executable, "cloudbet_live.py"],
                stdout=log_file,
                stderr=log_file,
                creationflags=DETACHED_PROCESS,
                env=env,
                cwd=os.getcwd()
            )
        
        print(f"[+] Bot started successfully in background (PID: {p.pid})")
        print("[!] You can now close this terminal. The bot will keep running.")
        print("[!] Monitor progress via dashboard.html or ipl_live.log")
        
        # Save PID for stopping later
        with open("bot.pid", "w") as f:
            f.write(str(p.pid))
            
    except Exception as e:
        print(f"[-] Failed to start bot: {e}")

if __name__ == "__main__":
    main()
