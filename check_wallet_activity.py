"""
Wallet Hunter v2 - detecție prin SIMULARE, nu prin decodare de calldata
==========================================================================

DE CE versiunea asta e mai bună decât v1:
  v1 decoda calldata căutând selectori cunoscuți (swapExactETHForTokens etc.)
  Problema: dacă wallet-ul țintă trece prin un agregator/Settler (ex: 0x
  Protocol "Settler", 1inch, Odos, Uniswap Universal Router), calldata e
  complet diferit și NU se potrivește cu niciun selector cunoscut. Practic
  v1 e oarbă exact la genul de wallet sofisticat pe care vrei să-l urmărești.

  v2 nu mai ghicește din calldata. Trimite tranzacția pending la
  alchemy_simulateAssetChanges, care rulează tranzacția într-un mediu
  simulat și returnează EXACT ce active se transferă - indiferent de
  router, agregator, settler, sau orice abstractizare e pe dedesubt.
  E aceeași tehnică folosită de wallet-uri/dapps ca să-ți arate "preview"
  înainte să semnezi o tranzacție.

Costuri reale de care să fii conștient:
  - Free tier Alchemy: 1000 simulări/zi. Dacă urmărești wallet-uri foarte
    active, poți atinge plafonul - ai nevoie de tier plătit pentru volum mare.
  - Simularea are latență proprie (sute de ms) - mai lentă decât un simplu
    pattern-match pe calldata, dar mult mai precisă.

NU cumpără automat nimic. E radar, nu trigger.

Instalare:
    pip install web3 aiohttp python-dotenv
"""

import asyncio
import json
import logging
import os
import time

from web3 import Web3, AsyncWeb3, WebSocketProvider
import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("wallet_hunter_v2.log"), logging.StreamHandler()],
)
log = logging.getLogger("hunter_v2")

alert_log = logging.getLogger("wallet_alerts_v2")
alert_log.setLevel(logging.INFO)
alert_handler = logging.FileHandler("wallet_buys_detected_v2.log")
alert_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
alert_log.addHandler(alert_handler)
alert_log.addHandler(logging.StreamHandler())


# --------------------------------------------------------------------------
# CONFIGURARE
# --------------------------------------------------------------------------

BASE_WSS_RPC = os.getenv("BASE_WSS_RPC", "wss://base-mainnet.g.alchemy.com/v2/CHEIA_TA")
BASE_HTTPS_RPC = os.getenv("BASE_HTTPS_RPC", "https://base-mainnet.g.alchemy.com/v2/CHEIA_TA")

WETH_ADDRESS = "0x4200000000000000000000000000000000000006".lower()

# --- Surse pentru lista de wallet-uri urmărite ---
SMART_MONEY_FILE = "smart_money_wallets.json"

MANUAL_FALLBACK_WALLETS = [
    "0x0000000000000000000000000000000000dEaD",  # <-- înlocuiește cu wallet-ul real, ca fallback
]

# --- Auto-refresh din lista de "activi" generată de check_wallet_activity.py pe GitHub ---
# Opțional - dacă nu setezi GITHUB_TOKEN/GITHUB_REPO, scriptul folosește doar
# fișierul local + lista manuală, fără să crape.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # ex: "username/wallet-activity-checker"
GITHUB_STATE_PATH = os.getenv("GITHUB_STATE_PATH", "activity_state.json")
REFRESH_INTERVAL_SEC = int(os.getenv("REFRESH_INTERVAL_SEC", "900"))  # 15 minute implicit

# --- Filtru de zgomot: ignorăm cumpărări sub acest prag în USD ---
MIN_BUY_USD = float(os.getenv("MIN_BUY_USD", "500"))


async def fetch_all_wallets_from_github(session: aiohttp.ClientSession) -> list:
    """
    Citește activity_state.json din repo-ul GitHub și extrage TOATE adresele
    (nu doar cele active acum) - ca să nu ratăm exact momentul în care un
    wallet "adormit" se trezește și face prima mișcare. Pragul MIN_BUY_USD
    e suficient ca filtru de zgomot; nu mai e nevoie să restrângem și lista
    de wallet-uri urmărite.
    Dacă GITHUB_TOKEN/GITHUB_REPO nu sunt setate, returnează listă goală
    (fără eroare) - feature-ul e opțional.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return []

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_STATE_PATH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.raw+json",
    }
    try:
        async with session.get(url, headers=headers, timeout=15) as resp:
            if resp.status != 200:
                log.warning(f"Nu am putut citi {GITHUB_STATE_PATH} din GitHub (status {resp.status}).")
                return []
            data = await resp.json()
            return [addr.lower() for addr in data.keys()]
    except Exception as e:
        log.warning(f"Eroare la citirea listei de wallet-uri din GitHub: {e}")
        return []


def load_target_wallets_static() -> set:
    """Wallet-urile cunoscute la pornire: fișierul smart_money + lista manuală."""
    wallets = set(w.lower() for w in MANUAL_FALLBACK_WALLETS)
    if os.path.exists(SMART_MONEY_FILE):
        with open(SMART_MONEY_FILE) as f:
            data = json.load(f)
        file_wallets = data.get("wallets", [])
        wallets.update(w.lower() for w in file_wallets)
        log.info(f"Încărcat {len(file_wallets)} wallet-uri din {SMART_MONEY_FILE}.")
    return wallets


SIMULATION_TIMEOUT_SEC = 8


# --------------------------------------------------------------------------
# Preț ETH/USD - ca să transformăm "ETH cheltuit" în "dolari cheltuiți"
# --------------------------------------------------------------------------

class EthPriceFeed:
    def __init__(self):
        self.price_usd = 0.0
        self.last_fetch = 0.0

    async def get(self, session: aiohttp.ClientSession) -> float:
        if time.time() - self.last_fetch > 120:
            try:
                async with session.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "ethereum", "vs_currencies": "usd"},
                    timeout=5,
                ) as resp:
                    data = await resp.json()
                    self.price_usd = data["ethereum"]["usd"]
                    self.last_fetch = time.time()
            except Exception as e:
                log.warning(f"Nu am putut actualiza prețul ETH/USD ({e}). Folosesc valoarea veche: {self.price_usd}")
        return self.price_usd


eth_feed = EthPriceFeed()


# --------------------------------------------------------------------------
# Simulare via alchemy_simulateAssetChanges
# --------------------------------------------------------------------------

async def simulate_asset_changes(session: aiohttp.ClientSession, tx: dict) -> dict | None:
    """
    Trimite tranzacția (așa cum a venit din pending) la Alchemy pentru
    simulare și returnează lista de schimbări de active.
    Funcționează indiferent de routerul/agregatorul/settler-ul folosit.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "alchemy_simulateAssetChanges",
        "params": [{
            "from": tx.get("from"),
            "to": tx.get("to"),
            "value": tx.get("value", "0x0"),
            "data": tx.get("input", tx.get("data", "0x")),
        }],
    }

    try:
        async with session.post(
            BASE_HTTPS_RPC, json=payload, timeout=SIMULATION_TIMEOUT_SEC
        ) as resp:
            result = await resp.json()
            if "error" in result:
                log.debug(f"Simulare cu eroare: {result['error']}")
                return None
            return result.get("result")
    except Exception as e:
        log.warning(f"Simulare eșuată: {e}")
        return None


def extract_buy_from_changes(sim_result: dict, wallet: str):
    """
    Parcurge schimbările de active returnate de simulare și identifică
    dacă wallet-ul a PRIMIT un token ERC20 nou (= a cumpărat ceva),
    indiferent prin ce contract a trecut tranzacția.
    Returnează (token_info, eth_cheltuit) sau None.
    """
    if not sim_result or sim_result.get("error"):
        return None

    changes = sim_result.get("changes", [])
    received_token = None
    spent_native_eth = 0.0

    for change in changes:
        to_addr = (change.get("to") or "").lower()
        from_addr = (change.get("from") or "").lower()
        asset_type = change.get("assetType")
        change_type = change.get("changeType")

        if change_type != "TRANSFER":
            continue

        if from_addr == wallet and asset_type == "NATIVE":
            spent_native_eth += float(change.get("amount", 0) or 0)

        if to_addr == wallet and asset_type == "ERC20":
            contract = (change.get("contractAddress") or "").lower()
            if contract == WETH_ADDRESS:
                continue
            received_token = {
                "address": change.get("contractAddress"),
                "symbol": change.get("symbol", "???"),
                "name": change.get("name", ""),
                "amount": change.get("amount"),
            }

    if received_token:
        return received_token, spent_native_eth
    return None


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


async def send_telegram_alert(session: aiohttp.ClientSession, mesaj: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_BOT_TOKEN/CHAT_ID nesetate - alerta rămâne doar în log local.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload, timeout=10) as resp:
            result = await resp.json()
            if result.get("ok") is True:
                return True
            log.warning(f"Telegram a refuzat mesajul: {result}")
            return False
    except Exception as e:
        log.warning(f"Eroare de rețea la trimiterea către Telegram: {e}")
        return False


# --------------------------------------------------------------------------
# Procesare tranzacție pending
# --------------------------------------------------------------------------

async def handle_pending_tx(session: aiohttp.ClientSession, tx: dict, target_wallets: set):
    from_addr = (tx.get("from") or "").lower()
    if from_addr not in target_wallets:
        return

    log.debug(f"Tranzacție pending de la wallet țintă: {tx.get('hash')}")

    sim_result = await simulate_asset_changes(session, tx)
    if sim_result is None:
        return

    buy_info = extract_buy_from_changes(sim_result, from_addr)
    if buy_info is None:
        log.debug("Simularea nu indică o achiziție de token nou (poate fi approve/altă acțiune).")
        return

    token, eth_spent = buy_info
    eth_price = await eth_feed.get(session)
    usd_spent = eth_spent * eth_price

    # FILTRU DE ZGOMOT: ignorăm cumpărările sub pragul setat
    if usd_spent < MIN_BUY_USD:
        log.debug(
            f"Cumpărare sub prag, ignorată: {from_addr} a cheltuit doar ${usd_spent:,.2f} "
            f"(prag minim: ${MIN_BUY_USD:,.0f})"
        )
        return

    tx_hash = tx.get("hash", "")
    dexscreener_url = f"https://dexscreener.com/base/{token['address']}"

    mesaj = (
        f"🎯 *WALLET ȚINTĂ A CUMPĂRAT*\n\n"
        f"👤 `{from_addr}`\n"
        f"🪙 Token: *{token['symbol']}* ({token['name']})\n"
        f"💰 Sumă: *${usd_spent:,.2f}* ({eth_spent:.5f} ETH)\n"
        f"📦 Cantitate: {token['amount']}\n"
        f"🔗 [Dexscreener]({dexscreener_url})\n"
        f"🧾 [Tx]({f'https://basescan.org/tx/{tx_hash}'})"
    )

    alert_log.info(
        f"WALLET ȚINTĂ A CUMPĂRAT | {from_addr} | "
        f"token: {token['symbol']} ({token['name']}) | "
        f"sumă: ${usd_spent:,.2f} ({eth_spent:.5f} ETH) | "
        f"cantitate token: {token['amount']} | "
        f"tx: {tx_hash} | {dexscreener_url}"
    )

    sent_ok = await send_telegram_alert(session, mesaj)
    if sent_ok:
        log.info("Alertă confirmată trimisă pe Telegram.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def run_session(session: aiohttp.ClientSession, target_wallets: set):
    """
    O singură sesiune de ascultare: deschide WSS, se abonează la wallet-urile
    curente, ascultă până la REFRESH_INTERVAL_SEC, apoi se închide - ca să
    poată fi reluată cu lista actualizată. Întreruperea e de ordinul
    milisecundelor, nu pierzi tranzacții reale în acel interval.
    """
    async with AsyncWeb3(WebSocketProvider(BASE_WSS_RPC)) as w3:
        log.info(f"Conectat la Base (WSS). Block curent: {await w3.eth.block_number}")

        subscription_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_subscribe",
            "params": ["alchemy_pendingTransactions", {"fromAddress": list(target_wallets)}],
        }

        raw_ws = w3.provider
        await raw_ws.send(json.dumps(subscription_request))
        log.info(f"Subscripție trimisă pentru {len(target_wallets)} wallet-uri. Ascultăm {REFRESH_INTERVAL_SEC}s...")

        deadline = time.time() + REFRESH_INTERVAL_SEC
        while time.time() < deadline:
            remaining = max(1, deadline - time.time())
            try:
                raw_msg = await asyncio.wait_for(raw_ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break  # gata, e timpul să reîmprospătăm lista

            try:
                msg = json.loads(raw_msg)
                if "params" in msg and "result" in msg["params"]:
                    tx = msg["params"]["result"]
                    asyncio.create_task(handle_pending_tx(session, tx, target_wallets))
            except Exception as e:
                log.error(f"Eroare la procesarea mesajului WS: {e}")


async def main():
    target_wallets = load_target_wallets_static()
    log.info(f"=== Wallet Hunter v2 - pornim cu {len(target_wallets)} wallet(uri) cunoscute ===")
    log.info(f"Prag minim de alertă: ${MIN_BUY_USD:,.0f} | Reîmprospătare listă la fiecare {REFRESH_INTERVAL_SEC}s")

    async with aiohttp.ClientSession() as session:
        while True:
            github_wallets = await fetch_all_wallets_from_github(session)
            if github_wallets:
                before = len(target_wallets)
                target_wallets.update(github_wallets)
                added = len(target_wallets) - before
                if added:
                    log.info(f"+{added} wallet(uri) noi adăugate din GitHub (niciunul eliminat).")

            try:
                await run_session(session, target_wallets)
            except Exception as e:
                log.error(f"Sesiune întreruptă: {e}. Reconectăm în 5s.")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
