"""
PSN Trophy API Server — Multi-Account Fallback
================================================
INSTALACAO:
    pip install flask flask-cors "psnawp==1.3.3"

CONFIGURACAO (variaveis de ambiente no Railway):
    PSN_NPSSO_1=seu_npsso_conta1
    PSN_NPSSO_2=seu_npsso_conta2
    PSN_NPSSO_3=seu_npsso_conta3

OU edite diretamente as variaveis abaixo para testes locais.

COMO OBTER O NPSSO:
    1. Acesse https://www.playstation.com e faca login
    2. Acesse https://ca.account.sony.com/api/v1/ssocookie
    3. Copie APENAS o valor do campo "npsso" (64 chars, sem aspas)

ENDPOINTS:
    POST /api/trophies   → busca e recalcula datas
    GET  /api/health     → status do servidor e contas
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timedelta
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
#  CONFIGURACAO — edite aqui para testes locais
#  Em producao (Railway) use variaveis de ambiente
# ══════════════════════════════════════════════════════════
PORT = int(os.environ.get("PORT", 5000))
# ══════════════════════════════════════════════════════════

def get_npsso_list():
    """Le os NSSOs dinamicamente a cada requisicao para garantir que
    as variaveis de ambiente do Railway sejam sempre lidas corretamente."""
    candidates = [
        os.environ.get("PSN_NPSSO_1", ""),
        os.environ.get("PSN_NPSSO_2", ""),
        os.environ.get("PSN_NPSSO_3", ""),
    ]
    return [n.strip() for n in candidates if n.strip()]

app = Flask(__name__)
CORS(app)


# ── Helpers ───────────────────────────────────────────────

def fmt_dt(dt):
    if dt is None:
        return None
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def recalculate(sorted_dates, final_date):
    """
    Recalcula datas mantendo intervalos proporcionais.
    Ultimo trofeu (platina) recebe a data final exata.
    """
    to_use = sorted_dates[:-1]
    last   = sorted_dates[-1]

    if not to_use:
        return [{**last, "new_date": final_date, "diff_sec": 0}]

    max_date = max(t["earned_date"] for t in to_use if t["earned_date"])
    result = []
    for t in to_use:
        if t["earned_date"]:
            diff_sec = int((max_date - t["earned_date"]).total_seconds())
            new_date = final_date - timedelta(seconds=diff_sec)
        else:
            diff_sec = None
            new_date = None
        result.append({**t, "new_date": new_date, "diff_sec": diff_sec})

    # platina com data final exata
    result.append({**last, "new_date": final_date, "diff_sec": 0})
    return result


def sort_key(t):
    """Platina primeiro, depois por trophy_id crescente."""
    is_plat = t["trophy_type"].upper() == "PLATINUM"
    return (0 if is_plat else 1, t["trophy_id"])


def try_fetch_trophies(npsso, psn_username, np_comm_id, platform):
    """
    Tenta buscar trofeus usando um NPSSO especifico.
    Retorna (trophies_raw, error_type, error_msg)
    error_type: None | "expired" | "not_found" | "private" | "error"
    """
    try:
        from psnawp_api import PSNAWP
        psnawp = PSNAWP(npsso)
    except Exception as e:
        msg = str(e).lower()
        if "npsso" in msg or "auth" in msg or "401" in msg or "token" in msg:
            log.warning(f"NPSSO expirado ou invalido: {e}")
            return None, "expired", str(e)
        log.error(f"Erro ao inicializar PSNAWP: {e}")
        return None, "error", str(e)

    # busca usuario
    try:
        user = psnawp.user(online_id=psn_username)
        _ = user.online_id
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "404" in msg:
            return None, "not_found", "Perfil nao encontrado. Verifique o PSN ID."
        if "private" in msg or "403" in msg or "forbidden" in msg:
            return None, "private", "Perfil privado. O usuario precisa tornar os trofeus publicos nas configuracoes da PSN."
        if "401" in msg or "unauthorized" in msg or "token" in msg:
            log.warning(f"NPSSO expirado ao buscar usuario: {e}")
            return None, "expired", str(e)
        log.error(f"Erro ao buscar usuario {psn_username}: {e}")
        return None, "error", f"Erro ao buscar perfil: {str(e)}"

    # busca trofeus
    try:
        trophies_raw = list(user.trophies(
            np_communication_id=np_comm_id,
            platform=platform,
            include_metadata=True,
        ))
        return trophies_raw, None, None
    except Exception as e:
        msg = str(e).lower()
        if "private" in msg or "403" in msg:
            return None, "private", "Lista de trofeus privada."
        if "not found" in msg or "404" in msg:
            return None, "not_found", "Jogo nao encontrado neste perfil."
        if "401" in msg or "unauthorized" in msg or "token" in msg:
            log.warning(f"NPSSO expirado ao buscar trofeus: {e}")
            return None, "expired", str(e)
        log.error(f"Erro ao buscar trofeus: {e}")
        return None, "error", f"Erro ao buscar trofeus: {str(e)}"


# ── Endpoint principal ─────────────────────────────────────

@app.route("/api/trophies", methods=["POST"])
def get_trophies():
    data = request.get_json()

    psn_username   = (data.get("psn_username") or "").strip()
    np_comm_id     = (data.get("np_comm_id")   or "").strip()
    platform_str   = (data.get("platform")     or "PS3").strip().upper()
    final_date_str = (data.get("final_date")   or "").strip()

    # validacoes
    if not psn_username:
        return jsonify({"status": "error", "message": "PSN username obrigatorio"}), 400
    if not np_comm_id:
        return jsonify({"status": "error", "message": "NP_COMM_ID obrigatorio"}), 400
    if not final_date_str:
        return jsonify({"status": "error", "message": "Data final obrigatoria"}), 400
    NPSSO_LIST = get_npsso_list()
    if not NPSSO_LIST:
        return jsonify({"status": "error", "message": "Nenhum NPSSO configurado no servidor"}), 500

    try:
        final_date = datetime.strptime(final_date_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return jsonify({"status": "error", "message": "Formato de data invalido. Use YYYY-MM-DD HH:MM:SS"}), 400

    try:
        from psnawp_api.models.trophies.trophy_constants import PlatformType
    except ImportError:
        return jsonify({"status": "error", "message": "psnawp nao instalado no servidor"}), 500

    platform_map = {
        "PS3":    PlatformType.PS3,
        "PS4":    PlatformType.PS4,
        "PS5":    PlatformType.PS5,
        "VITA":   PlatformType.PS_VITA,
        "PSVITA": PlatformType.PS_VITA,
    }
    platform = platform_map.get(platform_str, PlatformType.PS3)

    # ── Tenta cada conta em sequencia ──────────────────────
    trophies_raw  = None
    last_err_type = "error"
    last_err_msg  = "Todas as contas PSN falharam."

    for i, npsso in enumerate(NPSSO_LIST, 1):
        log.info(f"Tentando conta {i}/{len(NPSSO_LIST)} para {psn_username} / {np_comm_id}")
        raw, err_type, err_msg = try_fetch_trophies(npsso, psn_username, np_comm_id, platform)

        if err_type is None:
            # sucesso
            trophies_raw = raw
            log.info(f"Conta {i} funcionou — {len(raw)} trofeus")
            break

        last_err_type = err_type
        last_err_msg  = err_msg

        if err_type in ("not_found", "private"):
            # Erros que nao dependem da conta — nao adianta tentar as proximas
            log.info(f"Erro definitivo ({err_type}), nao tentando outras contas")
            break

        # err_type == "expired" ou "error" → tenta proxima conta
        log.warning(f"Conta {i} falhou ({err_type}), tentando proxima...")

    # nenhuma conta funcionou
    if trophies_raw is None:
        status_map = {
            "not_found": 404,
            "private":   403,
            "expired":   503,
            "error":     500,
        }
        http_code = status_map.get(last_err_type, 500)
        if last_err_type == "expired":
            last_err_msg = "Todas as contas PSN estao com token expirado. Contate o administrador."
        return jsonify({"status": last_err_type, "message": last_err_msg}), http_code

    # ── Processa trofeus ───────────────────────────────────
    trophy_list = []
    for t in trophies_raw:
        earned_dt = (
            getattr(t, "earned_date_time",   None) or
            getattr(t, "earned_datetime",    None) or
            getattr(t, "trophy_earned_date", None)
        )
        if earned_dt and hasattr(earned_dt, "tzinfo") and earned_dt.tzinfo:
            earned_dt = earned_dt.astimezone().replace(tzinfo=None)

        raw_type    = getattr(t, "trophy_type", None)
        trophy_type = raw_type.name if hasattr(raw_type, "name") else str(raw_type or "?")

        trophy_list.append({
            "trophy_id":   getattr(t, "trophy_id", None),
            "trophy_name": getattr(t, "trophy_name", None) or f"#{getattr(t, 'trophy_id', '?')}",
            "trophy_type": trophy_type,
            "earned_date": earned_dt,
        })

    earned = [t for t in trophy_list if t["earned_date"]]

    if not earned:
        return jsonify({
            "status":  "not_found",
            "message": "Nenhum trofeu desbloqueado encontrado neste perfil para este jogo."
        }), 404

    # recalcula e ordena
    earned_sorted = sorted(earned, key=lambda x: x["earned_date"])
    recalculated  = recalculate(earned_sorted, final_date)
    final_list    = sorted(recalculated, key=sort_key)

    trophies_out = [{
        "trophy_id":     t["trophy_id"],
        "trophy_name":   t["trophy_name"],
        "trophy_type":   t["trophy_type"],
        "new_date":      fmt_dt(t["new_date"]),
        "original_date": fmt_dt(t["earned_date"]),
        "diff_sec":      t.get("diff_sec"),
    } for t in final_list]

    return jsonify({
        "status":   "ok",
        "trophies": trophies_out,
        "total":    len(trophies_out),
    })


# ── Health check ──────────────────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    npsso_list = get_npsso_list()
    # debug: mostra quais variaveis existem no ambiente
    env_keys = [k for k in os.environ.keys() if "NPSSO" in k or "PSN" in k]
    return jsonify({
        "status":              "ok",
        "accounts_configured": len(npsso_list),
        "accounts":            [f"conta_{i+1}: configurada" for i in range(len(npsso_list))],
        "env_vars_found":      env_keys,
    })


# ── Start ─────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  PSN Trophy API Server")
    print("=" * 50)
    if not NPSSO_LIST:
        print("\n⚠️  Nenhum NPSSO configurado!")
        print("   Defina PSN_NPSSO_1, PSN_NPSSO_2, PSN_NPSSO_3")
        print("   como variaveis de ambiente, ou edite NPSSO_LIST\n")
    else:
        print(f"\n✅ {len(NPSSO_LIST)} conta(s) PSN configurada(s)")
    print(f"🚀 Rodando em http://localhost:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
