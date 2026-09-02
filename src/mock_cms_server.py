from __future__ import annotations
from datetime import datetime, timezone

from flask import Flask, request, jsonify

app = Flask(__name__)
RECEIVED: list[dict] = []


@app.route("/cms/records", methods=["POST"])
def receive_record():
    payload = request.get_json(force=True, silent=True)
    if payload is None:
        return jsonify({"error": "invalid JSON"}), 400

    received_at = datetime.now(timezone.utc).isoformat()
    RECEIVED.append({"received_at": received_at, "payload": payload})
    print(f"[mock-cms] received record {payload.get('external_id')} "
          f"({payload.get('classification')}/{payload.get('recommended_action')})")
    return jsonify({"status": "stored", "external_id": payload.get("external_id")}), 201


@app.route("/cms/records", methods=["GET"])
def list_records():
    return jsonify({"count": len(RECEIVED), "records": RECEIVED})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
