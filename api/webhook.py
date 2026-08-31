"""
Webhook do bot do Telegram - roda como Serverless Function na Vercel.

O Telegram chama esta função via POST toda vez que o bot recebe uma
mensagem. A função "acorda", processa o comando e responde na hora -
não fica rodando continuamente (modelo serverless / sob demanda).

Variáveis de ambiente esperadas (configure no painel da Vercel):
  TELEGRAM_BOT_TOKEN      -> token dado pelo @BotFather
  API_BASE_URL            -> URL pública da API FastAPI (main.py/models.py)
  SUPORTE_CONTATO         -> texto/usuário exibido no comando /contato
  TELEGRAM_WEBHOOK_SECRET -> (opcional, recomendado) segredo para validar
                             que a chamada realmente veio do Telegram
"""
import json
import os
from http.server import BaseHTTPRequestHandler

import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
CONTATO_SUPORTE = os.environ.get("SUPORTE_CONTATO", "@seu_usuario")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

ITENS_POR_PAGINA = 5


# ---------------------------------------------------------------------------
# Helpers de comunicação com o Telegram
# ---------------------------------------------------------------------------

def tg_request(method: str, payload: dict) -> dict:
    resp = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=10)
    return resp.json()


def enviar_mensagem(chat_id, texto, teclado=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }
    if teclado:
        payload["reply_markup"] = teclado
    return tg_request("sendMessage", payload)


def editar_mensagem(chat_id, message_id, texto, teclado=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": texto,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if teclado:
        payload["reply_markup"] = teclado
    return tg_request("editMessageText", payload)


def responder_callback(callback_query_id, texto=None):
    payload = {"callback_query_id": callback_query_id}
    if texto:
        payload["text"] = texto
    return tg_request("answerCallbackQuery", payload)


# ---------------------------------------------------------------------------
# Helpers de comunicação com a API (main.py / models.py)
# ---------------------------------------------------------------------------

def buscar_produtos(skip=0, limit=ITENS_POR_PAGINA):
    r = requests.get(
        f"{API_BASE_URL}/produtos", params={"skip": skip, "limit": limit}, timeout=10
    )
    r.raise_for_status()
    return [p for p in r.json() if p.get("publicado", True)]


def buscar_cupons(skip=0, limit=ITENS_POR_PAGINA):
    r = requests.get(
        f"{API_BASE_URL}/cupons", params={"skip": skip, "limit": limit}, timeout=10
    )
    r.raise_for_status()
    return [c for c in r.json() if c.get("publicado", True)]


# ---------------------------------------------------------------------------
# Formatação de mensagens
# ---------------------------------------------------------------------------

def formatar_preco(valor):
    if valor is None:
        return "Consulte o valor"
    return f"R$ {float(valor):.2f}".replace(".", ",")


def montar_texto_produtos(produtos):
    if not produtos:
        return "Nenhuma oferta disponível no momento. Volte mais tarde! ⏳"
    linhas = ["🛒 <b>Ofertas em destaque</b>\n"]
    for p in produtos:
        if p.get("imagem"):
            linhas.append(f'<a href="{p["imagem"]}">&#8203;</a>')
            
        linhas.append(f"🔹 <b>{p.get('nome') or 'Produto'}</b>")
        preco_txt = formatar_preco(p.get("preco"))
        if p.get("preco_parcelado"):
            preco_txt += f" (ou parcelado {formatar_preco(p['preco_parcelado'])})"
        linhas.append(f"💰 {preco_txt}")
        if p.get("cupom"):
            linhas.append(f"🏷️ Cupom: <code>{p['cupom']}</code>")
        if p.get("link"):
            linhas.append(f"🔗 <a href=\"{p['link']}\">Ver oferta</a>")
        linhas.append("")
    return "\n".join(linhas)


def montar_texto_cupons(cupons):
    if not cupons:
        return "Nenhum cupom disponível no momento. Volte mais tarde! 🕐"
    linhas = ["🏷️ <b>Cupons em destaque</b>\n"]
    for c in cupons:
        linhas.append(f"🔹 <b>{c.get('nome') or 'Cupom'}</b>")
        if c.get("codigo"):
            linhas.append(f"Código: <code>{c['codigo']}</code>")
        if c.get("desconto"):
            linhas.append(f"Desconto: {c['desconto']}")
        if c.get("limite_minimo"):
            linhas.append(f"Pedido mínimo: {formatar_preco(c['limite_minimo'])}")
        if c.get("link"):
            linhas.append(f"🔗 <a href=\"{c['link']}\">Ver cupom</a>")
        linhas.append("")
    return "\n".join(linhas)


def teclado_paginacao(tipo, skip):
    nav = []
    if skip > 0:
        nav.append(
            {"text": "❮ Anterior", "callback_data": f"{tipo}:{max(0, skip - ITENS_POR_PAGINA)}"}
        )
    nav.append({"text": "Próximo ❯", "callback_data": f"{tipo}:{skip + ITENS_POR_PAGINA}"})
    return {"inline_keyboard": [nav, [{"text": "◀️ Menu", "callback_data": "menu"}]]}


def teclado_menu_principal():
    return {
        "inline_keyboard": [
            [{"text": "🛒 Produtos", "callback_data": "produtos:0"}],
            [{"text": "🏷️ Cupons", "callback_data": "cupons:0"}],
            [{"text": "❓ Ajuda", "callback_data": "ajuda"}],
            [{"text": "📞 Contato", "callback_data": "contato"}],
        ]
    }


# ---------------------------------------------------------------------------
# Textos fixos
# ---------------------------------------------------------------------------

TEXTO_START = (
    "👋 Olá! Eu sou o bot de ofertas e cupons.\n\n"
    "Use o menu abaixo ou os comandos:\n"
    "/produtos - Menu de ofertas de grupos do Telegram\n"
    "/cupons - Menu de cupons de grupos do Telegram\n"
    "/ajuda - Saiba como o bot funciona\n"
    "/contato - Entre em contato com o suporte"
)

TEXTO_AJUDA = (
    "ℹ️ <b>Como funciona</b>\n\n"
    "Eu reúno as melhores ofertas e cupons de grupos do Telegram em um "
    "só lugar.\n\n"
    "• /produtos mostra as últimas ofertas cadastradas\n"
    "• /cupons mostra os cupons disponíveis\n"
    "• Toque em 'Ver oferta' ou 'Ver cupom' para ir direto ao link\n"
    "• Use os botões Anterior/Próximo para navegar entre as páginas"
)

TEXTO_CONTATO = f"📞 Precisa de ajuda? Fale com o suporte: {CONTATO_SUPORTE}"


# ---------------------------------------------------------------------------
# Lógica dos comandos
# ---------------------------------------------------------------------------

def tratar_comando(chat_id, texto):
    comando = texto.split()[0].split("@")[0].lower()
    if comando == "/start":
        enviar_mensagem(chat_id, TEXTO_START, teclado_menu_principal())
    elif comando == "/produtos":
        produtos = buscar_produtos(0)
        enviar_mensagem(chat_id, montar_texto_produtos(produtos), teclado_paginacao("produtos", 0))
    elif comando == "/cupons":
        cupons = buscar_cupons(0)
        enviar_mensagem(chat_id, montar_texto_cupons(cupons), teclado_paginacao("cupons", 0))
    elif comando == "/ajuda":
        enviar_mensagem(chat_id, TEXTO_AJUDA)
    elif comando == "/contato":
        enviar_mensagem(chat_id, TEXTO_CONTATO)
    else:
        enviar_mensagem(chat_id, "Não entendi 🤔. Use /ajuda para ver os comandos disponíveis.")


def tratar_callback(callback_query):
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    data = callback_query["data"]
    responder_callback(callback_query["id"])

    if data == "menu":
        editar_mensagem(chat_id, message_id, TEXTO_START, teclado_menu_principal())
        return
    if data == "ajuda":
        editar_mensagem(chat_id, message_id, TEXTO_AJUDA, {"inline_keyboard": [[{"text": "◀️ Menu", "callback_data": "menu"}]]})
        return
    if data == "contato":
        editar_mensagem(chat_id, message_id, TEXTO_CONTATO, {"inline_keyboard": [[{"text": "◀️ Menu", "callback_data": "menu"}]]})
        return

    tipo, _, skip_str = data.partition(":")
    skip = int(skip_str) if skip_str.isdigit() else 0

    if tipo == "produtos":
        produtos = buscar_produtos(skip)
        if not produtos and skip > 0:
            responder_callback(callback_query["id"], "Não há mais ofertas.")
            return
        editar_mensagem(chat_id, message_id, montar_texto_produtos(produtos), teclado_paginacao("produtos", skip))
    elif tipo == "cupons":
        cupons = buscar_cupons(skip)
        if not cupons and skip > 0:
            responder_callback(callback_query["id"], "Não há mais cupons.")
            return
        editar_mensagem(chat_id, message_id, montar_texto_cupons(cupons), teclado_paginacao("cupons", skip))


def processar_update(update: dict):
    try:
        if "message" in update and "text" in update["message"]:
            msg = update["message"]
            if msg["text"].startswith("/"):
                tratar_comando(msg["chat"]["id"], msg["text"])
            else:
                enviar_mensagem(msg["chat"]["id"], "Use /ajuda para ver os comandos disponíveis.")
        elif "callback_query" in update:
            tratar_callback(update["callback_query"])
    except Exception as e:
        # Nunca deixa a exceção derrubar a function - só loga.
        print(f"[bot] erro ao processar update: {e}")


# ---------------------------------------------------------------------------
# Entrypoint da Vercel (Python runtime nativo, sem framework)
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Valida o segredo do webhook, se configurado, para garantir que a
        # chamada veio mesmo do Telegram (ver TELEGRAM_WEBHOOK_SECRET no README).
        if WEBHOOK_SECRET:
            recebido = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if recebido != WEBHOOK_SECRET:
                self.send_response(401)
                self.end_headers()
                return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b"{}"
        try:
            update = json.loads(body or b"{}")
        except json.JSONDecodeError:
            update = {}

        processar_update(update)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "bot ativo"}')
