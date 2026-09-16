"""Flask web demo for The Adaptive Agent.

Thin HTTP wrapper around the real engine in adaptive_agent.py: no logic is
duplicated here, every run is computed by the same run_static /
run_adaptive functions exercised by --selftest. This is the entrypoint
Vercel's Python runtime deploys (see vercel.json).

Local run:
    pip install -r requirements.txt
    python app.py
    open http://localhost:5000
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Dict

from flask import Flask, jsonify, request, send_from_directory

from adaptive_agent import SCENARIOS, run_adaptive, run_static

BASE_DIR = Path(__file__).parent
WEB_DIR = BASE_DIR / "web"

app = Flask(__name__, static_folder=None)


@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename: str):
    return send_from_directory(WEB_DIR, filename)


@app.get("/api/scenarios")
def api_scenarios():
    return jsonify([
        {"id": s.id, "title": s.title, "summary": s.summary, "ticks": s.ticks}
        for s in SCENARIOS.values()
    ])


@app.get("/api/run")
def api_run():
    scenario_id = request.args.get("scenario", "")
    agent = request.args.get("agent", "adaptive")
    hysteresis = request.args.get("hysteresis", "1") != "0"

    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        available = ", ".join(sorted(SCENARIOS))
        return jsonify({"error": f"unknown scenario '{scenario_id}', available: {available}"}), 404

    # journal=None: a serverless function has no durable disk between
    # requests, so persistence is demonstrated by the CLI (--journal-dir)
    # and by test_persistence_round_trips_state in --selftest instead.
    if agent == "static":
        result = run_static(scenario)
    else:
        result = run_adaptive(scenario, hysteresis=hysteresis)

    return jsonify(_serialize_result(result))


def _serialize_result(result) -> Dict[str, Any]:
    return {
        "scenarioId": result.scenario_id,
        "agent": result.agent_name,
        "outcome": result.outcome,
        "finalState": dataclasses.asdict(result.final_state),
        "trace": [dataclasses.asdict(entry) for entry in result.trace],
        "revisions": [
            {
                "tick": r.tick,
                "fromVersion": r.from_version,
                "toVersion": r.to_version,
                "reason": r.reason,
                "newSteps": [dataclasses.asdict(s) for s in r.new_steps],
            }
            for r in result.revisions
        ],
    }


if __name__ == "__main__":
    app.run(debug=True, port=5000)
