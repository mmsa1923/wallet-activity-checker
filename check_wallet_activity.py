import os
import time
import requests
import json

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
DUNE_API_KEY = os.environ.get("DUNE_API_KEY", "")
DUNE_QUERY_ID = os.environ.get("DUNE_QUERY_ID", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

CHAIN_ID = 8453  # Base
API_URL = "https://api.etherscan.io/v2/api"
STATE_FILE = "activity_state.json"
ACTIVE_THRESHOLD_HOURS = float(os.environ.get("ACTIVE_THRESHOLD_HOURS", "24"))
RATE_LIMIT_SLEEP_SEC = 0.25

def get_wallets_from_dune():
    print(f"[i] Se descarcă datele de la Dune Query ID: {DUNE_QUERY_ID}...")
    url = f"https://api.dune.com/api/v1/query/{DUNE_QUERY_ID}/results"
    headers = {"X-Dune-API-Key": DUNE_API_KEY}
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        rows = data.get("result", {}).get("rows", [])
        wallets = [str(row["wallet"]).lower().strip() for row in rows if "wallet" in row]
        print(f"[+] Am descărcat cu succes {len(wallets)} adrese din Dune!")
        return list(set(wallets))
    except Exception as e:
        print(f"[!] Eroare critică la descărcarea din Dune: {e}")
        return []

def get_last_tx_timestamp(wallet_address):
    params = {
        "chainid": CHAIN_ID,
        "module": "account",
        "action": "txlist",
        "address": wallet_address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": 1,
        "sort": "desc",
        "apikey": ETHERSCAN_API_KEY,
    }
    resp = requests.get(API_URL, params=params, timeout=10)
    data = resp.json()
    if data.get("status") == "1" and data.get("result"):
        return int(data["result"][0]["timeStamp"])
    return None

def trimite_telegram(mesaj):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Eroare Telegram: {e}")

def main():
    if not DUNE_API_KEY or not DUNE_QUERY_ID:
        print("[!] Lipsesc cheile DUNE_API_KEY sau DUNE_QUERY_ID!")
        return

    wallets = get_wallets_from_dune()
    if not wallets:
        print("[!] Nu s-a găsit nicio adresă de verificat.")
        return

    previous_state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                previous_state = json.load(f)
        except:
            pass

    now = time.time()
    current_state = {}
    newly_active = []

    for i, addr in enumerate(wallets, start=1):
        try:
            last_ts = get_last_tx_timestamp(addr)
        except Exception:
            time.sleep(RATE_LIMIT_SLEEP_SEC)
            continue

        time.sleep(RATE_LIMIT_SLEEP_SEC)

        if last_ts is None:
            current_state[addr] = {"active": False}
            continue

        hours_since = (now - last_ts) / 3600
        is_active = hours_since <= ACTIVE_THRESHOLD_HOURS
        current_state[addr] = {"active": is_active}

        was_active_before = previous_state.get(addr, {}).get("active", False)
        if is_active and not was_active_before:
            newly_active.append(addr)

        if i % 20 == 0:
            print(f"[i] Verificate {i}/{len(wallets)}...")

    with open(STATE_FILE, "w") as f:
        json.dump(current_state, f, indent=2)

    if newly_active:
        mesaj = f"🚨 *{len(newly_active)} SNIPERI DIN DUNE AU DEVENIT ACTIVI!* 🚨\n\n"
        for addr in newly_active[:15]:
            mesaj += f"👤 `{addr}`\n🔗 [BaseScan](https://basescan.org/address/{addr})\n\n"
        if len(newly_active) > 15:
            mesaj += f"🔍 ...și încă {len(newly_active) - 15} portofele active."
        trimite_telegram(mesaj)
        print("[+] Alertă trimisă pe Telegram!")

if __name__ == "__main__":
    main()
