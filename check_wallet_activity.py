name: Wallet Hunter (GitHub - fereastră limitată)

on:
  schedule:
    - cron: "*/5 * * * *"  # la fiecare 5 minute - minimul practic recomandat de GitHub
  workflow_dispatch: {}

jobs:
  hunt:
    runs-on: ubuntu-latest
    timeout-minutes: 5  # se închide forțat dacă ar dura mai mult, ca să nu se suprapună cu următoarea rulare
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install web3 aiohttp

      - name: Run wallet hunter (fereastră de 4.5 min)
        env:
          BASE_WSS_RPC: ${{ secrets.BASE_WSS_RPC }}
          BASE_HTTPS_RPC: ${{ secrets.BASE_HTTPS_RPC }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          MIN_BUY_USD: "500"
          RUN_DURATION_SEC: "270"
        run: python wallet_hunter_github.py
