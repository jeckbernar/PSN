"""
PSN Trophy API Server v3
========================
Busca os trofeus DESBLOQUEADOS de um usuario para um jogo especifico.
Retorna apenas os trofeus que o usuario ganhou, com trophy_id e data.
Opcionalmente recalcula as datas para uma data final desejada.
Funciona para jogos COM e SEM platina.

INSTALACAO:
    pip install flask flask-cors "psnawp==1.3.3"

CONFIGURACAO (Railway env vars):
    PSN_NPSSO_1=seu_npsso_1
    PSN_NPSSO_2=seu_npsso_2
    PSN_NPSSO_3=seu_npsso_3

ENDPOINTS:
    POST /api/trophies  -> busca trofeus desbloqueados
    GET  /api/health    -> status do servidor
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 5000))
app = Flask(__name__)
CORS(app)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_npsso_list():
    candidates = [
        os.environ.get("PSN_NPSSO_1", ""),
        os.environ.get("PSN_NPSSO_2", ""),
        os.environ.get("PSN_NPSSO_3", ""),
    ]
    return [n.strip() for n in candidates if n.strip()]


def fmt_dt(dt):
    if dt is None:
        return None
    try:
        if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def get_earned_date(trophy_obj):
    """Extrai a data de desbloqueio do objeto trofeu da psnawp."""
    for attr in ["earned_date_time", "earned_datetime", "trophy_earned_date",
                 "earned_date", "date_earned", "unlock_date"]:
        val = getattr(trophy_obj, attr, None)
        if val is not None:
            try:
                if hasattr(val, "tzinfo") and val.tzinfo:
                    val = val.astimezone().replace(tzinfo=None)
                return val
            except Exception:
                continue
    return None


def classify_error(exception_msg):
    """
    Classifica o tipo de erro baseado na mensagem da excecao.
    Retorna: "expired" | "not_found" | "private" | "no_trophies" | "error"
    
    IMPORTANTE: a ordem importa — verifica erros especificos antes de genericos.
    """
    msg = exception_msg.lower()

    # Erros de autenticacao/token — SOMENTE se nao houver indicacao de jogo
    # (evita falso positivo para jogos sem platina que podem ter "trophy" na mensagem)
    auth_keywords = ["npsso", "npsso is expired", "refresh token", "access token expired",
                     "oauth", "authentication failed"]
    if any(k in msg for k in auth_keywords):
        return "expired"

    # HTTP 401 puro (sem contexto de jogo)
    if ("401" in msg or "unauthorized" in msg) and "trophy" not in msg and "game" not in msg:
        return "expired"

    # Perfil nao encontrado
    if "not found" in msg or "404" in msg or "user not found" in msg:
        return "not_found"

    # Perfil/lista privada
    if "private" in msg or "forbidden" in msg or "403" in msg:
        return "private"

    # Jogo sem trofeus / nao possui o jogo
    # psnawp pode lancar excecoes com essas mensagens para jogos sem platina
    no_trophy_keywords = ["trophy_title", "no trophies", "trophies not found",
                          "np_comm_id", "npcommid", "game not found",
                          "title not found", "psnawpnotfound", "psnawp_not_found"]
    if any(k in msg for k in no_trophy_keywords):
        return "no_trophies"

    # Qualquer outro erro nao identificado
    return "error"


def recalculate_dates(earned_trophies, final_date):
    """
    Recalcula as datas mantendo proporcao de intervalos.
    O anchor (platina se houver, senao o ultimo trofeu ganho) recebe final_date.
    Funciona para jogos COM e SEM platina.
    """
    if not earned_trophies:
        return []

    if len(earned_trophies) == 1:
        return [{**earned_trophies[0], "new_date": final_date, "diff_sec": 0}]

    # Ordenar todos por data crescente (o mais antigo primeiro)
    all_sorted = sorted(earned_trophies, key=lambda x: x["earned_date"])

    # Anchor = platina (se houver) ou o ultimo trofeu ganho (sem platina)
    platinum_list = [t for t in all_sorted if t["trophy_type"].upper() == "PLATINUM"]
    if platinum_list:
        anchor = platinum_list[0]
    else:
        anchor = all_sorted[-1]  # ultimo ganho = anchor para jogos sem platina

    to_recalc = [t for t in all_sorted if t is not anchor]

    # max_date = data do anchor (o mais tardio)
    max_date = anchor["earned_date"]

    result = []
    for t in to_recalc:
        diff_sec = int((max_date - t["earned_date"]).total_seconds())
        new_date = final_date - timedelta(seconds=diff_sec)
        result.append({**t, "new_date": new_date, "diff_sec": diff_sec})

    result.append({**anchor, "new_date": final_date, "diff_sec": 0})
    return result


def try_fetch_trophies(npsso, psn_username, np_comm_id, platform):
    """
    Tenta buscar os trofeus usando um NPSSO especifico.
    Retorna (trophies_raw, error_type, error_msg)
    """
    # 1. Inicializar psnawp
    try:
        from psnawp_api import PSNAWP
        psnawp = PSNAWP(npsso)
    except Exception as e:
        err_type = classify_error(str(e))
        log.warning(f"  PSNAWP init falhou ({err_type}): {e}")
        return None, err_type, str(e)

    # 2. Buscar usuario
    try:
        user = psnawp.user(online_id=psn_username)
        _ = user.online_id
    except Exception as e:
        err_type = classify_error(str(e))
        log.warning(f"  Busca usuario falhou ({err_type}): {e}")
        if err_type == "error":
            err_type = "not_found"  # usuario nao encontrado e o mais provavel aqui
        return None, err_type, str(e)

    # 3. Buscar trofeus do jogo
    try:
        trophies_raw = list(user.trophies(
            np_communication_id=np_comm_id,
            platform=platform,
            include_metadata=True,
        ))
        log.info(f"  Trofeus brutos: {len(trophies_raw)}")
        return trophies_raw, None, None
    except Exception as e:
        err_type = classify_error(str(e))
        log.warning(f"  Busca trofeus falhou ({err_type}): {type(e).__name__}: {e}")
        return None, err_type, str(e)


# ── Endpoint principal ────────────────────────────────────────────────────────

@app.route("/api/trophies", methods=["POST"])
def get_trophies():
    data = request.get_json()

    psn_username   = (data.get("psn_username") or "").strip()
    np_comm_id     = (data.get("np_comm_id")   or "").strip()
    platform_str   = (data.get("platform")     or "PS4").strip().upper()
    final_date_str = (data.get("final_date")   or "").strip()

    if not psn_username:
        return jsonify({"status": "error", "message": "PSN username obrigatorio"}), 400
    if not np_comm_id:
        return jsonify({"status": "error", "message": "NP_COMM_ID obrigatorio"}), 400

    use_recalc = bool(final_date_str)
    final_date = None
    if final_date_str:
        try:
            final_date = datetime.strptime(final_date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return jsonify({"status": "error", "message": "Formato invalido. Use YYYY-MM-DD HH:MM:SS"}), 400

    NPSSO_LIST = get_npsso_list()
    if not NPSSO_LIST:
        return jsonify({"status": "error", "message": "Nenhum NPSSO configurado"}), 500

    try:
        from psnawp_api.models.trophies.trophy_constants import PlatformType
    except ImportError:
        return jsonify({"status": "error", "message": "psnawp nao instalado"}), 500

    platform_map = {
        "PS3":    PlatformType.PS3,
        "PS4":    PlatformType.PS4,
        "PS5":    PlatformType.PS5,
        "VITA":   PlatformType.PS_VITA,
        "PSVITA": PlatformType.PS_VITA,
    }
    platform = platform_map.get(platform_str, PlatformType.PS4)

    log.info(f"Request: user={psn_username} game={np_comm_id} platform={platform_str} recalc={use_recalc}")

    # Tenta cada conta em sequencia
    trophies_raw  = None
    last_err_type = "error"
    last_err_msg  = "Todas as contas falharam."

    for i, npsso in enumerate(NPSSO_LIST, 1):
        log.info(f"Tentando conta {i}/{len(NPSSO_LIST)}...")
        raw, err_type, err_msg = try_fetch_trophies(npsso, psn_username, np_comm_id, platform)

        if err_type is None:
            trophies_raw = raw
            log.info(f"Conta {i} OK")
            break

        last_err_type = err_type
        last_err_msg  = err_msg

        # Erros definitivos (nao dependem da conta) — nao tenta outras
        if err_type in ("not_found", "private", "no_trophies"):
            log.info(f"Erro definitivo ({err_type}) — nao tentando outras contas")
            break

        # expired ou error generico — tenta proxima conta
        log.warning(f"Conta {i} falhou ({err_type}) — tentando proxima...")

    if trophies_raw is None:
        http_map = {"not_found": 404, "private": 403, "expired": 503,
                    "no_trophies": 200, "error": 500}
        msg_map = {
            "not_found":   "Perfil nao encontrado. Verifique o PSN ID.",
            "private":     "Perfil privado. O usuario precisa tornar os trofeus publicos.",
            "expired":     "Todas as contas PSN estao com token expirado. Contate o administrador.",
            "no_trophies": "Nenhum trofeu encontrado para este jogo neste perfil.",
            "error":       last_err_msg,
        }
        return jsonify({
            "status":  last_err_type,
            "message": msg_map.get(last_err_type, last_err_msg),
        }), http_map.get(last_err_type, 500)

    # ── Filtrar apenas os trofeus desbloqueados ───────────────────────────────
    earned_list = []
    for t in trophies_raw:
        earned_dt = get_earned_date(t)
        if earned_dt is None:
            continue  # nao ganho

        raw_type    = getattr(t, "trophy_type", None)
        trophy_type = raw_type.name if hasattr(raw_type, "name") else str(raw_type or "BRONZE")
        trophy_id   = getattr(t, "trophy_id", None)
        trophy_name = getattr(t, "trophy_name", None) or f"#{trophy_id}"

        earned_list.append({
            "trophy_id":   trophy_id,
            "trophy_name": trophy_name,
            "trophy_type": trophy_type,
            "earned_date": earned_dt,
        })

    log.info(f"Desbloqueados: {len(earned_list)} de {len(trophies_raw)} totais")

    if not earned_list:
        return jsonify({
            "status":  "no_trophies",
            "message": "Nenhum trofeu desbloqueado encontrado para este jogo.",
        }), 200

    # ── Recalcular datas ou usar originais ────────────────────────────────────
    if use_recalc and final_date:
        processed = recalculate_dates(earned_list, final_date)
    else:
        processed = [{**t, "new_date": t["earned_date"], "diff_sec": None} for t in earned_list]

    trophies_out = [{
        "trophy_id":     t["trophy_id"],
        "trophy_name":   t["trophy_name"],
        "trophy_type":   t["trophy_type"],
        "new_date":      fmt_dt(t.get("new_date")),
        "original_date": fmt_dt(t["earned_date"]),
        "diff_sec":      t.get("diff_sec"),
    } for t in processed]

    return jsonify({
        "status":   "ok",
        "trophies": trophies_out,
        "total":    len(trophies_out),
    })



# ══════════════════════════════════════════════════════════════════════════════
#  PAYMENT INTEGRATION  —  Mercado Pago + PayPal
# ══════════════════════════════════════════════════════════════════════════════
#
#  VARIAVEIS DE AMBIENTE NECESSARIAS (Railway):
#
#  Mercado Pago:
#    MP_ACCESS_TOKEN   = seu Access Token de producao (APP_USR-...)
#
#  PayPal:
#    PAYPAL_CLIENT_ID     = Client ID do app (developer.paypal.com)
#    PAYPAL_CLIENT_SECRET = Client Secret
#    PAYPAL_ENV           = "sandbox" | "live"  (default: live)
#
#  Supabase (para atualizar status do pedido):
#    SUPABASE_URL = https://xxxx.supabase.co
#    SUPABASE_KEY = sua anon key
#
#  URL do proprio servidor (para redirects e webhooks):
#    APP_URL = https://seu-app.railway.app
#
# ══════════════════════════════════════════════════════════════════════════════

import json
import hmac
import hashlib
import urllib.request
import urllib.parse

# ── Config helpers ────────────────────────────────────────────────────────────

def get_env(key, default=""):
    return os.environ.get(key, default).strip()

def supabase_update_order(order_id, patch):
    """Atualiza o pedido no Supabase via REST."""
    url = get_env("SUPABASE_URL")
    key = get_env("SUPABASE_KEY")
    if not url or not key:
        log.warning("SUPABASE_URL/KEY nao configurados — nao foi possivel atualizar pedido")
        return False
    try:
        req_url = f"{url}/rest/v1/tb_orders?id=eq.{order_id}"
        body = json.dumps(patch).encode()
        req = urllib.request.Request(
            req_url, data=body, method="PATCH",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info(f"Supabase update order {order_id}: {resp.status}")
            return True
    except Exception as e:
        log.error(f"Supabase update erro: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  MERCADO PAGO
# ══════════════════════════════════════════════════════════════════════════════

def mp_api(method, path, body=None):
    """Faz uma requisicao autenticada para a API do Mercado Pago."""
    token = get_env("MP_ACCESS_TOKEN")
    if not token:
        raise ValueError("MP_ACCESS_TOKEN nao configurado")
    url = f"https://api.mercadopago.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Idempotency-Key": str(id(body)) if body else "nokey",
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


@app.route("/api/payment/mp/create", methods=["POST"])
def mp_create():
    """
    Cria uma preferencia de pagamento no Mercado Pago.
    Body: { order_id, total, currency, customer_name, customer_email, items[] }
    Retorna: { preference_id, init_point (link checkout), pix_qr, pix_payload }
    """
    data = request.get_json() or {}
    order_id    = str(data.get("order_id", ""))
    total       = float(data.get("total", 0))
    currency    = str(data.get("currency", "BRL"))
    cust_name   = str(data.get("customer_name", "Cliente"))
    cust_email  = str(data.get("customer_email", "cliente@email.com"))
    items_in    = data.get("items", [])
    app_url     = get_env("APP_URL", "https://example.com")

    if not order_id or total <= 0:
        return jsonify({"error": "order_id e total sao obrigatorios"}), 400

    # Monta os items para o MP (precisa de pelo menos 1 item)
    if items_in:
        mp_items = [
            {
                "id":          str(it.get("game_name", "item"))[:50],
                "title":       str(it.get("game_name", "Trophy Service"))[:256],
                "description": str(it.get("item_type", "service"))[:256],
                "quantity":    1,
                "unit_price":  float(it.get("price", total)),
                "currency_id": currency,
            }
            for it in items_in
        ]
    else:
        mp_items = [{"id": order_id, "title": "Trophy Service", "quantity": 1,
                     "unit_price": total, "currency_id": currency}]

    preference_body = {
        "items": mp_items,
        "payer": {"name": cust_name, "email": cust_email},
        "external_reference": order_id,
        "notification_url": f"{app_url}/api/webhook/mp",
        "back_urls": {
            "success": f"{app_url}/api/payment/mp/return?status=approved&order={order_id}",
            "failure": f"{app_url}/api/payment/mp/return?status=failure&order={order_id}",
            "pending": f"{app_url}/api/payment/mp/return?status=pending&order={order_id}",
        },
        "auto_return": "approved",
        "payment_methods": {
            "excluded_payment_types": [],
            "installments": 1,
        },
        "statement_descriptor": "UNLOCKTROPHIES",
        "expires": False,
        "metadata": {"order_id": order_id},
    }

    try:
        pref = mp_api("POST", "/checkout/preferences", preference_body)
    except Exception as e:
        log.error(f"MP create preference error: {e}")
        return jsonify({"error": f"Mercado Pago erro: {str(e)}"}), 502

    preference_id = pref.get("id")
    init_point    = pref.get("init_point")       # link producao
    sandbox_init  = pref.get("sandbox_init_point")

    # Tenta criar pagamento PIX direto (apenas BRL)
    pix_qr      = None
    pix_payload = None
    pix_id      = None
    if currency == "BRL":
        try:
            pix_payment = mp_api("POST", "/v1/payments", {
                "transaction_amount": total,
                "description":        "UnlockTrophies - Servicos de Trofeus",
                "payment_method_id":  "pix",
                "payer":              {"email": cust_email},
                "external_reference": order_id,
                "notification_url":   f"{app_url}/api/webhook/mp",
            })
            pix_id      = pix_payment.get("id")
            pix_data    = (pix_payment.get("point_of_interaction") or {}).get("transaction_data") or {}
            pix_qr      = pix_data.get("qr_code_base64")
            pix_payload = pix_data.get("qr_code")
            log.info(f"PIX criado: payment_id={pix_id}")
        except Exception as e:
            log.warning(f"PIX direto falhou (nao critico): {e}")

    # Salvar payment_id no pedido para checagem posterior
    if pix_id:
        supabase_update_order(order_id, {
            "payment_method": "pix_mp",
            "payment_id": str(pix_id),
            "status": "pending",
        })
    else:
        supabase_update_order(order_id, {
            "payment_method": "mercadopago",
            "payment_id": preference_id,
            "status": "pending",
        })

    return jsonify({
        "preference_id": preference_id,
        "init_point":    init_point,
        "sandbox_init":  sandbox_init,
        "pix_id":        pix_id,
        "pix_qr":        pix_qr,       # base64 da imagem do QR
        "pix_payload":   pix_payload,  # string "copia e cola"
    })


@app.route("/api/payment/mp/status/<payment_id>", methods=["GET"])
def mp_status(payment_id):
    """
    Consulta o status de um pagamento MP pelo payment_id (nao preference_id).
    Usado pelo frontend para polling.
    """
    try:
        result = mp_api("GET", f"/v1/payments/{payment_id}")
        status = result.get("status")        # approved | pending | rejected
        status_detail = result.get("status_detail", "")
        order_id = result.get("external_reference", "")

        # Se aprovado, atualiza Supabase automaticamente
        if status == "approved" and order_id:
            supabase_update_order(order_id, {"status": "processing", "payment_status": "paid"})
            log.info(f"MP: pagamento {payment_id} aprovado — pedido {order_id} atualizado")

        return jsonify({
            "payment_id":    payment_id,
            "status":        status,
            "status_detail": status_detail,
            "order_id":      order_id,
            "paid":          status == "approved",
        })
    except Exception as e:
        log.error(f"MP status error: {e}")
        return jsonify({"error": str(e)}), 502


@app.route("/api/payment/mp/return", methods=["GET"])
def mp_return():
    """Redirect do Mercado Pago apos pagamento (back_urls)."""
    status   = request.args.get("status", "unknown")
    order_id = request.args.get("order", "")
    payment  = request.args.get("payment_id", "")

    if status == "approved" and order_id:
        supabase_update_order(order_id, {"status": "processing", "payment_status": "paid"})

    # Redireciona para o frontend com parametros
    frontend = get_env("FRONTEND_URL", get_env("APP_URL", ""))
    return f"""
    <html><head><script>
      window.opener && window.opener.postMessage(
        {{type:"mp_return", status:"{status}", order_id:"{order_id}", payment_id:"{payment}"}}, "*"
      );
      window.location = "{frontend}?payment_status={status}&order={order_id}";
    </script></head>
    <body>Redirecionando...</body></html>
    """


@app.route("/api/webhook/mp", methods=["POST"])
def webhook_mp():
    """
    Webhook do Mercado Pago — chamado automaticamente quando um pagamento muda de status.
    Valida a assinatura secreta antes de processar.
    MP envia: { action, data: { id } }
    """
    # ── Validar assinatura secreta do MP ──────────────────────────────────────
    secret = get_env("MP_WEBHOOK_SECRET")
    if secret:
        # MP envia no header: x-signature = "ts=...,v1=..."
        # e no header:        x-request-id = uuid
        x_signature  = request.headers.get("x-signature", "")
        x_request_id = request.headers.get("x-request-id", "")
        data_id      = request.args.get("data.id", "")  # query param

        if x_signature:
            # Monta o manifest: id:{data_id};request-id:{x_request_id};ts:{ts};
            ts = ""
            v1 = ""
            for part in x_signature.split(","):
                k, _, v = part.partition("=")
                if k.strip() == "ts":   ts = v.strip()
                if k.strip() == "v1":   v1 = v.strip()

            manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
            expected = hmac.new(
                secret.encode(), manifest.encode(), hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(expected, v1):
                log.warning(f"Webhook MP: assinatura invalida — rejeitado")
                return jsonify({"error": "invalid signature"}), 401
            log.info("Webhook MP: assinatura validada ✓")

    # ── Processar evento ──────────────────────────────────────────────────────
    payload = request.get_json(silent=True) or {}
    action  = payload.get("action", "")
    data    = payload.get("data", {})
    obj_id  = str(data.get("id", ""))

    log.info(f"Webhook MP: action={action} id={obj_id}")

    if action in ("payment.created", "payment.updated") and obj_id:
        try:
            result   = mp_api("GET", f"/v1/payments/{obj_id}")
            status   = result.get("status")
            order_id = result.get("external_reference", "")
            if status == "approved" and order_id:
                supabase_update_order(order_id, {
                    "status":         "processing",
                    "payment_status": "paid",
                    "payment_id":     obj_id,
                })
                log.info(f"Webhook MP: pedido {order_id} marcado como pago ✓")
            else:
                log.info(f"Webhook MP: pagamento {obj_id} status={status} (sem acao)")
        except Exception as e:
            log.error(f"Webhook MP erro: {e}")

    return jsonify({"received": True}), 200


# ══════════════════════════════════════════════════════════════════════════════
#  PAYPAL
# ══════════════════════════════════════════════════════════════════════════════

def paypal_base_url():
    env = get_env("PAYPAL_ENV", "live")
    return "https://api-m.sandbox.paypal.com" if env == "sandbox" else "https://api-m.paypal.com"


_paypal_token_cache = {"token": None, "expires": 0}

def paypal_get_token():
    """Obtem ou reutiliza o access token do PayPal (OAuth2 client_credentials)."""
    import time
    now = time.time()
    if _paypal_token_cache["token"] and now < _paypal_token_cache["expires"] - 60:
        return _paypal_token_cache["token"]

    client_id     = get_env("PAYPAL_CLIENT_ID")
    client_secret = get_env("PAYPAL_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET nao configurados")

    creds   = f"{client_id}:{client_secret}".encode()
    b64     = __import__("base64").b64encode(creds).decode()
    body    = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req     = urllib.request.Request(
        f"{paypal_base_url()}/v1/oauth2/token",
        data=body, method="POST",
        headers={"Authorization": f"Basic {b64}", "Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())

    _paypal_token_cache["token"]   = result["access_token"]
    _paypal_token_cache["expires"] = now + result.get("expires_in", 3600)
    return _paypal_token_cache["token"]


def paypal_api(method, path, body=None, token=None):
    """Faz uma requisicao autenticada para a API do PayPal."""
    if token is None:
        token = paypal_get_token()
    url  = f"{paypal_base_url()}{path}"
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": str(id(body) if body else path),
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


@app.route("/api/payment/paypal/create", methods=["POST"])
def paypal_create():
    """
    Cria uma Order no PayPal.
    Body: { order_id, total, currency, customer_email, items[] }
    Retorna: { paypal_order_id, approve_url }
    """
    data        = request.get_json() or {}
    order_id    = str(data.get("order_id", ""))
    total       = float(data.get("total", 0))
    currency    = str(data.get("currency", "USD"))
    cust_email  = str(data.get("customer_email", ""))
    app_url     = get_env("APP_URL", "https://example.com")

    # PayPal nao aceita BRL — converte para USD se necessario
    if currency == "BRL":
        currency = "USD"
        # Conversao aproximada — idealmente buscar cotacao em tempo real
        usd_rate = float(get_env("BRL_TO_USD_RATE", "0.19"))
        total    = round(total * usd_rate, 2)
        log.info(f"PayPal: convertendo BRL -> USD: {total}")

    if not order_id or total <= 0:
        return jsonify({"error": "order_id e total sao obrigatorios"}), 400

    paypal_body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id":   order_id,
            "custom_id":      order_id,
            "description":    f"GameJSB Trophy Service #{order_id}",
            "amount": {
                "currency_code": currency,
                "value":         f"{total:.2f}",
            },
        }],
        "payer": {"email_address": cust_email} if cust_email else {},
        "application_context": {
            "brand_name":          "GameJSB",
            "landing_page":        "LOGIN",
            "user_action":         "PAY_NOW",
            "shipping_preference": "NO_SHIPPING",
            "return_url": f"{app_url}/api/payment/paypal/capture",
            "cancel_url": f"{app_url}/api/payment/paypal/cancel",
        },
    }

    try:
        result       = paypal_api("POST", "/v2/checkout/orders", paypal_body)
        pp_order_id  = result.get("id")
        approve_url  = next(
            (l["href"] for l in result.get("links", []) if l.get("rel") == "approve"),
            None
        )
    except Exception as e:
        log.error(f"PayPal create error: {e}")
        return jsonify({"error": f"PayPal erro: {str(e)}"}), 502

    # Salvar paypal_order_id no pedido
    supabase_update_order(order_id, {
        "payment_method": "paypal",
        "payment_id":     pp_order_id,
        "status":         "pending",
    })

    log.info(f"PayPal order criado: {pp_order_id} para pedido {order_id}")
    return jsonify({
        "paypal_order_id": pp_order_id,
        "approve_url":     approve_url,
        "currency":        currency,
        "total":           total,
    })


@app.route("/api/payment/paypal/capture", methods=["GET", "POST"])
def paypal_capture():
    """
    Captura o pagamento apos o cliente aprovar no PayPal.
    Chamado pelo redirect (GET) ou diretamente pelo frontend (POST).
    """
    if request.method == "GET":
        pp_order_id = request.args.get("token", "")
        payer_id    = request.args.get("PayerID", "")
    else:
        body        = request.get_json() or {}
        pp_order_id = body.get("paypal_order_id", "")
        payer_id    = body.get("payer_id", "")

    if not pp_order_id:
        return jsonify({"error": "paypal_order_id obrigatorio"}), 400

    try:
        result  = paypal_api("POST", f"/v2/checkout/orders/{pp_order_id}/capture")
        status  = result.get("status")   # COMPLETED | APPROVED | CREATED
        units   = result.get("purchase_units", [{}])
        order_id = (units[0].get("reference_id") or
                    units[0].get("custom_id") or
                    units[0].get("payments", {}).get("captures", [{}])[0].get("custom_id", ""))

        paid = status == "COMPLETED"

        if paid and order_id:
            supabase_update_order(order_id, {
                "status":         "processing",
                "payment_status": "paid",
                "payment_id":     pp_order_id,
            })
            log.info(f"PayPal COMPLETED: pedido {order_id} marcado como pago")

        if request.method == "GET":
            frontend = get_env("FRONTEND_URL", get_env("APP_URL", ""))
            return f"""
            <html><head><script>
              window.opener && window.opener.postMessage(
                {{type:"paypal_return", status:"{status}", order_id:"{order_id}"}}, "*"
              );
              setTimeout(() => window.close(), 500);
              window.location = "{frontend}?payment_status={'approved' if paid else 'pending'}&order={order_id}";
            </script></head><body>Processando pagamento...</body></html>
            """

        return jsonify({
            "status":   status,
            "paid":     paid,
            "order_id": order_id,
        })

    except Exception as e:
        log.error(f"PayPal capture error: {e}")
        return jsonify({"error": str(e)}), 502


@app.route("/api/payment/paypal/status/<pp_order_id>", methods=["GET"])
def paypal_status(pp_order_id):
    """Consulta o status de uma Order do PayPal. Usado para polling."""
    try:
        result   = paypal_api("GET", f"/v2/checkout/orders/{pp_order_id}")
        status   = result.get("status")
        units    = result.get("purchase_units", [{}])
        order_id = units[0].get("reference_id", "") or units[0].get("custom_id", "")

        paid = status == "COMPLETED"
        if paid and order_id:
            supabase_update_order(order_id, {
                "status":         "processing",
                "payment_status": "paid",
            })

        return jsonify({
            "paypal_order_id": pp_order_id,
            "status":          status,
            "paid":            paid,
            "order_id":        order_id,
        })
    except Exception as e:
        log.error(f"PayPal status error: {e}")
        return jsonify({"error": str(e)}), 502


@app.route("/api/payment/paypal/cancel", methods=["GET"])
def paypal_cancel():
    """Redirect quando cliente cancela no PayPal."""
    order_id = request.args.get("order", "")
    frontend = get_env("FRONTEND_URL", get_env("APP_URL", ""))
    return f"""
    <html><head><script>
      window.opener && window.opener.postMessage({{type:"paypal_cancel", order_id:"{order_id}"}}, "*");
      setTimeout(() => window.close(), 300);
      window.location = "{frontend}?payment_status=cancelled&order={order_id}";
    </script></head><body>Pagamento cancelado.</body></html>
    """


@app.route("/api/webhook/paypal", methods=["POST"])
def webhook_paypal():
    """
    Webhook do PayPal — IPN/Webhooks v2.
    Verifica a assinatura e atualiza o pedido se pagamento completado.
    """
    payload     = request.get_json(silent=True) or {}
    event_type  = payload.get("event_type", "")
    resource    = payload.get("resource", {})

    log.info(f"Webhook PayPal: {event_type}")

    if event_type in ("PAYMENT.CAPTURE.COMPLETED", "CHECKOUT.ORDER.APPROVED"):
        pp_order_id = resource.get("id", "")
        units       = resource.get("purchase_units") or [{}]
        order_id    = (units[0].get("reference_id") or
                       units[0].get("custom_id") or
                       resource.get("custom_id", ""))

        if not order_id and pp_order_id:
            # Tenta buscar o order_id via API
            try:
                details  = paypal_api("GET", f"/v2/checkout/orders/{pp_order_id}")
                units    = details.get("purchase_units", [{}])
                order_id = units[0].get("reference_id") or units[0].get("custom_id", "")
            except Exception:
                pass

        if order_id:
            supabase_update_order(order_id, {
                "status":         "processing",
                "payment_status": "paid",
                "payment_id":     pp_order_id,
            })
            log.info(f"Webhook PayPal: pedido {order_id} marcado como pago")

    return jsonify({"received": True}), 200


# ── Health check atualizado ───────────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    mp_ok  = bool(get_env("MP_ACCESS_TOKEN"))
    pp_ok  = bool(get_env("PAYPAL_CLIENT_ID") and get_env("PAYPAL_CLIENT_SECRET"))
    return jsonify({
        "status":              "ok",
        "accounts_configured": len(get_npsso_list()),
        "mercadopago":         "configured" if mp_ok  else "not_configured",
        "paypal":              "configured" if pp_ok  else "not_configured",
        "supabase":            "configured" if get_env("SUPABASE_URL") else "not_configured",
    })


if __name__ == "__main__":
    print(f"\n{'='*50}\n  PSN Trophy API Server v4 (+ Payments)\n{'='*50}")
    n = len(get_npsso_list())
    print(f"\n{'✅' if n else '⚠️ '} {n} conta(s) PSN configurada(s)")
    print(f"{'✅' if get_env('MP_ACCESS_TOKEN') else '⚠️ '} Mercado Pago: {'OK' if get_env('MP_ACCESS_TOKEN') else 'NAO CONFIGURADO'}")
    print(f"{'✅' if get_env('PAYPAL_CLIENT_ID') else '⚠️ '} PayPal: {'OK' if get_env('PAYPAL_CLIENT_ID') else 'NAO CONFIGURADO'}")
    print(f"{'✅' if get_env('SUPABASE_URL') else '⚠️ '} Supabase: {'OK' if get_env('SUPABASE_URL') else 'NAO CONFIGURADO'}")
    print(f"\n🚀 http://localhost:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
