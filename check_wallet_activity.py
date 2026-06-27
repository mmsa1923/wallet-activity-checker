import os
import time
import requests
import json

ALCHEMY_RPC_URL = os.environ.get("ALCHEMY_RPC_URL", "")  # ex: https://base-mainnet.g.alchemy.com/v2/CHEIA_TA
DUNE_API_KEY = os.environ.get("DUNE_API_KEY", "")
DUNE_QUERY_ID = os.environ.get("DUNE_QUERY_ID", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE = "activity_state.json"
ACTIVE_THRESHOLD_HOURS = float(os.environ.get("ACTIVE_THRESHOLD_HOURS", "24"))
RATE_LIMIT_SLEEP_SEC = 0.1

_debug_prints_done = 0


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
    """
    Foloseste Alchemy (alchemy_getAssetTransfers) ca sa gaseasca ultima
    tranzactie TRIMISA de acest wallet, indiferent de tip (ETH, ERC20, NFT).
    Returneaza timestamp unix, sau None daca nu gaseste nimic.
    """
    global _debug_prints_done

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "alchemy_getAssetTransfers",
        "params": [{
            "fromBlock": "0x0",
            "toBlock": "latest",
            "fromAddress": wallet_address,
            "category": ["external", "erc20", "erc721", "erc1155"],
            "order": "desc",
            "maxCount": "0x1",
            "withMetadata": True,
        }],
    }

    resp = requests.post(ALCHEMY_RPC_URL, json=payload, timeout=15)
    data = resp.json()

    if "error" in data:
        global _debug_prints_done
        if _debug_prints_done < 3:
            print(f"[DEBUG] Răspuns brut Alchemy (eroare) pentru {wallet_address}: {json.dumps(data)}")
            _debug_prints_done += 1
        return None

    transfers = data.get("result", {}).get("transfers", [])
    if not transfers:
        return None

    timestamp_iso = transfers[0].get("metadata", {}).get("blockTimestamp")
    if not timestamp_iso:
        return None

    struct_time = time.strptime(timestamp_iso, "%Y-%m-%dT%H:%M:%S.%fZ")
    return int(time.mktime(struct_time) - time.timezone)


def trimite_telegram(mesaj) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] TELEGRAM_BOT_TOKEN/CHAT_ID nesetate - nu trimit nimic.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()
        if result.get("ok") is True:
            return True
        print(f"[!] Telegram a refuzat mesajul: {result}")
        return False
    except Exception as e:
        print(f"[!] Eroare de rețea la trimiterea către Telegram: {e}")
        return False


def main():
    if not ALCHEMY_RPC_URL:
        print("[!] ALCHEMY_RPC_URL nesetat! Pune URL-ul tau complet Alchemy ca secret pe GitHub.")
        return
    else:
        print(f"[i] ALCHEMY_RPC_URL setat, lungime: {len(ALCHEMY_RPC_URL)} caractere.")

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
        except Exception:
            pass

    now = time.time()
    current_state = {}
    newly_active = []
    errors = 0
    none_count = 0
    hours_seen = []

    for i, addr in enumerate(wallets, start=1):
        try:
            last_ts = get_last_tx_timestamp(addr)
        except Exception as e:
            print(f"[!] Eroare la {addr}: {e}")
            errors += 1
            time.sleep(RATE_LIMIT_SLEEP_SEC)
            continue

        time.sleep(RATE_LIMIT_SLEEP_SEC)

        if last_ts is None:
            current_state[addr] = {"active": False}
            none_count += 1
            continue

        hours_since = (now - last_ts) / 3600
        hours_seen.append((hours_since, addr))
        is_active = hours_since <= ACTIVE_THRESHOLD_HOURS
        current_state[addr] = {"active": is_active, "hours_since": round(hours_since, 1)}

        was_active_before = previous_state.get(addr, {}).get("active", False)
        if is_active and not was_active_before:
            newly_active.append(addr)

        if i % 20 == 0:
            print(f"[i] Verificate {i}/{len(wallets)}...")

    with open(STATE_FILE, "w") as f:
        json.dump(current_state, f, indent=2)

    total_active = sum(1 for s in current_state.values() if s.get("active"))
    print(f"[i] Rezumat: {len(wallets)} verificate, {errors} erori, "
          f"{none_count} fără NICIO tranzacție găsită, "
          f"{total_active} active TOTAL (în ultimele {ACTIVE_THRESHOLD_HOURS}h), "
          f"{len(newly_active)} NOI active față de rularea anterioară.")

    if hours_seen:
        hours_seen.sort(key=lambda x: x[0])
        print("[i] Cele mai RECENT active 5 wallet-uri (indiferent de prag):")
        for h, a in hours_seen[:5]:
            print(f"    {a} -> ultima tranzacție acum {h:.1f} ore ({h/24:.1f} zile)")

    if not newly_active:
        print("[i] Niciun wallet nou activ - nu trimit nimic pe Telegram (comportament normal, nu eroare).")
        return

    mesaj = f"🚨 *{len(newly_active)} SNIPERI DIN DUNE AU DEVENIT ACTIVI!* 🚨\n\n"
    for addr in newly_active[:15]:
        mesaj += f"👤 `{addr}`\n🔗 [BaseScan](https://basescan.org/address/{addr})\n\n"
    if len(newly_active) > 15:
        mesaj += f"🔍 ...și încă {len(newly_active) - 15} portofele active."

    sent_ok = trimite_telegram(mesaj)
    if sent_ok:
        print("[+] Alertă confirmată trimisă pe Telegram!")
    else:
        print("[!] Alerta NU a fost confirmată ca trimisă - vezi eroarea de mai sus.")


if __name__ == "__main__":
    main()
