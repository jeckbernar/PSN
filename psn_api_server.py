"""
PSN Trophy API Server
=====================
Busca os trofeus DESBLOQUEADOS de um usuario para um jogo especifico.
Retorna apenas os trofeus que o usuario ganhou, com trophy_id e data.
Opcionalmente recalcula as datas para uma data final desejada.

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
    """
    Tenta extrair a data de desbloqueio do objeto trofeu da psnawp.
    Retorna datetime ou None se nao ganho.
    """
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


def recalculate_dates(earned_trophies, final_date):
    """
    Recalcula as datas dos trofeus mantendo proporcao de intervalos.
    O trofeu mais recente (ou platina) recebe a data final exata.
    earned_trophies: lista de dicts com trophy_id, trophy_name, trophy_type, earned_date
    final_date: datetime da data final desejada
    """
    if not earned_trophies:
        return []

    # Separar platina (se houver) dos demais
    platinum = [t for t in earned_trophies if t["trophy_type"].upper() == "PLATINUM"]
    others   = [t for t in earned_trophies if t["trophy_type"].upper() != "PLATINUM"]

    # Ordenar outros por data crescente
    others_sorted = sorted(others, key=lambda x: x["earned_date"])

    # O ultimo da lista recebe a data final (platina ou ultimo trofeu)
    if platinum:
        anchor = platinum[0]
        to_recalc = others_sorted
    else:
        anchor = others_sorted[-1] if others_sorted else None
        to_recalc = others_sorted[:-1] if len(others_sorted) > 1 else []

    if anchor is None:
        return []

    # Data maxima dos trofeus a recalcular (referencia para proporcao)
    if to_recalc:
        max_date = max(t["earned_date"] for t in to_recalc)
    else:
        max_date = final_date

    result = []
    for t in to_recalc:
        diff_sec = int((max_date - t["earned_date"]).total_seconds())
        new_date = final_date - timedelta(seconds=diff_sec)
        result.append({**t, "new_date": new_date, "diff_sec": diff_sec})

    # Anchor (platina ou ultimo) recebe a data final exata
    result.append({**anchor, "new_date": final_date, "diff_sec": 0})

    return result


def try_fetch_trophies(npsso, psn_username, np_comm_id, platform):
    """
    Tenta buscar os trofeus de um usuario para um jogo.
    Retorna (lista_trofeus, erro_tipo, erro_msg)
    erro_tipo: None | "expired" | "not_found" | "private" | "error"
    """
    try:
        from psnawp_api import PSNAWP
        psnawp = PSNAWP(npsso)
    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ["npsso", "auth", "401", "token"]):
            return None, "expired", str(e)
        return None, "error", str(e)

    try:
        user = psnawp.user(online_id=psn_username)
        _ = user.online_id
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "404" in msg:
            return None, "not_found", "Perfil nao encontrado."
        if "private" in msg or "403" in msg or "forbidden" in msg:
            return None, "private", "Perfil privado."
        if any(k in msg for k in ["401", "unauthorized", "token"]):
            return None, "expired", str(e)
        return None, "error", f"Erro ao buscar perfil: {e}"

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
        if any(k in msg for k in ["401", "unauthorized", "token"]):
            return None, "expired", str(e)
        return None, "error", f"Erro ao buscar trofeus: {e}"


@app.route("/api/trophies", methods=["POST"])
def get_trophies():
    data = request.get_json()

    psn_username   = (data.get("psn_username") or "").strip()
    np_comm_id     = (data.get("np_comm_id")   or "").strip()
    platform_str   = (data.get("platform")     or "PS3").strip().upper()
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
            return jsonify({"status": "error", "message": "Formato de data invalido. Use YYYY-MM-DD HH:MM:SS"}), 400

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
    platform = platform_map.get(platform_str, PlatformType.PS3)

    # Tenta cada conta em sequencia
    trophies_raw  = None
    last_err_type = "error"
    last_err_msg  = "Todas as contas falharam."

    for i, npsso in enumerate(NPSSO_LIST, 1):
        log.info(f"Conta {i}/{len(NPSSO_LIST)}: {psn_username} / {np_comm_id}")
        raw, err_type, err_msg = try_fetch_trophies(npsso, psn_username, np_comm_id, platform)

        if err_type is None:
            trophies_raw = raw
            log.info(f"Conta {i} OK — {len(raw)} trofeus totais")
            break

        last_err_type = err_type
        last_err_msg  = err_msg
        if err_type in ("not_found", "private"):
            break
        log.warning(f"Conta {i} falhou ({err_type}), tentando proxima...")

    if trophies_raw is None:
        http_map = {"not_found": 404, "private": 403, "expired": 503, "error": 500}
        if last_err_type == "expired":
            last_err_msg = "Todas as contas PSN estao com token expirado."
        return jsonify({"status": last_err_type, "message": last_err_msg}), http_map.get(last_err_type, 500)

    # ── Processar: filtrar apenas os trofeus desbloqueados ──
    earned_list = []
    for t in trophies_raw:
        earned_dt = get_earned_date(t)
        if earned_dt is None:
            # Nao ganho — pular
            continue

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
        log.info(f"  Ganho: id={trophy_id} type={trophy_type} date={earned_dt}")

    log.info(f"Total desbloqueados: {len(earned_list)} de {len(trophies_raw)}")

    if not earned_list:
        return jsonify({
            "status":  "no_trophies",
            "message": "Nenhum trofeu desbloqueado encontrado.",
        }), 200

    # Recalcular datas (se data final foi fornecida) ou usar originais
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


@app.route("/api/health", methods=["GET"])
def health():
    npsso_list = get_npsso_list()
    return jsonify({
        "status":              "ok",
        "accounts_configured": len(npsso_list),
    })


if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  PSN Trophy API Server")
    print("=" * 50)
    npsso_list = get_npsso_list()
    if not npsso_list:
        print("\n⚠️  Nenhum NPSSO configurado!")
    else:
        print(f"\n✅ {len(npsso_list)} conta(s) PSN configurada(s)")
    print(f"🚀 Rodando em http://localhost:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
