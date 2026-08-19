import subprocess

def check_ram():
    print("--- Inspecting VPS Memory (RAM) and Disk Swap Space ---")
    try:
        res = subprocess.run(["free", "-h"], capture_output=True, text=True, check=True)
        print(res.stdout)
    except Exception as e:
        print(f"Failed to run free -h: {e}")
        
    print("--- Inspecting CPU & System Load ---")
    try:
        res = subprocess.run(["uptime"], capture_output=True, text=True, check=True)
        print(res.stdout)
    except Exception as e:
        print(f"Failed to run uptime: {e}")

if __name__ == "__main__":
    check_ram()
