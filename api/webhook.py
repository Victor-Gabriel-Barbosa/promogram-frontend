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

Correções aplicadas nesta versão (ver resumo enviado ao usuário):
  1. answerCallbackQuery só é chamado 1x por callback (evita erro silencioso
     e o toast "Não há mais ofertas/cupons" nunca aparecer).
  2. Falhas na API do backend são tratadas e avisadas ao usuário, em vez de
     deixar a mensagem sem resposta.
  3. Paginação agora é feita sobre os itens já filtrados por "publicado",
     buscando lotes brutos extras quando necessário - antes, uma página
     podia ficar vazia mesmo havendo mais itens publicados adiante.
  4. Campos vindos da API (nome, código, desconto etc.) são escapados com
     html.escape antes de entrar na mensagem HTML, evitando que um "<" ou
     "&" no texto quebre o envio (Telegram rejeita HTML inválido).
  5. Extras: comparação do secret com hmac.compare_digest, logging com
     traceback em vez de print, checagem de "ok" nas respostas do Telegram,
     sessão HTTP reaproveitada, e o botão "Próximo" só aparece quando
     realmente existe mais uma página.
"""
import hmac
import html
import json
import logging
import os
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
LOTE_BRUTO = max(ITENS_POR_PAGINA * 3, 15)  # buffer p/ compensar itens não publicados
MAX_TENTATIVAS_PAGINACAO = 10

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


def _buscar_raw(endpoint, skip, limit):
    r = sessao.get(
        f"{API_BASE_URL}/{endpoint}", params={"skip": skip, "limit": limit}, timeout=10
    )
    r.raise_for_status()
    return r.json()


def _buscar_publicados_paginado(endpoint, skip, limit):
    """
    A API mistura itens publicados e não publicados na mesma página, então
    filtrar depois de aplicar skip/limit dela pode esvaziar uma página sem
    que os itens tenham realmente acabado. Aqui buscamos lotes brutos
    (maiores) até juntar itens publicados suficientes para montar a página
    pedida e para saber se existe pelo menos mais 1 item além dela (o que
    decide se mostramos o botão "Próximo").

    Retorna (itens_da_pagina, tem_proxima_pagina).
    """
    publicados = []
    raw_skip = 0
    necessario = skip + limit + 1
    for _ in range(MAX_TENTATIVAS_PAGINACAO):
        lote = _buscar_raw(endpoint, raw_skip, LOTE_BRUTO)
        if not lote:
            break
        publicados.extend(item for item in lote if item.get("publicado", True))
        raw_skip += LOTE_BRUTO
        if len(lote) < LOTE_BRUTO or len(publicados) >= necessario:
            break
    pagina = publicados[skip:skip + limit]
    tem_proxima = len(publicados) > skip + limit
    return pagina, tem_proxima


def buscar_produtos(skip=0, limit=ITENS_POR_PAGINA):
    return _buscar_publicados_paginado("produtos", skip, limit)


def buscar_cupons(skip=0, limit=ITENS_POR_PAGINA):
    return _buscar_publicados_paginado("cupons", skip, limit)


def formatar_preco(valor):
    if valor is None:
        return "Consulte o valor"
    # ex.: 1234.5 -> "R$ 1.234,50" (separador de milhar + vírgula decimal)
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


def montar_texto_produtos(produtos):
    if not produtos:
        return "Nenhuma oferta disponível no momento. Volte mais tarde! ⏳"
    linhas = ["🛍️ <b>Ofertas em destaque</b>\n"]
    for p in produtos:
        adicionar_produto_ao_texto(linhas, p)
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


def montar_texto_cupons(cupons):
    if not cupons:
        return "Nenhum cupom disponível no momento. Volte mais tarde! 🕐"
    linhas = ["🎟️ <b>Cupons em destaque</b>\n"]
    for c in cupons:
        adicionar_cupom_ao_texto(linhas, c)
    return "\n".join(linhas)


def teclado_paginacao(tipo, skip, tem_proxima):
    nav = []
    if skip > 0:
        nav.append(
            {"text": "❮ Anterior", "callback_data": f"{tipo}:{max(0, skip - ITENS_POR_PAGINA)}"}
        )
    if tem_proxima:
        nav.append({"text": "Próximo ❯", "callback_data": f"{tipo}:{skip + ITENS_POR_PAGINA}"})
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


def tratar_comando(chat_id, texto):
    comando = texto.split()[0].split("@")[0].lower()
    if comando == "/start":
        enviar_mensagem(chat_id, TEXTO_START, teclado_menu_principal())
    elif comando == "/produtos":
        try:
            produtos, tem_proxima = buscar_produtos(0)
        except requests.RequestException:
            logger.exception("Erro ao buscar produtos")
            enviar_mensagem(chat_id, MSG_ERRO_API)
            return
        enviar_mensagem(
            chat_id, montar_texto_produtos(produtos), teclado_paginacao("produtos", 0, tem_proxima)
        )
    elif comando == "/cupons":
        try:
            cupons, tem_proxima = buscar_cupons(0)
        except requests.RequestException:
            logger.exception("Erro ao buscar cupons")
            enviar_mensagem(chat_id, MSG_ERRO_API)
            return
        enviar_mensagem(
            chat_id, montar_texto_cupons(cupons), teclado_paginacao("cupons", 0, tem_proxima)
        )
    elif comando == "/ajuda":
        enviar_mensagem(chat_id, TEXTO_AJUDA)
    elif comando == "/contato":
        enviar_mensagem(chat_id, TEXTO_CONTATO)
    else:
        enviar_mensagem(chat_id, "Não entendi 🤔. Use /ajuda para ver os comandos disponíveis.")


def tratar_callback(callback_query):
    mensagem = callback_query.get("message")
    if not mensagem:
        # mensagem original pode ter sido apagada; só confirma o callback
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

    tipo, _, skip_str = data.partition(":")
    skip = int(skip_str) if skip_str.isdigit() else 0

    if tipo not in ("produtos", "cupons"):
        responder_callback(callback_id)
        return

    buscar = buscar_produtos if tipo == "produtos" else buscar_cupons
    montar_texto = montar_texto_produtos if tipo == "produtos" else montar_texto_cupons

    try:
        itens, tem_proxima = buscar(skip)
    except requests.RequestException:
        logger.exception("Erro ao paginar %s (skip=%s)", tipo, skip)
        responder_callback(callback_id, "Erro ao buscar dados, tente novamente.")
        return

    if not itens and skip > 0:
        # já respondemos a callback aqui com o aviso e paramos - antes disso
        # havia uma 2ª tentativa de resposta que o Telegram rejeitava
        responder_callback(callback_id, "Não há mais itens para mostrar.")
        return

    editar_mensagem(
        chat_id, message_id, montar_texto(itens), teclado_paginacao(tipo, skip, tem_proxima)
    )
    responder_callback(callback_id)


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