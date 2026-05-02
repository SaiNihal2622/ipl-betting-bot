import os
import signal
import subprocess

def kill_pid(file):
    if os.path.exists(file):
        try:
            with open(file, "r") as f:
                pid = int(f.read().strip())
            print(f"[*] Stopping process {pid} from {file}...")
            if os.name == 'nt':
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, 9)
            os.remove(file)
            print(f"[+] Stopped.")
        except Exception as e:
            print(f"[-] Error stopping {file}: {e}")

def main():
    kill_pid("bot.pid")
    kill_pid("server.pid")
    print("[!] All background processes cleaned up.")

if __name__ == "__main__":
    main()
