"""Flask application for Inventory Overview."""

import logging
import os
import secrets
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_login import login_required

from auth import auth_bp, login_manager
from config import load_config, save_config, is_odoo_configured, DATA_DIR
from bom_cache import BomCache
from odoo_client import OdooClient
from bom_service import BomService

# ── Logging ──────────────────────────────────────────────────────────

DATA_DIR.mkdir(parents=True, exist_ok=True)
log_file = DATA_DIR / "inventory.log"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(file_handler)

logger = logging.getLogger(__name__)

# ── Flask app ────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)

url_prefix = os.getenv("URL_PREFIX", "").rstrip("/")

login_manager.init_app(app)
app.register_blueprint(auth_bp)

# WSGI middleware: set SCRIPT_NAME so Flask generates correct URLs behind reverse proxy
if url_prefix:
    _inner_wsgi = app.wsgi_app
    def _prefix_wsgi(environ, start_response):
        environ["SCRIPT_NAME"] = url_prefix + environ.get("SCRIPT_NAME", "")
        return _inner_wsgi(environ, start_response)
    app.wsgi_app = _prefix_wsgi

# ── Caches ──────────────────────────────────────────────────────────

_assembly_cache: dict = {"data": None, "expires": 0}
_CACHE_TTL = 300  # 5 minutes
bom_cache = BomCache()


def _get_cached_assemblies(odoo: OdooClient) -> list[dict]:
    now = time.time()
    if _assembly_cache["data"] is not None and now < _assembly_cache["expires"]:
        return _assembly_cache["data"]

    assemblies = odoo.get_assemblies_with_boms()
    _assembly_cache["data"] = assemblies
    _assembly_cache["expires"] = now + _CACHE_TTL
    logger.info("Refreshed assembly cache: %d assemblies", len(assemblies))
    return assemblies


# ── Helpers ──────────────────────────────────────────────────────────

def build_odoo() -> OdooClient:
    cfg = load_config()
    if not is_odoo_configured(cfg):
        raise ValueError("Odoo not configured. Set credentials in .env or data/config.json")
    o = cfg["odoo"]
    client = OdooClient(o["url"], o["db"], o["user"], o["password"])
    client.authenticate()
    return client


def _check_csrf():
    """Verify X-Requested-With header for CSRF protection on API POST routes."""
    if request.method == "POST":
        if request.headers.get("X-Requested-With") != "XMLHttpRequest":
            return jsonify({"error": "Missing CSRF header"}), 403
    return None


# ── Security headers ─────────────────────────────────────────────────

@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ── Page routes ──────────────────────────────────────────────────────

@app.route("/")
@login_required
def page_overview():
    cfg = load_config()
    odoo_url = cfg["odoo"].get("url", "").rstrip("/")
    return render_template("overview.html", odoo_url=odoo_url)


@app.route("/settings")
@login_required
def page_settings():
    return render_template("settings.html")


# ── API routes ───────────────────────────────────────────────────────

@app.route("/api/config")
@login_required
def api_config_get():
    """Get current config (password masked)."""
    cfg = load_config()
    odoo = cfg.get("odoo", {})
    return jsonify({
        "odoo": {
            "url": odoo.get("url", ""),
            "db": odoo.get("db", ""),
            "user": odoo.get("user", ""),
            "password": "••••••••" if odoo.get("password") else "",
        }
    })


@app.route("/api/config", methods=["POST"])
@login_required
def api_config_save():
    """Save Odoo config."""
    csrf_err = _check_csrf()
    if csrf_err:
        return csrf_err

    data = request.get_json()
    if not data or "odoo" not in data:
        return jsonify({"error": "Invalid config data"}), 400

    cfg = load_config()
    odoo_data = data["odoo"]

    for key in ("url", "db", "user"):
        if key in odoo_data:
            cfg["odoo"][key] = odoo_data[key].strip()

    # Only update password if it was actually changed (not the masked placeholder)
    if "password" in odoo_data and odoo_data["password"] and not odoo_data["password"].startswith("••"):
        cfg["odoo"]["password"] = odoo_data["password"]

    save_config(cfg)
    _assembly_cache["data"] = None
    bom_cache.invalidate_all()
    return jsonify({"status": "ok"})

@app.route("/api/assemblies")
@login_required
def api_assemblies():
    """List all products that have a BOM defined."""
    try:
        odoo = build_odoo()
        assemblies = _get_cached_assemblies(odoo)
        return jsonify({"assemblies": assemblies})
    except Exception as e:
        logger.exception("Failed to fetch assemblies")
        return jsonify({"error": str(e)}), 500


@app.route("/api/check", methods=["POST"])
@login_required
def api_check():
    """Check component availability for a given assembly and quantity."""
    csrf_err = _check_csrf()
    if csrf_err:
        return csrf_err

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    template_id = data.get("template_id")
    qty = data.get("qty", 1)

    if not template_id:
        return jsonify({"error": "template_id is required"}), 400
    try:
        qty = float(qty)
        if qty <= 0:
            return jsonify({"error": "qty must be positive"}), 400
    except (TypeError, ValueError):
        return jsonify({"error": "qty must be a number"}), 400

    try:
        odoo = build_odoo()
        service = BomService(odoo, bom_cache=bom_cache)
        result = service.check_availability(int(template_id), qty)
        return jsonify(result)
    except Exception as e:
        logger.exception("Failed to check availability")
        return jsonify({"error": str(e)}), 500


@app.route("/api/max", methods=["POST"])
@login_required
def api_max():
    """Calculate maximum producible quantity for an assembly."""
    csrf_err = _check_csrf()
    if csrf_err:
        return csrf_err

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    template_id = data.get("template_id")
    if not template_id:
        return jsonify({"error": "template_id is required"}), 400

    try:
        odoo = build_odoo()
        service = BomService(odoo, bom_cache=bom_cache)
        result = service.calculate_max_producible(int(template_id))
        return jsonify(result)
    except Exception as e:
        logger.exception("Failed to calculate max producible")
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
@login_required
def api_health():
    """Test Odoo connection."""
    try:
        odoo = build_odoo()
        version = odoo.get_server_version()
        return jsonify({"status": "ok", "odoo_version": version})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/api/cache/info")
@login_required
def api_cache_info():
    return jsonify(bom_cache.get_info())


@app.route("/api/cache/invalidate", methods=["POST"])
@login_required
def api_cache_invalidate():
    csrf_err = _check_csrf()
    if csrf_err:
        return csrf_err
    bom_cache.invalidate_all()
    _assembly_cache["data"] = None
    return jsonify({"status": "ok"})
