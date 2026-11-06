"""Quick self-healing demo using error_injection for immediate detection.

This script uses error_injection instead of memory_hog because errors are 
detected immediately (within one 15-second metrics cycle), whereas memory
pressure requires multiple cycles to establish a pattern.
"""
import json
import sys
import time
import urllib.request
from datetime import datetime

# Service ports
CENTRAL_LEDGER_PORT = 8012
API_BASE = "http://localhost:8000"

def http_get(url: str, timeout: int = 10) -> dict:
    """Make HTTP GET request and return JSON response."""
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)

def http_post(url: str, body: dict, timeout: int = 15) -> dict:
    """Make HTTP POST request and return JSON response."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)

print("=" * 80)
print("QUICK SELF-HEALING DEMO - ERROR INJECTION")
print("=" * 80)
print("\nThis demo uses error_injection fault for faster detection.")
print("Errors are detected within 15-30 seconds.\n")

# Step 1: Inject error fault
print("[1] Injecting error_injection fault into central-ledger...")
fault_result = http_post(
    f"http://localhost:{CENTRAL_LEDGER_PORT}/sim/faults",
    {
        "kind": "error_injection",
        "duration_seconds": 180,
        "magnitude": 0.50,  # 50% error rate - high enough to detect quickly
        "injected_by": "demo_quick_healing.py",
        "notes": "Quick healing demo - error injection",
    }
)
fault_time = datetime.fromisoformat(
    fault_result["injected"]["started_at"].replace("Z", "+00:00")
)
print(f"✓ Fault injected (ID: {fault_result['injected']['fault_id']})")
print(f"  Magnitude: {fault_result['injected']['magnitude']} (50% error rate)")
print(f"  Duration: 180 seconds")

# Step 2: Wait for anomaly detection (errors are detected quickly)
print("\n[2] Waiting for anomaly detection (timeout: 60s)...")
print("   Checking every 5 seconds...")

start = time.time()
anomaly = None

while time.time() - start < 60:
    time.sleep(5)
    elapsed = int(time.time() - start)
    print(f"   {elapsed}s elapsed...")
    
    try:
        anomalies = http_get(f"{API_BASE}/api/anomalies?service=central-ledger&limit=10")
        
        # Look for anomaly after fault injection
        for anom in anomalies:
            anom_time = datetime.fromisoformat(anom["detected_at"].replace("Z", "+00:00"))
            if anom_time >= fault_time:
                anomaly = anom
                break
        
        if anomaly:
            detection_time = int(time.time() - start)
            print(f"\n✓ Anomaly detected after {detection_time} seconds!")
            print(f"  Anomaly ID: {anomaly['anomaly_id']}")
            print(f"  Kind: {anomaly['kind']}")
            print(f"  Severity: {anomaly['severity']}")
            print(f"  Score: {anomaly['score']:.3f}")
            print(f"  Description: {anomaly['description'][:100]}...")
            
            # Clear the fault immediately to prevent anomaly spam
            print(f"\n[Cleanup] Clearing fault to prevent additional anomalies...")
            try:
                request = urllib.request.Request(
                    f"http://localhost:{CENTRAL_LEDGER_PORT}/sim/faults",
                    method="DELETE"
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    result = json.load(response)
                    print(f"✓ Fault cleared ({result.get('cleared', 0)} fault(s) removed)")
            except Exception as e:
                print(f"  Warning: Could not clear fault: {e}")
            
            break
            
    except Exception as e:
        print(f"   Error checking anomalies: {e}")

if not anomaly:
    print("\n✗ No anomaly detected within 60 seconds.")
    print("   This could mean:")
    print("   - Metrics agent hasn't collected new metrics yet (waits 15s)")
    print("   - Anomaly cooldown is active (30s between detections)")
    print("   - Error magnitude not high enough")
    sys.exit(1)

# Step 3: Check for incident
print("\n[3] Checking for RCA incident creation...")
time.sleep(5)

try:
    incidents = http_get(f"{API_BASE}/api/incidents?limit=20")
    
    # Look for incident with this anomaly
    incident = None
    for inc in incidents:
        if anomaly['anomaly_id'] in inc.get('anomaly_ids', []):
            incident = inc
            break
    
    if incident:
        print(f"✓ Incident created: {incident['incident_id']}")
        print(f"  Severity: {incident['severity']}")
        if incident.get('root_cause_hypotheses'):
            hypothesis = incident['root_cause_hypotheses'][0]
            print(f"  Root Cause: {hypothesis['category']}")
            print(f"  Confidence: {hypothesis['confidence']:.1%}")
    else:
        print("  No incident created yet (RCA may need more time)")
        
except Exception as e:
    print(f"  Error checking incidents: {e}")

# Step 4: Summary
print("\n" + "=" * 80)
print("DEMO COMPLETE")
print("=" * 80)
print("\n✅ Self-healing system successfully detected the fault!")
print(f"\nTimeline:")
print(f"  T+0s:  Fault injected (50% error rate)")
print(f"  T+{detection_time}s: Anomaly detected")
print(f"\nNext steps in the full workflow:")
print("  • RCA engine analyzes root cause")
print("  • Incident created and correlated with other anomalies")
print("  • Healing engine evaluates playbook eligibility")
print("  • Automated action executed (if configured)")
print(f"\nView the dashboard: http://localhost:5173/")
print(f"\n✓ Fault automatically cleared after detection")
print(f"  No additional anomalies will be generated")
print(f"\nTo inject another fault:")
print(f"  python scripts/demo_quick_healing.py")
print(f"\nTo check current state:")
print(f"  curl http://localhost:8012/sim/faults")
print(f"  curl {API_BASE}/api/anomalies?service=central-ledger")
