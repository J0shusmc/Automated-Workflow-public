import requests
import time

# Replace with the actual dashboard URL
DASHBOARD_URL = "https://example.com/dashboard"

def smoke_test_dashboard():
    try:
        response = requests.get(DASHBOARD_URL, timeout=10)
        if response.status_code == 200:
            print(f"✅ Dashboard is reachable: {response.status_code}")
        else:
            print(f"❌ Dashboard returned status: {response.status_code}")
    except requests.RequestException as e:
        print(f"❌ Failed to reach dashboard: {e}")

if __name__ == "__main__":
    smoke_test_dashboard()