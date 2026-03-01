"""List all available futures, with proper Big5 decoding, output to file."""
import os, sys, time, threading, yaml, ctypes

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

complete_event = threading.Event()
SK.OnConnection(lambda lid, code: None)
SK.OnReplyMessage(lambda m1, m2: None)
SK.OnComplete(lambda lid: complete_event.set())

result = SK.Login(creds["user_id"], creds["password"], 0)
if result.Code != 0:
    sys.exit(1)

for svc in [0, 1, 4]:
    SK.ManageServerConnection(creds["user_id"], 0, svc)
    time.sleep(1)
time.sleep(2)

complete_event.clear()
SK.LoadCommodity(0)
complete_event.wait(timeout=10)

complete_event.clear()
SK.LoadCommodity(2)
complete_event.wait(timeout=10)
time.sleep(1)

ptr = SK._dll.SKQuoteLib_RequestStockList(2)
raw_bytes = ctypes.cast(ptr, ctypes.c_char_p).value
raw = raw_bytes.decode("cp950", errors="replace") if raw_bytes else ""

out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "futures_list.txt")
with open(out_path, "w", encoding="utf-8") as out:
    segments = [s for s in raw.split('%') if s]
    i = 0
    while i + 2 < len(segments):
        try:
            type_no = int(segments[i])
        except ValueError:
            i += 3
            continue

        type_name = segments[i + 1]
        raw_items = segments[i + 2]
        entries = [e.strip() for e in raw_items.split(';') if e.strip()]

        out.write(f"\n=== Type {type_no}: {type_name} ({len(entries)} contracts) ===\n")

        for entry in entries:
            fields = entry.split(',')
            if len(fields) >= 4:
                symbol, name, code, expiry = fields[0], fields[1], fields[2], fields[3]
                out.write(f"  {symbol:<20} {name:<24} code={code:<16} expires={expiry}\n")

        i += 3

print(f"Written to {out_path}")
