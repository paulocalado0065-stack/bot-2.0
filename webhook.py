import os
import sqlite3
import hashlib
import hmac
import logging

from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
import requests

load_dotenv()
DB_PATH = os.getenv("DATABASE_PATH", "loja.db")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Loja Neon - Mercado Pago Webhook")


def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def signature_valid(request: Request, payment_id: str) -> bool:
    if not MP_WEBHOOK_SECRET:
        return False
    parts = {}
    for part in request.headers.get("x-signature", "").split(","):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            parts[k] = v
    ts, v1 = parts.get("ts"), parts.get("v1")
    if not ts or not v1:
        return False
    manifest = f"id:{payment_id};request-id:{request.headers.get('x-request-id', '')};ts:{ts};"
    expected = hmac.new(MP_WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def send_discord_dm(user_id: int, order_id: int, product_name: str, delivery_type: str, payload: str):
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN não configurado no ambiente do webhook")
    if not payload:
        raise RuntimeError("Entrega não configurada para este produto; configure e reenvie a notificação")
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    channel_response = requests.post(
        "https://discord.com/api/v10/users/@me/channels",
        headers=headers, json={"recipient_id": str(user_id)}, timeout=20
    )
    if channel_response.status_code >= 300:
        raise RuntimeError(f"Discord não abriu DM (HTTP {channel_response.status_code}): {channel_response.text[:200]}")
    channel_id = channel_response.json()["id"]
    if delivery_type == "key":
        intro = f"💜 **Loja Neon — entrega do pedido #{order_id}**\nProduto: **{product_name}**\nSua key/código único:"
    elif delivery_type == "link":
        intro = f"💜 **Loja Neon — pedido #{order_id} confirmado!**\nProduto: **{product_name}**\nSeu link de acesso/download:"
    else:
        intro = f"💜 **Loja Neon — pedido #{order_id} confirmado!**\nProduto: **{product_name}**\nInstruções e dados da compra:"
    message = f"{intro}\n{payload}"
    if len(message) > 1950:
        message = message[:1947] + "..."
    response = requests.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers=headers, json={"content": message, "allowed_mentions": {"parse": []}}, timeout=20
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Discord não enviou DM (HTTP {response.status_code}): {response.text[:200]}")


def deliver_paid_order(order_id: int):
    with db() as con:
        order = con.execute(
            "SELECT o.*,p.name AS product_name,p.delivery_type,p.delivery_content "
            "FROM orders o JOIN products p ON p.id=o.product_id WHERE o.id=?", (order_id,)
        ).fetchone()
        if not order or order["status"] != "paid" or order["delivery_sent"]:
            return
        payload = (order["delivered_content"] or "").strip()
        delivery_type = order["delivery_type"]
        if not payload and delivery_type in ("link", "text"):
            payload = (order["delivery_content"] or "").strip()
            if payload:
                con.execute("UPDATE orders SET delivered_content=? WHERE id=?", (payload, order_id))
        if not payload:
            raise RuntimeError("Pedido pago, mas não há conteúdo de entrega reservado/configurado")
        user_id = order["user_id"]
        product_name = order["product_name"]
    send_discord_dm(user_id, order_id, product_name, delivery_type, payload)
    with db() as con:
        con.execute("UPDATE orders SET delivery_sent=1 WHERE id=? AND status='paid'", (order_id,))
    logging.info("Entrega enviada para o pedido %s", order_id)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook/mercadopago")
async def mercadopago_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    payment_id = str((body.get("data") or {}).get("id") or request.query_params.get("data.id") or "")
    event_type = body.get("type") or request.query_params.get("type")
    if event_type != "payment" or not payment_id:
        return {"received": True, "ignored": True}
    if not signature_valid(request, payment_id):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN not configured")
    response = requests.get(
        f"https://api.mercadopago.com/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}, timeout=20
    )
    if response.status_code >= 300:
        raise HTTPException(status_code=502, detail="Payment verification failed")
    payment = response.json()
    order_ref = str(payment.get("external_reference") or "")
    if not order_ref.isdigit():
        return {"received": True, "ignored": True}
    status = payment.get("status")
    order_id = int(order_ref)
    should_deliver = False

    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return {"received": True, "ignored": True}
        expected = round(order["total_cents"] / 100, 2)
        actual = float(payment.get("transaction_amount") or 0)
        if payment.get("currency_id") != "BRL" or abs(actual - expected) > 0.009:
            raise HTTPException(status_code=400, detail="Amount or currency mismatch")

        if status == "approved":
            if order["status"] not in ("paid", "paid_delivery_review"):
                product = con.execute(
                    "SELECT id,name,stock,delivery_type,delivery_content FROM products WHERE id=?",
                    (order["product_id"],)
                ).fetchone()
                if not product or product["stock"] < order["quantity"]:
                    con.execute("UPDATE orders SET status='paid_stock_review',mp_payment_id=? WHERE id=?", (payment_id, order_id))
                elif product["delivery_type"] == "key":
                    key_row = con.execute(
                        "SELECT id,content FROM delivery_items WHERE product_id=? AND assigned_order_id IS NULL ORDER BY id LIMIT 1",
                        (order["product_id"],)
                    ).fetchone()
                    if not key_row:
                        con.execute("UPDATE orders SET status='paid_stock_review',mp_payment_id=? WHERE id=?", (payment_id, order_id))
                    else:
                        con.execute("UPDATE delivery_items SET assigned_order_id=? WHERE id=? AND assigned_order_id IS NULL", (order_id, key_row["id"]))
                        con.execute("UPDATE products SET stock=stock-? WHERE id=?", (order["quantity"], order["product_id"]))
                        con.execute("UPDATE orders SET status='paid',mp_payment_id=?,delivered_content=? WHERE id=?", (payment_id, key_row["content"], order_id))
                        should_deliver = True
                else:
                    content = (product["delivery_content"] or "").strip()
                    con.execute("UPDATE products SET stock=stock-? WHERE id=?", (order["quantity"], order["product_id"]))
                    new_status = "paid" if content else "paid_delivery_review"
                    con.execute("UPDATE orders SET status=?,mp_payment_id=?,delivered_content=? WHERE id=?",
                                (new_status, payment_id, content, order_id))
                    should_deliver = bool(content)
            elif order["status"] == "paid_delivery_review":
                product = con.execute("SELECT delivery_type,delivery_content FROM products WHERE id=?", (order["product_id"],)).fetchone()
                if product and product["delivery_type"] in ("link", "text") and (product["delivery_content"] or "").strip():
                    con.execute("UPDATE orders SET status='paid',delivered_content=? WHERE id=?",
                                (product["delivery_content"].strip(), order_id))
                    should_deliver = True
            elif order["status"] == "paid":
                should_deliver = not bool(order["delivery_sent"])
            con.execute("UPDATE orders SET mp_payment_id=? WHERE id=?", (payment_id, order_id))
        elif status in ("rejected", "cancelled", "refunded", "charged_back"):
            con.execute("UPDATE orders SET status=?,mp_payment_id=? WHERE id=?", (status, payment_id, order_id))
        else:
            if order["status"] not in ("paid", "paid_delivery_review"):
                con.execute("UPDATE orders SET status=?,mp_payment_id=? WHERE id=?", (status or "pending", payment_id, order_id))

    if status == "approved":
        with db() as con:
            latest = con.execute("SELECT status,delivery_sent FROM orders WHERE id=?", (order_id,)).fetchone()
        if latest and latest["status"] == "paid" and not latest["delivery_sent"]:
            try:
                deliver_paid_order(order_id)
            except Exception as exc:
                logging.exception("Entrega pendente para pedido %s", order_id)
                raise HTTPException(status_code=502, detail="Payment approved but automatic delivery is pending; webhook retry required") from exc
        elif latest and latest["status"] == "paid_stock_review":
            logging.error("Pedido %s pago, mas sem estoque/key disponível; precisa de revisão administrativa", order_id)
    return {"received": True, "delivery_attempted": should_deliver}
