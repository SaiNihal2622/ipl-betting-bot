import http.server
import socketserver
import threading
import os
import json
import subprocess
import sys
import time

PORT = 8080

class VibeHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # Serve API endpoints
        if self.path == '/api/stats':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            if os.path.exists('trader_stats.json'):
                try:
                    with open('trader_stats.json', 'r') as f:
                        self.wfile.write(f.read().encode())
                except:
                    self.wfile.write(b'{}')
            else:
                self.wfile.write(b'{}')
        elif self.path == '/api/logs':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            if os.path.exists('ipl_live.log'):
                try:
                    # Read last 200 lines to keep it fast
                    with open('ipl_live.log', 'r', encoding='utf-8') as f:
                        lines = f.readlines()
                        self.wfile.write("".join(lines[-200:]).encode())
                except Exception as e:
                    self.wfile.write(f"Error reading logs: {e}".encode())
            else:
                self.wfile.write(b"Waiting for log stream...")
        elif self.path == '/':
            self.path = '/dashboard.html'
            return http.server.SimpleHTTPRequestHandler.do_GET(self)
        else:
            return http.server.SimpleHTTPRequestHandler.do_GET(self)

def run_server():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), VibeHandler) as httpd:
        print(f"=========================================================")
        print(f"[*] VIBE TRADER DASHBOARD: http://localhost:{PORT}")
        print(f"=========================================================")
        httpd.serve_forever()

if __name__ == "__main__":
    # 1. Kill any existing bot processes
    print("[*] Cleaning up existing processes...")
    if os.path.exists("bot.pid"):
        try:
            with open("bot.pid", "r") as f:
                pid = int(f.read().strip())
                if os.name == 'nt':
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                else:
                    os.kill(pid, 9)
        except: pass

    # 2. Start the Bot in Background
    print("[*] Starting Cloudbet Sniper Bot (Background)...")
    DETACHED_PROCESS = 0x00000008 if os.name == 'nt' else 0
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    
    with open("ipl_live.log", "a", encoding="utf-8") as log_file:
        p = subprocess.Popen(
            [sys.executable, "cloudbet_live.py"],
            stdout=log_file,
            stderr=log_file,
            creationflags=DETACHED_PROCESS,
            env=env
        )
    
    with open("bot.pid", "w") as f:
        f.write(str(p.pid))
    
    print(f"[+] Bot active (PID: {p.pid})")
    
    # Save server PID
    with open("server.pid", "w") as f:
        f.write(str(os.getpid()))

    # 3. Start the Web Server
    run_server()
