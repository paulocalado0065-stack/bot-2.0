import os
import sqlite3
import base64
from io import BytesIO
from decimal import Decimal, InvalidOperation

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import requests

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "MTU0MzQ2MTA2NzYyNjI1NDQyOQ.GSpDvQ.xZAeS6JsU-otk5CjZLxcxen68n9JHYAKjZqIi8").strip()
DB_PATH = os.getenv("DATABASE_PATH", "loja.db")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS catalogs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
            name TEXT NOT NULL, description TEXT DEFAULT '', image_url TEXT DEFAULT '',
            created_by INTEGER NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT, catalog_id INTEGER NOT NULL,
            name TEXT NOT NULL, description TEXT DEFAULT '', price_cents INTEGER NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0, image_url TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(catalog_id) REFERENCES catalogs(id) ON DELETE CASCADE)""")
        con.execute("""CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL, quantity INTEGER NOT NULL, total_cents INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', mp_payment_id TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_mp_payment ON orders(mp_payment_id)")
        # Migrations for existing databases
        product_columns = {r[1] for r in con.execute("PRAGMA table_info(products)").fetchall()}
        if "delivery_type" not in product_columns:
            con.execute("ALTER TABLE products ADD COLUMN delivery_type TEXT NOT NULL DEFAULT 'text'")
        if "delivery_content" not in product_columns:
            con.execute("ALTER TABLE products ADD COLUMN delivery_content TEXT NOT NULL DEFAULT ''")
        order_columns = {r[1] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
        if "delivery_sent" not in order_columns:
            con.execute("ALTER TABLE orders ADD COLUMN delivery_sent INTEGER NOT NULL DEFAULT 0")
        if "delivered_content" not in order_columns:
            con.execute("ALTER TABLE orders ADD COLUMN delivered_content TEXT NOT NULL DEFAULT ''")
        con.execute("""CREATE TABLE IF NOT EXISTS delivery_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            assigned_order_id INTEGER UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE,
            FOREIGN KEY(assigned_order_id) REFERENCES orders(id))""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_delivery_items_available ON delivery_items(product_id, assigned_order_id)")


def is_admin(interaction):
    return bool(interaction.guild and interaction.user.guild_permissions.manage_guild)


async def require_admin(interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("Você precisa da permissão **Gerenciar servidor**.", ephemeral=True)
        return False
    return True


def money(cents):
    return ("R$ {:,.2f}".format(cents / 100).replace(",", "X").replace(".", ",").replace("X", "."))


def create_checkout(order_id, name, cents):
    if not MP_ACCESS_TOKEN:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado")
    if not PUBLIC_BASE_URL.startswith("https://"):
        raise RuntimeError("PUBLIC_BASE_URL precisa ser HTTPS público")
    payload = {
        "items": [{"id": str(order_id), "title": name[:250], "quantity": 1,
                   "currency_id": "BRL", "unit_price": round(cents / 100, 2)}],
        "external_reference": str(order_id),
        "notification_url": f"{PUBLIC_BASE_URL}/webhook/mercadopago",
        "metadata": {"discord_order_id": str(order_id)}
    }
    r = requests.post("https://api.mercadopago.com/checkout/preferences", json=payload,
                      headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}, timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"Mercado Pago HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    return data.get("init_point") or data.get("sandbox_init_point")


def create_pix(order_id, name, cents, email):
    """Cria um Pix dinâmico; o QR/copia-e-cola é exclusivo deste pedido."""
    if not MP_ACCESS_TOKEN:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado")
    if not PUBLIC_BASE_URL.startswith("https://"):
        raise RuntimeError("PUBLIC_BASE_URL precisa ser HTTPS público")
    import uuid
    payload = {
        "transaction_amount": round(cents / 100, 2),
        "description": name[:250],
        "payment_method_id": "pix",
        "payer": {"email": email.strip()},
        "external_reference": str(order_id),
        "notification_url": f"{PUBLIC_BASE_URL}/webhook/mercadopago",
        "metadata": {"discord_order_id": str(order_id)},
    }
    r = requests.post("https://api.mercadopago.com/v1/payments", json=payload,
                      headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                               "X-Idempotency-Key": str(uuid.uuid4())}, timeout=20)
    if r.status_code >= 300:
        raise RuntimeError(f"Mercado Pago HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    tx = (data.get("point_of_interaction") or {}).get("transaction_data") or {}
    return {"id": str(data.get("id", "")), "qr": tx.get("qr_code_base64"),
            "copy_paste": tx.get("qr_code"), "ticket_url": tx.get("ticket_url"),
            "status": data.get("status", "pending")}


def create_order(interaction, product):
    with db() as con:
        cur = con.execute("INSERT INTO orders(guild_id,user_id,product_id,quantity,total_cents,status) VALUES(?,?,?,?,?,'pending')",
                          (interaction.guild_id, interaction.user.id, product["id"], 1, product["price_cents"]))
        return cur.lastrowid


def get_available_product(product_id, guild_id):
    with db() as con:
        return con.execute("SELECT p.*,c.guild_id FROM products p JOIN catalogs c ON c.id=p.catalog_id WHERE p.id=? AND c.guild_id=? AND p.active=1",
                           (product_id, guild_id)).fetchone()


class CreateCatalogModal(discord.ui.Modal, title="Criar catálogo"):
    name = discord.ui.TextInput(label="Nome do catálogo", max_length=100)
    description = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, required=False, max_length=800)
    image_url = discord.ui.TextInput(label="URL da imagem (opcional)", required=False, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin(interaction): return
        with db() as con:
            cur = con.execute("INSERT INTO catalogs(guild_id,name,description,image_url,created_by) VALUES(?,?,?,?,?)",
                              (interaction.guild_id, str(self.name).strip(), str(self.description).strip(),
                               str(self.image_url).strip(), interaction.user.id))
            cid = cur.lastrowid
        await interaction.response.send_message(f"Catálogo **{self.name}** criado (ID {cid}).", ephemeral=True)


class EditCatalogModal(discord.ui.Modal, title="Editar catálogo"):
    catalog_id = discord.ui.TextInput(label="ID do catálogo", max_length=10)
    name = discord.ui.TextInput(label="Novo nome (deixe vazio para manter)", required=False, max_length=100)
    description = discord.ui.TextInput(label="Nova descrição (opcional)", style=discord.TextStyle.paragraph, required=False, max_length=800)
    image_url = discord.ui.TextInput(label="Nova URL de imagem (opcional)", required=False, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin(interaction): return
        try: cid = int(str(self.catalog_id))
        except ValueError:
            await interaction.response.send_message("ID inválido.", ephemeral=True); return
        with db() as con:
            row = con.execute("SELECT * FROM catalogs WHERE id=? AND guild_id=?", (cid, interaction.guild_id)).fetchone()
            if not row:
                await interaction.response.send_message("Catálogo não encontrado.", ephemeral=True); return
            con.execute("UPDATE catalogs SET name=?,description=?,image_url=? WHERE id=?",
                        (str(self.name).strip() or row["name"],
                         str(self.description).strip() or row["description"],
                         str(self.image_url).strip() or row["image_url"], cid))
        await interaction.response.send_message("Catálogo atualizado.", ephemeral=True)


class AddProductModal(discord.ui.Modal, title="Adicionar produto"):
    catalog_id = discord.ui.TextInput(label="ID do catálogo", max_length=10)
    name = discord.ui.TextInput(label="Nome do produto", max_length=100)
    price = discord.ui.TextInput(label="Preço em reais (ex.: 19,90)", max_length=20)
    stock = discord.ui.TextInput(label="Quantidade em estoque", max_length=10)
    description = discord.ui.TextInput(label="Descrição", style=discord.TextStyle.paragraph, required=False, max_length=800)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin(interaction): return
        try:
            cid = int(str(self.catalog_id))
            qty = int(str(self.stock))
            value = Decimal(str(self.price).replace(",", "."))
            if qty < 0 or not value.is_finite() or value < 0: raise ValueError
            cents = int((value * 100).quantize(Decimal("1")))
        except (ValueError, InvalidOperation):
            await interaction.response.send_message("ID, preço ou estoque inválido.", ephemeral=True); return
        with db() as con:
            cat = con.execute("SELECT id FROM catalogs WHERE id=? AND guild_id=?", (cid, interaction.guild_id)).fetchone()
            if not cat:
                await interaction.response.send_message("Catálogo não encontrado.", ephemeral=True); return
            cur = con.execute("INSERT INTO products(catalog_id,name,description,price_cents,stock) VALUES(?,?,?,?,?)",
                              (cid, str(self.name).strip(), str(self.description).strip(), cents, qty))
            pid = cur.lastrowid
        await interaction.response.send_message(f"Produto criado (ID {pid}). Para adicionar imagem, use o botão **Editar produto** e informe uma URL.", ephemeral=True)


class EditProductModal(discord.ui.Modal, title="Editar produto"):
    product_id = discord.ui.TextInput(label="ID do produto", max_length=10)
    name = discord.ui.TextInput(label="Novo nome (opcional)", required=False, max_length=100)
    price = discord.ui.TextInput(label="Novo preço em reais (opcional)", required=False, max_length=20)
    stock = discord.ui.TextInput(label="Novo estoque (opcional)", required=False, max_length=10)
    image_url = discord.ui.TextInput(label="URL da imagem (opcional)", required=False, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin(interaction): return
        try:
            pid = int(str(self.product_id))
            with db() as con:
                p = con.execute("SELECT p.* FROM products p JOIN catalogs c ON c.id=p.catalog_id WHERE p.id=? AND c.guild_id=?",
                                (pid, interaction.guild_id)).fetchone()
                if not p:
                    await interaction.response.send_message("Produto não encontrado.", ephemeral=True); return
                cents = p["price_cents"]
                if str(self.price).strip():
                    val = Decimal(str(self.price).replace(",", "."))
                    if not val.is_finite() or val < 0: raise ValueError
                    cents = int((val * 100).quantize(Decimal("1")))
                stock = p["stock"] if not str(self.stock).strip() else int(str(self.stock))
                if stock < 0: raise ValueError
                con.execute("UPDATE products SET name=?,price_cents=?,stock=?,image_url=? WHERE id=?",
                            (str(self.name).strip() or p["name"], cents, stock,
                             str(self.image_url).strip() or p["image_url"], pid))
        except (ValueError, InvalidOperation):
            await interaction.response.send_message("ID, preço ou estoque inválido.", ephemeral=True); return
        await interaction.response.send_message("Produto atualizado.", ephemeral=True)


class ConfigureDeliveryModal(discord.ui.Modal, title="Configurar entrega do produto"):
    product_id = discord.ui.TextInput(label="ID do produto", max_length=10)
    delivery_type = discord.ui.TextInput(label="Tipo: key, link ou text", placeholder="link", max_length=10)
    content = discord.ui.TextInput(label="Link ou texto para entregar (não use para keys)", style=discord.TextStyle.paragraph, required=False, max_length=1800)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin(interaction): return
        try: pid = int(str(self.product_id))
        except ValueError:
            await interaction.response.send_message("ID de produto inválido.", ephemeral=True); return
        kind = str(self.delivery_type).strip().lower()
        if kind not in ("key", "link", "text"):
            await interaction.response.send_message("Tipo inválido. Use exatamente: key, link ou text.", ephemeral=True); return
        content = str(self.content).strip()
        if kind in ("link", "text") and not content:
            await interaction.response.send_message("Para link ou text, informe o conteúdo de entrega.", ephemeral=True); return
        if kind == "link" and not (content.startswith("https://") or content.startswith("http://")):
            await interaction.response.send_message("O tipo link precisa começar com https:// ou http://.", ephemeral=True); return
        with db() as con:
            row = con.execute("SELECT p.id,p.name FROM products p JOIN catalogs c ON c.id=p.catalog_id WHERE p.id=? AND c.guild_id=?", (pid, interaction.guild_id)).fetchone()
            if not row:
                await interaction.response.send_message("Produto não encontrado.", ephemeral=True); return
            if kind == "key":
                available = con.execute("SELECT COUNT(*) FROM delivery_items WHERE product_id=? AND assigned_order_id IS NULL", (pid,)).fetchone()[0]
                con.execute("UPDATE products SET delivery_type='key',delivery_content='' ,stock=? WHERE id=?", (available, pid))
            else:
                con.execute("UPDATE products SET delivery_type=?,delivery_content=? WHERE id=?", (kind, content, pid))
        if kind == "key":
            msg = f"Entrega configurada para **keys únicas** em **{row['name']}**. Estoque sincronizado com {available} key(s) disponíveis. Use **Adicionar keys** para cadastrar códigos."
        else:
            msg = f"Entrega configurada para **{kind}** em **{row['name']}**. O conteúdo será enviado por mensagem privada após a confirmação do pagamento."
        await interaction.response.send_message(msg, ephemeral=True)


class AddKeysModal(discord.ui.Modal, title="Adicionar keys ao estoque"):
    product_id = discord.ui.TextInput(label="ID do produto (tipo key)", max_length=10)
    keys = discord.ui.TextInput(label="Uma key por linha", style=discord.TextStyle.paragraph, placeholder="Cole uma key por linha", max_length=3900)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin(interaction): return
        try: pid = int(str(self.product_id))
        except ValueError:
            await interaction.response.send_message("ID de produto inválido.", ephemeral=True); return
        values = list(dict.fromkeys(line.strip() for line in str(self.keys).splitlines() if line.strip()))
        if not values:
            await interaction.response.send_message("Cole pelo menos uma key, uma por linha.", ephemeral=True); return
        with db() as con:
            product = con.execute("SELECT p.id,p.name,p.delivery_type FROM products p JOIN catalogs c ON c.id=p.catalog_id WHERE p.id=? AND c.guild_id=?", (pid, interaction.guild_id)).fetchone()
            if not product:
                await interaction.response.send_message("Produto não encontrado.", ephemeral=True); return
            if product["delivery_type"] != "key":
                await interaction.response.send_message("Configure primeiro a entrega como tipo **key** no botão Configurar entrega.", ephemeral=True); return
            existing = {r[0] for r in con.execute("SELECT content FROM delivery_items WHERE product_id=?", (pid,)).fetchall()}
            fresh = [v for v in values if v not in existing]
            for value in fresh:
                con.execute("INSERT INTO delivery_items(product_id,content) VALUES(?,?)", (pid, value))
            available = con.execute("SELECT COUNT(*) FROM delivery_items WHERE product_id=? AND assigned_order_id IS NULL", (pid,)).fetchone()[0]
            con.execute("UPDATE products SET stock=? WHERE id=?", (available, pid))
        await interaction.response.send_message(f"Adicionadas **{len(fresh)}** key(s) novas para **{product['name']}**. Disponíveis: **{available}**.", ephemeral=True)


class DeleteProductModal(discord.ui.Modal, title="Excluir produto"):
    product_id = discord.ui.TextInput(label="ID do produto", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin(interaction): return
        try: pid = int(str(self.product_id))
        except ValueError:
            await interaction.response.send_message("ID inválido.", ephemeral=True); return
        with db() as con:
            cur = con.execute("DELETE FROM products WHERE id=? AND catalog_id IN (SELECT id FROM catalogs WHERE guild_id=?)",
                              (pid, interaction.guild_id))
        await interaction.response.send_message("Produto excluído." if cur.rowcount else "Produto não encontrado.", ephemeral=True)


class DeleteCatalogModal(discord.ui.Modal, title="Excluir catálogo"):
    catalog_id = discord.ui.TextInput(label="ID do catálogo", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin(interaction): return
        try: cid = int(str(self.catalog_id))
        except ValueError:
            await interaction.response.send_message("ID inválido.", ephemeral=True); return
        with db() as con:
            row = con.execute("SELECT id FROM catalogs WHERE id=? AND guild_id=?", (cid, interaction.guild_id)).fetchone()
            if row:
                con.execute("DELETE FROM products WHERE catalog_id=?", (cid,))
                con.execute("DELETE FROM catalogs WHERE id=?", (cid,))
        await interaction.response.send_message("Catálogo excluído." if row else "Catálogo não encontrado.", ephemeral=True)


class PublishCatalogModal(discord.ui.Modal, title="Publicar catálogo"):
    catalog_id = discord.ui.TextInput(label="ID do catálogo", max_length=10)
    channel_id = discord.ui.TextInput(label="ID do canal Discord", max_length=25)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin(interaction): return
        try: cid, ch_id = int(str(self.catalog_id)), int(str(self.channel_id))
        except ValueError:
            await interaction.response.send_message("ID inválido.", ephemeral=True); return
        channel = interaction.guild.get_channel(ch_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Canal de texto não encontrado. Ative o Modo Desenvolvedor para copiar o ID.", ephemeral=True); return
        with db() as con:
            cat = con.execute("SELECT * FROM catalogs WHERE id=? AND guild_id=?", (cid, interaction.guild_id)).fetchone()
            products = con.execute("SELECT * FROM products WHERE catalog_id=? AND active=1 ORDER BY id", (cid,)).fetchall()
        if not cat:
            await interaction.response.send_message("Catálogo não encontrado.", ephemeral=True); return
        if not products:
            await interaction.response.send_message("Este catálogo não tem produtos.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        header = discord.Embed(title=f"✦ {cat['name']} ✦", description=cat["description"] or "Escolha seu produto.",
                               color=0x8B5CF6)
        if cat["image_url"]: header.set_image(url=cat["image_url"])
        await channel.send(embed=header)
        for p in products:
            e = discord.Embed(title=f"✦ {p['name']}", description=p["description"] or "Sem descrição.", color=0x8B5CF6)
            e.add_field(name="💜 Preço", value=money(p["price_cents"]), inline=True)
            e.add_field(name="📦 Estoque", value=str(p["stock"]) if p["stock"] else "Esgotado", inline=True)
            if p["image_url"]: e.set_image(url=p["image_url"])
            await channel.send(embed=e, view=BuyView(p["id"]) if p["stock"] > 0 else None)
        await interaction.followup.send("Catálogo publicado.", ephemeral=True)


class BuyView(discord.ui.View):
    def __init__(self, product_id):
        super().__init__(timeout=None)
        self.product_id = product_id

    @discord.ui.button(label="🛒 Comprar", style=discord.ButtonStyle.success)
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        product = get_available_product(self.product_id, interaction.guild_id)
        if not product or product["stock"] < 1:
            await interaction.response.send_message("Produto esgotado ou indisponível.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"**{product['name']}** — **{money(product['price_cents'])}**\nEscolha como deseja pagar:",
            view=PaymentMethodView(self.product_id), ephemeral=True)


class PaymentMethodView(discord.ui.View):
    def __init__(self, product_id):
        super().__init__(timeout=300)
        self.product_id = product_id

    @discord.ui.button(label="🔗 Link de pagamento", style=discord.ButtonStyle.primary)
    async def link_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        product = get_available_product(self.product_id, interaction.guild_id)
        if not product or product["stock"] < 1:
            await interaction.followup.send("Produto esgotado ou indisponível.", ephemeral=True); return
        oid = create_order(interaction, product)
        try:
            checkout = create_checkout(oid, product["name"], product["price_cents"])
            if not checkout: raise RuntimeError("Checkout sem URL")
        except Exception as e:
            print("Checkout link error:", repr(e))
            with db() as con: con.execute("UPDATE orders SET status='checkout_error' WHERE id=?", (oid,))
            await interaction.followup.send("Não foi possível criar o link de pagamento. Avise a equipe.", ephemeral=True); return
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Abrir pagamento seguro", style=discord.ButtonStyle.link, url=checkout))
        await interaction.followup.send(f"Pedido **#{oid}** criado — **{product['name']}**. O pedido será confirmado automaticamente após o Mercado Pago notificar o pagamento.", view=view, ephemeral=True)

    @discord.ui.button(label="🟪 Pix QR Code", style=discord.ButtonStyle.success)
    async def pix_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PixEmailModal(self.product_id))


class PixEmailModal(discord.ui.Modal, title="Pagar com Pix"):
    email = discord.ui.TextInput(label="Seu e-mail", placeholder="voce@exemplo.com", max_length=150)

    def __init__(self, product_id):
        super().__init__(timeout=300)
        self.product_id = product_id

    async def on_submit(self, interaction: discord.Interaction):
        email = str(self.email).strip()
        if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
            await interaction.response.send_message("Informe um e-mail válido para gerar o Pix.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True, thinking=True)
        product = get_available_product(self.product_id, interaction.guild_id)
        if not product or product["stock"] < 1:
            await interaction.followup.send("Produto esgotado ou indisponível.", ephemeral=True); return
        oid = create_order(interaction, product)
        try:
            pix = create_pix(oid, product["name"], product["price_cents"], email)
            if not pix.get("copy_paste"):
                raise RuntimeError("Mercado Pago não retornou o código Pix")
            with db() as con:
                con.execute("UPDATE orders SET mp_payment_id=? WHERE id=?", (pix["id"], oid))
        except Exception as e:
            print("Checkout Pix error:", repr(e))
            with db() as con: con.execute("UPDATE orders SET status='checkout_error' WHERE id=?", (oid,))
            await interaction.followup.send("Não foi possível gerar o Pix agora. Avise a equipe.", ephemeral=True); return

        embed = discord.Embed(title=f"🟪 Pix — Pedido #{oid}",
                              description=f"**{product['name']}**\nValor: **{money(product['price_cents'])}**\n\nEscaneie o QR Code ou use Pix copia e cola. O pedido só será confirmado após a confirmação automática do Mercado Pago.",
                              color=0x8B5CF6)
        files = []
        if pix.get("qr"):
            try:
                qr_bytes = base64.b64decode(pix["qr"])
                qr_file = discord.File(BytesIO(qr_bytes), filename=f"pix_{oid}.png")
                embed.set_image(url=f"attachment://pix_{oid}.png")
                files.append(qr_file)
            except Exception as e:
                print("QR decode error:", repr(e))
        await interaction.followup.send(embed=embed, content=f"**Pix copia e cola:**\n```\n{pix['copy_paste']}\n```", files=files or [], ephemeral=True)
        if pix.get("ticket_url"):
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Abrir instruções do Pix", style=discord.ButtonStyle.link, url=pix["ticket_url"]))
            await interaction.followup.send("Também pode abrir as instruções de pagamento:", view=view, ephemeral=True)


class StockModal(discord.ui.Modal, title="Alterar estoque"):
    product_id = discord.ui.TextInput(label="ID do produto", max_length=10)
    stock = discord.ui.TextInput(label="Nova quantidade", max_length=10)

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_admin(interaction): return
        try: pid, qty = int(str(self.product_id)), int(str(self.stock))
        except ValueError:
            await interaction.response.send_message("Informe números válidos.", ephemeral=True); return
        if qty < 0:
            await interaction.response.send_message("Estoque não pode ser negativo.", ephemeral=True); return
        with db() as con:
            cur = con.execute("UPDATE products SET stock=? WHERE id=? AND catalog_id IN (SELECT id FROM catalogs WHERE guild_id=?)",
                              (qty, pid, interaction.guild_id))
        await interaction.response.send_message("Estoque atualizado." if cur.rowcount else "Produto não encontrado.", ephemeral=True)


class OrdersView(discord.ui.View):
    @discord.ui.button(label="Atualizar lista de pedidos", style=discord.ButtonStyle.secondary, emoji="📋")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await require_admin(interaction): return
        with db() as con:
            rows = con.execute("SELECT o.*,p.name FROM orders o JOIN products p ON p.id=o.product_id WHERE o.guild_id=? ORDER BY o.id DESC LIMIT 15",
                               (interaction.guild_id,)).fetchall()
        e = discord.Embed(title="📋 PEDIDOS RECENTES", color=0x8B5CF6)
        if not rows: e.description = "Ainda não há pedidos."
        for r in rows:
            e.add_field(name=f"Pedido #{r['id']} • {r['status']}",
                        value=f"Cliente: <@{r['user_id']}>\nProduto: {r['name']}\nTotal: {money(r['total_cents'])}\nPagamento: {r['mp_payment_id'] or 'aguardando'}",
                        inline=False)
        await interaction.response.edit_message(embed=e, view=self)


class AdminPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Criar catálogo", style=discord.ButtonStyle.primary, emoji="📚", row=0)
    async def create_catalog(self, interaction, button):
        if not await require_admin(interaction): return
        await interaction.response.send_modal(CreateCatalogModal())

    @discord.ui.button(label="Editar catálogo", style=discord.ButtonStyle.secondary, emoji="✏️", row=0)
    async def edit_catalog(self, interaction, button):
        if not await require_admin(interaction): return
        await interaction.response.send_modal(EditCatalogModal())

    @discord.ui.button(label="Excluir catálogo", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def delete_catalog(self, interaction, button):
        if not await require_admin(interaction): return
        await interaction.response.send_modal(DeleteCatalogModal())

    @discord.ui.button(label="Adicionar produto", style=discord.ButtonStyle.success, emoji="➕", row=1)
    async def add_product(self, interaction, button):
        if not await require_admin(interaction): return
        await interaction.response.send_modal(AddProductModal())

    @discord.ui.button(label="Editar produto", style=discord.ButtonStyle.secondary, emoji="🛠️", row=1)
    async def edit_product(self, interaction, button):
        if not await require_admin(interaction): return
        await interaction.response.send_modal(EditProductModal())

    @discord.ui.button(label="Excluir produto", style=discord.ButtonStyle.danger, emoji="❌", row=1)
    async def delete_product(self, interaction, button):
        if not await require_admin(interaction): return
        await interaction.response.send_modal(DeleteProductModal())

    @discord.ui.button(label="Alterar estoque", style=discord.ButtonStyle.primary, emoji="📦", row=2)
    async def edit_stock(self, interaction, button):
        if not await require_admin(interaction): return
        await interaction.response.send_modal(StockModal())

    @discord.ui.button(label="Publicar catálogo", style=discord.ButtonStyle.success, emoji="📣", row=2)
    async def publish(self, interaction, button):
        if not await require_admin(interaction): return
        await interaction.response.send_modal(PublishCatalogModal())

    @discord.ui.button(label="Ver catálogos", style=discord.ButtonStyle.secondary, emoji="🗂️", row=2)
    async def list_catalogs(self, interaction, button):
        with db() as con:
            rows = con.execute("SELECT id,name,description FROM catalogs WHERE guild_id=? ORDER BY id",
                               (interaction.guild_id,)).fetchall()
        e = discord.Embed(title="🗂️ CATÁLOGOS", color=0x8B5CF6)
        if not rows: e.description = "Nenhum catálogo. Clique em Criar catálogo."
        for r in rows[:25]:
            e.add_field(name=f"#{r['id']} • {r['name']}", value=r["description"] or "Sem descrição", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Configurar entrega", style=discord.ButtonStyle.primary, emoji="🚚", row=3)
    async def configure_delivery(self, interaction, button):
        if not await require_admin(interaction): return
        await interaction.response.send_modal(ConfigureDeliveryModal())

    @discord.ui.button(label="Adicionar keys", style=discord.ButtonStyle.success, emoji="🔑", row=3)
    async def add_keys(self, interaction, button):
        if not await require_admin(interaction): return
        await interaction.response.send_modal(AddKeysModal())

    @discord.ui.button(label="Ver pedidos", style=discord.ButtonStyle.secondary, emoji="🧾", row=4)
    async def show_orders(self, interaction, button):
        if not await require_admin(interaction): return
        with db() as con:
            rows = con.execute("SELECT o.*,p.name FROM orders o JOIN products p ON p.id=o.product_id WHERE o.guild_id=? ORDER BY o.id DESC LIMIT 15",
                               (interaction.guild_id,)).fetchall()
        e = discord.Embed(title="📋 PEDIDOS RECENTES", color=0x8B5CF6)
        if not rows: e.description = "Ainda não há pedidos."
        for r in rows:
            e.add_field(name=f"Pedido #{r['id']} • {r['status']}",
                        value=f"Cliente: <@{r['user_id']}>\nProduto: {r['name']}\nTotal: {money(r['total_cents'])}\nPagamento: {r['mp_payment_id'] or 'aguardando'}",
                        inline=False)
        await interaction.response.send_message(embed=e, view=OrdersView(), ephemeral=True)


@bot.tree.command(name="painel_loja", description="Abre o painel visual para administrar a loja")
async def painel_loja(interaction: discord.Interaction):
    if not await require_admin(interaction): return
    e = discord.Embed(title="💜 LOJA NEON • PAINEL ADMINISTRATIVO",
                      description="Use os botões abaixo para gerenciar a loja. Não precisa digitar comandos para cadastrar produtos.",
                      color=0x8B5CF6)
    e.add_field(name="📚 Catálogos", value="Criar, editar, excluir e visualizar", inline=True)
    e.add_field(name="🛍️ Produtos", value="Adicionar, editar, excluir e imagens por URL", inline=True)
    e.add_field(name="📦 Operação", value="Estoque, publicar catálogo e pedidos", inline=True)
    e.add_field(name="🚚 Entrega automática", value="Configurar por produto: key única, link ou texto; cadastrar keys pelo painel", inline=False)
    await interaction.response.send_message(embed=e, view=AdminPanel(), ephemeral=True)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Conectado como {bot.user}; {len(synced)} comandos sincronizados.")
    except Exception as e:
        print("Erro ao sincronizar comandos:", repr(e))


if not TOKEN:
    raise RuntimeError("Configure DISCORD_TOKEN no arquivo .env.")
init_db()
bot.run(TOKEN)
