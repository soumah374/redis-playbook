#!/usr/bin/env python3
# =============================================================================
# Chat temps réel — Redis HA + Sentinel — AlmaLinux 9
# Usage : python3 app.py   puis ouvrir http://localhost:5000
# Prérequis : pip install -r requirements.txt
#
# Architecture :
#   Navigateur ──HTTP POST──> Flask ──PUBLISH──> Redis (master via Sentinel)
#   Navigateur <───SSE─────── Flask <──SUBSCRIBE── Redis
# Les messages transitent par Redis Pub/Sub ; l'historique récent est conservé
# dans une liste Redis (LPUSH/LTRIM) pour les nouveaux arrivants.
# =============================================================================

import json
import time

from flask import Flask, Response, render_template, request, stream_with_context
import redis
from redis.sentinel import Sentinel

# ─── Configuration (alignée sur main.py / inventory.ini) ──────────────────────
SENTINELS = [
    ("192.168.1.158", 26379),  # master
    ("192.168.1.140", 26379),  # replica1
    ("192.168.1.159", 26379),  # replica2
]
MASTER_NAME    = "computemaster"
REDIS_PASSWORD = "Securep@55Here"

CHANNEL_PREFIX = "chat:channel:"   # canal Pub/Sub par salon
HISTORY_PREFIX = "chat:history:"   # liste d'historique par salon
HISTORY_MAX    = 50                # nb de messages conservés par salon

app = Flask(__name__)

# Connexion master partagée pour PUBLISH / historique (le pool gère le reste).
_sentinel = Sentinel(SENTINELS, socket_timeout=5, password=REDIS_PASSWORD)


def master():
    return _sentinel.master_for(MASTER_NAME, socket_timeout=5, password=REDIS_PASSWORD)


def channel(room):
    return f"{CHANNEL_PREFIX}{room}"


def history_key(room):
    return f"{HISTORY_PREFIX}{room}"


# ─── Pages ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    room = request.args.get("room", "general")
    return render_template("index.html", room=room)


# ─── Historique récent du salon ──────────────────────────────────────────────
@app.route("/history")
def history():
    room = request.args.get("room", "general")
    raw = master().lrange(history_key(room), 0, HISTORY_MAX - 1)
    # Stockés du plus récent au plus ancien → on inverse pour l'affichage.
    messages = [json.loads(m) for m in reversed(raw)]
    return {"messages": messages}


# ─── Envoi d'un message ──────────────────────────────────────────────────────
@app.route("/send", methods=["POST"])
def send():
    data = request.get_json(force=True)
    user = (data.get("user") or "anon").strip()[:32]
    text = (data.get("text") or "").strip()[:500]
    room = (data.get("room") or "general").strip()[:64]
    if not text:
        return {"ok": False, "error": "message vide"}, 400

    msg = json.dumps({"user": user, "text": text, "ts": time.time()})
    r = master()
    pipe = r.pipeline()
    pipe.lpush(history_key(room), msg)        # ajoute en tête
    pipe.ltrim(history_key(room), 0, HISTORY_MAX - 1)  # borne l'historique
    pipe.publish(channel(room), msg)          # diffuse en temps réel
    pipe.execute()
    return {"ok": True}


# ─── Flux temps réel (Server-Sent Events) ────────────────────────────────────
@app.route("/stream")
def stream():
    room = request.args.get("room", "general")

    def event_stream():
        # Chaque client SSE a son propre pubsub/connexion dédiée.
        pubsub = master().pubsub()
        pubsub.subscribe(channel(room))
        try:
            # Commentaire SSE initial : ouvre le flux côté navigateur.
            yield ": connecté\n\n"
            last_ping = time.time()
            for message in pubsub.listen():
                if message["type"] == "message":
                    payload = message["data"]
                    if isinstance(payload, bytes):
                        payload = payload.decode()
                    yield f"data: {payload}\n\n"
                # Keep-alive périodique pour éviter les coupures de proxy.
                if time.time() - last_ping > 15:
                    yield ": keep-alive\n\n"
                    last_ping = time.time()
        finally:
            pubsub.close()

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",   # désactive le buffering nginx si présent
    }
    return Response(stream_with_context(event_stream()),
                    mimetype="text/event-stream", headers=headers)


if __name__ == "__main__":
    # threaded=True : nécessaire pour servir plusieurs flux SSE simultanés.
    app.run(host="0.0.0.0", port=5003, threaded=True, debug=False)
