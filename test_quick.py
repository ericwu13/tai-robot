"""Quick end-to-end test matching test_connection.py flow."""
import os, sys, time, threading, yaml

sdk_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "CapitalAPI_2.13.57", "CapitalAPI_2.13.57_PythonExample", "SKDLLPythonTester",
)
libs_path = os.path.join(sdk_path, "libs")
os.add_dll_directory(libs_path)
os.environ["PATH"] = libs_path + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, sdk_path)
from SKDLLPython import SK

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.yaml"), "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
creds = cfg["credentials"]

tick_count = 0

def on_conn(lid, code):
    status = {0: "connected", 1: "disconnected", 2: "lost", 3: "reconnecting"}
    print(f"  CONNECTION: {lid} code={code} ({status.get(code, code)})")

def on_reply(m1, m2):
    print(f"  REPLY: {m1} | {m2}")

def on_complete(lid):
    print(f"  COMPLETE: {lid}")

def on_tick(market_no, stockno, ptr, date, time_hms, time_ms, bid, ask, close, qty, simulate):
    global tick_count
    tick_count += 1
    h, m, s = time_hms // 10000, (time_hms % 10000) // 100, time_hms % 100
    sim = " [SIM]" if simulate else ""
    print(f"  TICK #{tick_count}: {stockno} {h:02d}:{m:02d}:{s:02d} C={close} Q={qty} B={bid} A={ask}{sim}")

def on_quote(market_no, stock_no):
    try:
        s = SK.SKQuoteLib_GetStockByStockNo(market_no, stock_no)
        print(f"  QUOTE: {s.strStockNo} {s.strStockName} O={s.nOpen} H={s.nHigh} L={s.nLow} C={s.nClose} V={s.nTQty}")
    except Exception as e:
        print(f"  QUOTE err: {e}")

SK.OnConnection(on_conn)
SK.OnReplyMessage(on_reply)
SK.OnComplete(on_complete)
SK.OnNotifyTicksLONG(on_tick)
SK.OnNotifyQuoteLONG(on_quote)

# Step 1: Login
print("=== 1. Login ===")
result = SK.Login(creds["user_id"], creds["password"], 0)
print(f"  Code: {result.Code} -> {SK.GetMessage(result.Code) if result.Code != 0 else 'OK'}")
if result.Code != 0:
    sys.exit(1)
time.sleep(1)

# Step 2: Connect services
print("\n=== 2. Connect Services ===")
for svc, name in [(0, "Reply"), (1, "Quote"), (4, "Order")]:
    code = SK.ManageServerConnection(creds["user_id"], 0, svc)
    print(f"  {name}({svc}): code={code} -> {SK.GetMessage(code) if code != 0 else 'OK'}")
    time.sleep(1)
time.sleep(2)

# Step 3: Load commodity (market 0 first, then 2)
print("\n=== 3. Load Commodity ===")
code = SK.LoadCommodity(0)
print(f"  LoadCommodity(0): code={code} -> {SK.GetMessage(code) if code != 0 else 'OK'}")
time.sleep(3)
code = SK.LoadCommodity(2)
print(f"  LoadCommodity(2): code={code} -> {SK.GetMessage(code) if code != 0 else 'OK'}")
time.sleep(3)

# Step 4: Subscribe TX00
print("\n=== 4. Subscribe TX00 ===")
code = SK.SKQuoteLib_RequestStocks("TX00")
print(f"  RequestStocks(TX00): code={code} -> {SK.GetMessage(code) if code != 0 else 'OK'}")
code = SK.SKQuoteLib_RequestTicks(0, "TX00")
print(f"  RequestTicks(TX00): code={code} -> {SK.GetMessage(code) if code != 0 else 'OK'}")
time.sleep(2)

# Get quote snapshot
try:
    s = SK.SKQuoteLib_GetStockByStockNo(2, "TX00")
    print(f"\n  TX00 Quote: O={s.nOpen} H={s.nHigh} L={s.nLow} C={s.nClose} V={s.nTQty} Dec={s.nDecimal}")
except Exception as e:
    print(f"  Quote error: {e}")

# Listen
print(f"\n=== Listening 15s ===")
try:
    start = time.time()
    while time.time() - start < 15:
        time.sleep(0.5)
except KeyboardInterrupt:
    pass

print(f"\n=== Done. {tick_count} ticks ===")
