"""
Webhook do bot do Telegram - roda como Serverless Function na Vercel.

O Telegram chama esta função via POST toda vez que o bot recebe uma
mensagem. A função "acorda", processa o comando e responde na hora -
não fica rodando continuamente (modelo serverless / sob demanda).

Variáveis de ambiente esperadas (configure no painel da Vercel):
  TELEGRAM_BOT_TOKEN      -> token dado pelo @BotFather
  API_BASE_URL            -> URL pública da API FastAPI (main.py/models.py)
  SUPORTE_CONTATO         -> texto/usuário exibido no comando /contato
  TELEGRAM_WEBHOOK_SECRET -> (recomendado fortemente) segredo para validar
                             que a chamada realmente veio do Telegram
"""
import hmac
import html
import json
import logging
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler

import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot_telegram")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
CONTATO_SUPORTE = os.environ.get("SUPORTE_CONTATO", "@seu_usuario")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

ITENS_POR_PAGINA = 5
LOTE_BRUTO = max(ITENS_POR_PAGINA * 3, 15)
MAX_TENTATIVAS_PAGINACAO = 10
TAMANHO_MAX_FILTRO = 60

MSG_ERRO_API = "⚠️ Não consegui buscar as informações agora. Tente novamente em instantes."

sessao = requests.Session()


def tg_request(method: str, payload: dict) -> dict:
    try:
        resp = sessao.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=10)
        dados = resp.json()
    except (requests.RequestException, ValueError) as e:
        logger.error("Falha ao chamar %s: %s", method, e)
        return {"ok": False, "error": str(e)}
    if not dados.get("ok"):
        logger.warning("Telegram retornou erro em %s: %s", method, dados)
    return dados


def enviar_mensagem(chat_id, texto, teclado=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "text": texto,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
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


def _buscar_raw(endpoint, skip, limit, nome=None):
    params = {"skip": skip, "limit": limit}
    if nome:
        params["nome"] = nome
    r = sessao.get(f"{API_BASE_URL}/{endpoint}", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def _buscar_publicados_paginado(endpoint, skip, limit, nome=None):
    publicados = []
    raw_skip = 0
    necessario = skip + limit + 1
    for _ in range(MAX_TENTATIVAS_PAGINACAO):
        lote = _buscar_raw(endpoint, raw_skip, LOTE_BRUTO, nome=nome)
        if not lote:
            break
        publicados.extend(item for item in lote if item.get("publicado", True))
        raw_skip += LOTE_BRUTO
        if len(lote) < LOTE_BRUTO or len(publicados) >= necessario:
            break
    pagina = publicados[skip:skip + limit]
    tem_proxima = len(publicados) > skip + limit
    return pagina, tem_proxima


def buscar_produtos(skip=0, limit=ITENS_POR_PAGINA, nome=None):
    return _buscar_publicados_paginado("produtos", skip, limit, nome=nome)


def buscar_cupons(skip=0, limit=ITENS_POR_PAGINA, nome=None):
    return _buscar_publicados_paginado("cupons", skip, limit, nome=nome)


def formatar_preco(valor):
    if valor is None:
        return "Consulte o valor"
    texto = f"{float(valor):,.2f}"
    texto = texto.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {texto}"


def adicionar_produto_ao_texto(linhas, p):
    linhas.append(f"🔹 <b>{html.escape(p.get('nome') or 'Produto')}</b>")
    preco_txt = formatar_preco(p.get("preco"))
    if p.get("preco_parcelado"):
        preco_txt += f" (ou parcelado {formatar_preco(p['preco_parcelado'])})"
    linhas.append(f"💵 {preco_txt}")
    if p.get("cupom"):
        linhas.append(f"🎟️ Cupom: <code>{html.escape(str(p['cupom']))}</code>")
    if p.get("link"):
        linhas.append(f"🔗 <a href=\"{html.escape(p['link'])}\">Ver oferta</a>")
    linhas.append("")


def montar_texto_produtos(produtos, filtro=None):
    if not produtos:
        if filtro:
            return f'Nenhuma oferta encontrada para "{html.escape(filtro)}". Tente outro termo! 🔎\n\n💡 <i>Dica: Responda esta mensagem para buscar novamente.</i>'
        return "Nenhuma oferta disponível no momento. Volte mais tarde! ⏳"
    
    if filtro:
        titulo = f'🛍️ <b>Ofertas - resultados para "{html.escape(filtro)}"</b>'
    else:
        titulo = "🛍️ <b>Ofertas em destaque</b>"
        
    linhas = [titulo + "\n"]
    for p in produtos:
        adicionar_produto_ao_texto(linhas, p)
        
    if not filtro:
        linhas.append("💡 <i>Dica: Responda esta mensagem com o nome do produto para buscar!</i>")
    
    return "\n".join(linhas)


def adicionar_cupom_ao_texto(linhas, c):
    linhas.append(f"🔹 <b>{html.escape(c.get('nome') or 'Cupom')}</b>")
    if c.get("codigo"):
        linhas.append(f"Código: <code>{html.escape(str(c['codigo']))}</code>")
    if c.get("desconto"):
        linhas.append(f"Desconto: {html.escape(str(c['desconto']))}")
    if c.get("limite_minimo"):
        linhas.append(f"Pedido mínimo: {formatar_preco(c['limite_minimo'])}")
    if c.get("link"):
        linhas.append(f"🔗 <a href=\"{html.escape(c['link'])}\">Ver cupom</a>")
    linhas.append("")


def montar_texto_cupons(cupons, filtro=None):
    if not cupons:
        if filtro:
            return f'Nenhum cupom encontrado para "{html.escape(filtro)}". Tente outro termo! 🔎\n\n💡 <i>Dica: Responda esta mensagem para buscar novamente.</i>'
        return "Nenhum cupom disponível no momento. Volte mais tarde! 🕐"
        
    if filtro:
        titulo = f'🎟️ <b>Cupons - resultados para "{html.escape(filtro)}"</b>'
    else:
        titulo = "🎟️ <b>Cupons em destaque</b>"
        
    linhas = [titulo + "\n"]
    for c in cupons:
        adicionar_cupom_ao_texto(linhas, c)
        
    if not filtro:
        linhas.append("💡 <i>Dica: Responda esta mensagem com o nome da loja/cupom para buscar!</i>")
    
    return "\n".join(linhas)


def _codificar_callback(tipo, skip, filtro=None):
    prefixo = f"{tipo}:{skip}"
    if not filtro:
        return prefixo
    quoted = urllib.parse.quote(filtro, safe="")
    espaco_disponivel = 64 - len(prefixo) - 1
    quoted = quoted[:max(espaco_disponivel, 0)]
    quoted = re.sub(r"%[0-9A-Fa-f]?$", "", quoted)
    return f"{prefixo}:{quoted}" if quoted else prefixo


def teclado_paginacao(tipo, skip, tem_proxima, filtro=None):
    nav = []
    if skip > 0:
        nav.append(
            {
                "text": "❮ Anterior",
                "callback_data": _codificar_callback(tipo, max(0, skip - ITENS_POR_PAGINA), filtro),
            }
        )
    if tem_proxima:
        nav.append(
            {
                "text": "Próximo ❯",
                "callback_data": _codificar_callback(tipo, skip + ITENS_POR_PAGINA, filtro),
            }
        )
    linhas_botoes = [nav] if nav else []
    linhas_botoes.append([{"text": "◀️ Menu", "callback_data": "menu"}])
    return {"inline_keyboard": linhas_botoes}


def teclado_menu_principal():
    return {
        "inline_keyboard": [
            [{"text": "🛍️ Produtos", "callback_data": "produtos:0"}],
            [{"text": "🎟️ Cupons", "callback_data": "cupons:0"}],
            [{"text": "ℹ️ Ajuda", "callback_data": "ajuda"}],
            [{"text": "📞 Contato", "callback_data": "contato"}],
        ]
    }


TEXTO_START = (
    "👋 <b>Olá! Seja bem-vindo ao Promogram!</b>\n\n"
    "🛍️ Encontre ofertas e cupons em poucos cliques.\n\n"
    "👇 O que você está procurando?"
)

TEXTO_AJUDA = (
    "ℹ️ <b>Como funciona</b>\n\n"
    "Eu reúno as melhores ofertas e cupons de grupos do Telegram em um "
    "só lugar.\n\n"
    
    "🛍️ <b>Produtos</b>\n"
    "• /produtos mostra as últimas ofertas cadastradas\n"
    "• Você pode buscar pelo nome usando, por exemplo, "
    "<code>/produtos tênis</code>\n\n"
    
    "🎟️ <b>Cupons</b>\n"
    "• /cupons mostra os cupons disponíveis\n"
    "• Você pode buscar pelo nome usando, por exemplo, "
    "<code>/cupons frete grátis</code>\n\n"
    
    "🔎 <b>Busca rápida</b>\n"
    "Deslize uma mensagem de menu (ou responda a ela) e digite o que "
    "deseja procurar. A lista será atualizada automaticamente.\n\n"
    
    "🔗 <b>Links</b>\n"
    "• Toque em <b>Ver oferta</b> para acessar uma oferta\n"
    "• Toque em <b>Ver cupom</b> para acessar um cupom\n\n"
    
    "💬 <b>Suporte</b>\n"
    "Use /contato para entrar em contato com o suporte."
)

TEXTO_CONTATO = f"📞 Precisa de ajuda? Fale com o suporte: {CONTATO_SUPORTE}"


def tratar_comando(chat_id, texto):
    partes = texto.split(maxsplit=1)
    comando = partes[0].split("@")[0].lower()
    filtro = partes[1].strip()[:TAMANHO_MAX_FILTRO] if len(partes) > 1 and partes[1].strip() else None

    if comando == "/start":
        enviar_mensagem(chat_id, TEXTO_START, teclado_menu_principal())
    elif comando == "/produtos":
        try:
            produtos, tem_proxima = buscar_produtos(0, nome=filtro)
        except requests.RequestException:
            logger.exception("Erro ao buscar produtos")
            enviar_mensagem(chat_id, MSG_ERRO_API)
            return
        enviar_mensagem(
            chat_id,
            montar_texto_produtos(produtos, filtro),
            teclado_paginacao("produtos", 0, tem_proxima, filtro),
        )
    elif comando == "/cupons":
        try:
            cupons, tem_proxima = buscar_cupons(0, nome=filtro)
        except requests.RequestException:
            logger.exception("Erro ao buscar cupons")
            enviar_mensagem(chat_id, MSG_ERRO_API)
            return
        enviar_mensagem(
            chat_id,
            montar_texto_cupons(cupons, filtro),
            teclado_paginacao("cupons", 0, tem_proxima, filtro),
        )
    elif comando == "/ajuda":
        enviar_mensagem(chat_id, TEXTO_AJUDA)
    elif comando == "/contato":
        enviar_mensagem(chat_id, TEXTO_CONTATO)
    else:
        enviar_mensagem(chat_id, "Não entendi 🤔. Use /ajuda para ver os comandos disponíveis.")


def identificar_tipo_menu(texto):
    """Analisa o texto da mensagem respondida para saber de qual menu se trata."""
    if "Ofertas" in texto or "Produto" in texto:
        return "produtos"
    return "cupons" if "Cupons" in texto or "Cupom" in texto else None


def tratar_resposta_menu(msg, reply_to):
    """Chamado quando o usuário responde diretamente a uma mensagem enviada pelo bot."""
    chat_id = msg["chat"]["id"]
    menu_message_id = reply_to["message_id"]
    texto_menu = reply_to.get("text", "")

    tipo = identificar_tipo_menu(texto_menu)
    if not tipo:
        enviar_mensagem(
            chat_id, 
            "⚠️ Só consigo fazer buscas se você responder a um menu de Produtos ou Cupons. Tente usar /produtos ou /cupons."
        )
        return

    texto_digitado = (msg.get("text") or "").strip()
    filtro = texto_digitado[:TAMANHO_MAX_FILTRO] if texto_digitado else None

    buscar = buscar_produtos if tipo == "produtos" else buscar_cupons
    montar_texto = montar_texto_produtos if tipo == "produtos" else montar_texto_cupons

    try:
        itens, tem_proxima = buscar(0, nome=filtro)
    except requests.RequestException:
        logger.exception("Erro ao buscar (%s) via Reply, filtro=%r", tipo, filtro)
        editar_mensagem(chat_id, menu_message_id, MSG_ERRO_API, teclado_paginacao(tipo, 0, False, filtro))
    else:
        editar_mensagem(
            chat_id,
            menu_message_id,
            montar_texto(itens, filtro),
            teclado_paginacao(tipo, 0, tem_proxima, filtro),
        )

    tg_request("deleteMessage", {"chat_id": chat_id, "message_id": msg["message_id"]})


def tratar_callback(callback_query):
    mensagem = callback_query.get("message")
    if not mensagem:
        responder_callback(callback_query["id"])
        return

    chat_id = mensagem["chat"]["id"]
    message_id = mensagem["message_id"]
    data = callback_query.get("data", "")
    callback_id = callback_query["id"]

    if data == "menu":
        editar_mensagem(chat_id, message_id, TEXTO_START, teclado_menu_principal())
        responder_callback(callback_id)
        return
    if data == "ajuda":
        editar_mensagem(
            chat_id, message_id, TEXTO_AJUDA,
            {"inline_keyboard": [[{"text": "◀️ Menu", "callback_data": "menu"}]]},
        )
        responder_callback(callback_id)
        return
    if data == "contato":
        editar_mensagem(
            chat_id, message_id, TEXTO_CONTATO,
            {"inline_keyboard": [[{"text": "◀️ Menu", "callback_data": "menu"}]]},
        )
        responder_callback(callback_id)
        return

    partes = data.split(":", 2)
    tipo = partes[0]
    skip_str = partes[1] if len(partes) > 1 else "0"
    skip = int(skip_str) if skip_str.isdigit() else 0
    filtro = urllib.parse.unquote(partes[2]) if len(partes) > 2 and partes[2] else None

    if tipo not in ("produtos", "cupons"):
        responder_callback(callback_id)
        return

    buscar = buscar_produtos if tipo == "produtos" else buscar_cupons
    montar_texto = montar_texto_produtos if tipo == "produtos" else montar_texto_cupons

    try:
        itens, tem_proxima = buscar(skip, nome=filtro)
    except requests.RequestException:
        logger.exception("Erro ao paginar %s (skip=%s, filtro=%r)", tipo, skip, filtro)
        responder_callback(callback_id, "Erro ao buscar dados, tente novamente.")
        return

    if not itens and skip > 0:
        responder_callback(callback_id, "Não há mais itens para mostrar.")
        return

    editar_mensagem(
        chat_id,
        message_id,
        montar_texto(itens, filtro),
        teclado_paginacao(tipo, skip, tem_proxima, filtro),
    )
    responder_callback(callback_id)


def processar_update(update: dict):
    try:
        if "message" in update and "text" in update["message"]:
            msg = update["message"]
            reply_to = msg.get("reply_to_message") or {}
            
            # Se o usuário respondeu a uma mensagem que o bot enviou
            if reply_to.get("from", {}).get("is_bot") and reply_to.get("text"):
                tratar_resposta_menu(msg, reply_to)
            elif msg["text"].startswith("/"):
                tratar_comando(msg["chat"]["id"], msg["text"])
            else:
                enviar_mensagem(
                    msg["chat"]["id"], 
                    "Não entendi 🤔. Use /ajuda para ver os comandos disponíveis, ou responda a um de meus menus para buscar."
                )
        elif "callback_query" in update:
            tratar_callback(update["callback_query"])
    except Exception:
        logger.exception("Erro ao processar update: %s", update)


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if WEBHOOK_SECRET:
            recebido = self.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
            if not hmac.compare_digest(recebido, WEBHOOK_SECRET):
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

        self.enviar_resposta_json(b'{"ok": true}')

    def do_GET(self):
        self.enviar_resposta_json(b'{"status": "bot ativo"}')

    def enviar_resposta_json(self, arg0):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(arg0)