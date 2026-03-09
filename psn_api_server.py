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


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status":              "ok",
        "accounts_configured": len(get_npsso_list()),
    })


if __name__ == "__main__":
    print(f"\n{'='*50}\n  PSN Trophy API Server v3\n{'='*50}")
    n = len(get_npsso_list())
    print(f"\n{'✅' if n else '⚠️ '} {n} conta(s) PSN configurada(s)")
    print(f"🚀 http://localhost:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
