"""
KLM AUTOSHOP — bot Telegram + NOWPayments
==========================================

Ce que fait ce script :
- Reçoit les données envoyées par la mini app (commande ou demande de recharge)
  via le message spécial "web_app_data" de Telegram.
- Pour une recharge : crée une facture crypto chez NOWPayments et envoie
  l'adresse + le montant exact au client dans le chat.
- Expose un petit serveur web qui reçoit les notifications IPN de NOWPayments
  (webhook appelé automatiquement quand le paiement est confirmé on-chain).
- Quand le paiement est confirmé : crédite le solde du client dans une base
  SQLite locale, et lui renvoie un message avec un bouton qui rouvre la mini
  app directement sur l'animation "Paiement réussi".
- Pour une commande : vérifie le solde en base, le débite, et t'envoie (toi,
  l'admin) le détail de la commande à préparer et expédier.

Ce que tu dois faire avant de lancer ce script :
1. pip install python-telegram-bot==21.* flask requests python-dotenv
2. Crée un fichier .env à côté de ce script avec :
     BOT_TOKEN=1234567890:ton_token_botfather
     WEBAPP_URL=https://tonsite.exemple/autoshop-noel-sanglant.html
     NOWPAYMENTS_API_KEY=ta_cle_api_nowpayments
     NOWPAYMENTS_IPN_SECRET=ton_secret_ipn_nowpayments
     ADMIN_CHAT_ID=ton_id_telegram_numerique
     PUBLIC_URL=https://ton-domaine-ou-tunnel.exemple
3. Sur https://nowpayments.io : crée un compte, récupère la clé API et le
   secret IPN dans les paramètres, et configure l'URL de callback IPN sur :
     https://ton-domaine-ou-tunnel.exemple/ipn/nowpayments
4. Lance ce script (il démarre le bot ET le petit serveur web ensemble).

Ceci est une base fonctionnelle et commentée, pas un système blindé prêt pour
de gros volumes. Avant une vraie mise en prod : ajoute des logs persistants,
surveille les erreurs réseau vers NOWPayments, et fais relire le code par
quelqu'un si tu gères des montants importants.
"""

import os
import json
import sqlite3
import hmac
import hashlib
import logging
import threading
from decimal import Decimal, ROUND_DOWN

import requests
from dotenv import load_dotenv
from flask import Flask, request as flask_request, abort
from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"]
NOWPAYMENTS_API_KEY = os.environ["NOWPAYMENTS_API_KEY"]
NOWPAYMENTS_IPN_SECRET = os.environ["NOWPAYMENTS_IPN_SECRET"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")

NOWPAYMENTS_API = "https://api.nowpayments.io/v1"
DB_PATH = os.path.join(os.path.dirname(__file__), "klm_autoshop.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("klm-autoshop")

# Correspondance entre les identifiants de crypto utilisés par la mini app
# et les codes attendus par NOWPayments.
COIN_CODES = {"BTC": "btc", "ETH": "eth", "LTC": "ltc", "SOL": "sol"}


# ---------------------------------------------------------------------------
# Base de données (soldes clients + commandes + factures en attente)
# ---------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS balances (
        user_id INTEGER PRIMARY KEY, balance REAL NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS invoices (
        payment_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
        amount_eur REAL NOT NULL, status TEXT NOT NULL DEFAULT 'waiting'
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
        items TEXT NOT NULL, total REAL NOT NULL
    )""")
    return conn


def get_balance(user_id: int) -> Decimal:
    row = db().execute("SELECT balance FROM balances WHERE user_id=?", (user_id,)).fetchone()
    return Decimal(str(row[0])) if row else Decimal("0")


def add_balance(user_id: int, amount: Decimal):
    conn = db()
    conn.execute(
        "INSERT INTO balances(user_id, balance) VALUES(?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance",
        (user_id, float(amount)),
    )
    conn.commit()


def set_balance(user_id: int, amount: Decimal):
    conn = db()
    conn.execute(
        "INSERT INTO balances(user_id, balance) VALUES(?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance",
        (user_id, float(amount)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# NOWPayments : création de facture crypto
# ---------------------------------------------------------------------------

def nowpayments_create_invoice(amount_eur: float, coin: str, order_id: str) -> dict:
    """Demande à NOWPayments une adresse de paiement pour ce montant et cette crypto."""
    resp = requests.post(
        f"{NOWPAYMENTS_API}/payment",
        headers={"x-api-key": NOWPAYMENTS_API_KEY},
        json={
            "price_amount": amount_eur,
            "price_currency": "eur",
            "pay_currency": COIN_CODES[coin],
            "order_id": order_id,
            "ipn_callback_url": f"{PUBLIC_URL}/ipn/nowpayments",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def verify_ipn_signature(raw_body: bytes, signature: str) -> bool:
    """NOWPayments signe chaque notification IPN avec ton secret : on vérifie
    cette signature pour être sûr que la notification vient bien d'eux."""
    sorted_payload = json.dumps(json.loads(raw_body), sort_keys=True, separators=(",", ":"))
    expected = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode(), sorted_payload.encode(), hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


# ---------------------------------------------------------------------------
# Bot Telegram
# ---------------------------------------------------------------------------

telegram_app: Application = Application.builder().token(BOT_TOKEN).build()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🩸 Ouvrir KLM AUTOSHOP", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await update.message.reply_text(
        "Bienvenue chez KLM AUTOSHOP. Clique pour ouvrir la boutique.",
        reply_markup=kb,
    )


async def cmd_solde(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = get_balance(update.effective_user.id)
    await update.message.reply_text(f"Ton solde actuel : {bal:.2f} €")


async def on_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reçoit tout ce que la mini app envoie avec tg.sendData(...)."""
    user = update.effective_user
    try:
        payload = json.loads(update.effective_message.web_app_data.data)
    except Exception:
        await update.message.reply_text("Données reçues illisibles, réessaie depuis la boutique.")
        return

    kind = payload.get("type")

    if kind == "recharge_request":
        await handle_recharge_request(update, context, user.id, payload)
    else:
        # Pas de "type" = commande produit (format envoyé par la mini app actuelle)
        await handle_order(update, context, user.id, payload)


async def handle_recharge_request(update, context, user_id: int, payload: dict):
    coin = payload.get("coin")
    amount = float(payload.get("amount", 0))
    if coin not in COIN_CODES or amount <= 0:
        await update.message.reply_text("Demande de recharge invalide.")
        return

    try:
        invoice = nowpayments_create_invoice(amount, coin, order_id=f"user{user_id}-{int(amount*100)}")
    except requests.RequestException as e:
        log.exception("Erreur NOWPayments")
        await update.message.reply_text("La création de la facture a échoué, réessaie dans un instant.")
        return

    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO invoices(payment_id, user_id, amount_eur, status) VALUES (?,?,?,?)",
        (str(invoice["payment_id"]), user_id, amount, "waiting"),
    )
    conn.commit()

    await update.message.reply_text(
        "Facture créée ✅\n\n"
        f"Montant : {invoice['pay_amount']} {invoice['pay_currency'].upper()}\n"
        f"Adresse : {invoice['pay_address']}\n\n"
        "Envoie exactement ce montant à cette adresse. Tu recevras un message "
        "dès que le paiement est confirmé, et ton solde sera crédité automatiquement."
    )


async def handle_order(update, context, user_id: int, payload: dict):
    total = float(payload.get("balanceUsed") or payload.get("total") or 0)
    items = payload.get("items", [])
    bal = get_balance(user_id)

    if bal < Decimal(str(total)):
        await update.message.reply_text(
            "Ton solde ne couvre plus cette commande, recharge-toi puis repasse commande depuis la boutique."
        )
        return

    set_balance(user_id, bal - Decimal(str(total)))
    conn = db()
    conn.execute("INSERT INTO orders(user_id, items, total) VALUES (?,?,?)", (user_id, json.dumps(items), total))
    conn.commit()

    detail = "\n".join(f"- {it['qty']}x {it['name']} ({it['price']} €)" for it in items)
    await update.message.reply_text(f"Commande enregistrée, merci ! Total débité : {total:.2f} €.")
    await context.bot.send_message(
        ADMIN_CHAT_ID,
        f"🩸 Nouvelle commande — client {user_id}\n{detail}\nTotal : {total:.2f} €\n"
        "Pense à lui demander son adresse de livraison si ce n'est pas déjà fait.",
    )


telegram_app.add_handler(CommandHandler("start", cmd_start))
telegram_app.add_handler(CommandHandler("solde", cmd_solde))
telegram_app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_webapp_data))


# ---------------------------------------------------------------------------
# Serveur web : reçoit les notifications IPN de NOWPayments
# ---------------------------------------------------------------------------

flask_app = Flask(__name__)


@flask_app.post("/ipn/nowpayments")
def nowpayments_ipn():
    raw = flask_request.get_data()
    signature = flask_request.headers.get("x-nowpayments-sig", "")
    if not verify_ipn_signature(raw, signature):
        abort(401)

    data = json.loads(raw)
    status = data.get("payment_status")
    payment_id = str(data.get("payment_id"))

    if status in ("finished", "confirmed"):
        conn = db()
        row = conn.execute(
            "SELECT user_id, amount_eur, status FROM invoices WHERE payment_id=?", (payment_id,)
        ).fetchone()
        if row and row[2] != "paid":
            user_id, amount_eur, _ = row
            amount = Decimal(str(amount_eur)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            add_balance(user_id, amount)
            conn.execute("UPDATE invoices SET status='paid' WHERE payment_id=?", (payment_id,))
            conn.commit()

            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                "🎄 Retour à la boutique",
                web_app=WebAppInfo(url=f"{WEBAPP_URL}#paid={amount}"),
            )]])
            telegram_app.create_task(
                telegram_app.bot.send_message(
                    user_id,
                    f"Paiement reçu ✅ +{amount} € crédités sur ton solde. Bon achat !",
                    reply_markup=kb,
                )
            )
    return "ok", 200


def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("Bot et serveur IPN démarrés.")
    telegram_app.run_polling()
