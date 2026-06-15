from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/health")
@app.route("/healthz")
def health():
    return jsonify({"status": "ok", "bot": "CineIndexBot"})


@app.route("/")
def index():
    return (
        "<h2>CineIndexBot is running ✅</h2>"
        "<p>No SESSION required — index-based search bot.</p>"
        "<p><a href='/healthz'>/healthz</a></p>"
    ), 200
