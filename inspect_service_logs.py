import subprocess
import os

def check_logs():
    print("--- Inspecting Last 100 Lines of systemd trade-analyser service logs ---")
    try:
        res = subprocess.run(
            ["journalctl", "-u", "trade-analyser", "-n", "100", "--no-pager"],
            capture_output=True,
            text=True,
            check=True
        )
        print(res.stdout)
    except Exception as e:
        print(f"Failed to fetch systemd logs: {e}")
        
    print("\n--- Inspecting Last 100 Lines of /root/trade-analyser/analyser.log ---")
    log_path = "/root/trade-analyser/analyser.log"
    if os.path.exists(log_path):
        try:
            with open(log_path, "r") as f:
                lines = f.readlines()
                for l in lines[-100:]:
                    print(l, end="")
        except Exception as e:
            print(f"Failed to read analyser.log: {e}")
    else:
        print("analyser.log does not exist.")

if __name__ == "__main__":
    check_logs()
