"""Odoo ERP XML-RPC client (read-only).

Provides helpers for reading products, BOMs, and inventory data.
"""

import logging
import xmlrpc.client

logger = logging.getLogger(__name__)


class OdooClient:
    """Read-only client for Odoo's XML-RPC External API."""

    def __init__(self, url: str, db: str, user: str, password: str):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.password = password
        self._uid: int | None = None
        self._models: xmlrpc.client.ServerProxy | None = None

    def authenticate(self) -> int:
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        self._uid = common.authenticate(self.db, self.user, self.password, {})
        if not self._uid:
            raise ValueError("Odoo authentication failed: invalid credentials")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)
        logger.info("Odoo: authenticated uid=%d", self._uid)
        return self._uid

    def _ensure_auth(self) -> None:
        if not self._uid or not self._models:
            self.authenticate()

    def execute(self, model: str, method: str, args: list, kwargs: dict | None = None):
        self._ensure_auth()
        return self._models.execute_kw(self.db, self._uid, self.password, model, method, args, kwargs or {})

    # ── Products ─────────────────────────────────────────────────────

    def get_product_variant_id(self, template_id: int) -> int | None:
        ids = self.execute("product.product", "search", [[["product_tmpl_id", "=", template_id]]])
        return ids[0] if ids else None

    def get_product_template_for_variant(self, variant_id: int) -> int | None:
        """Get template ID from a product.product (variant) ID."""
        data = self.execute("product.product", "read", [[variant_id], ["product_tmpl_id"]])
        if data and data[0].get("product_tmpl_id"):
            tmpl = data[0]["product_tmpl_id"]
            return tmpl[0] if isinstance(tmpl, (list, tuple)) else tmpl
        return None

    def get_product_template_info(self, template_id: int) -> dict | None:
        """Read template with sale_ok, purchase_ok, default_code, name."""
        data = self.execute("product.template", "read",
                            [[template_id], ["id", "name", "default_code", "sale_ok", "purchase_ok"]])
        return data[0] if data else None

    def batch_get_product_template_info(self, template_ids: list[int]) -> dict[int, dict]:
        """Batch-read template info. Returns {template_id: {id, name, default_code, sale_ok, purchase_ok}}."""
        if not template_ids:
            return {}
        data = self.execute("product.template", "read",
                            [template_ids, ["id", "name", "default_code", "sale_ok", "purchase_ok"]])
        return {d["id"]: d for d in data} if data else {}

    # ── BOMs ─────────────────────────────────────────────────────────

    def get_assemblies_with_boms(self) -> list[dict]:
        """Get all products that have a BOM defined.

        Returns list of {id, name, default_code} for each product template
        that has at least one mrp.bom record.
        """
        bom_ids = self.execute("mrp.bom", "search", [[]])
        if not bom_ids:
            return []

        boms = self.execute("mrp.bom", "read", [bom_ids, ["product_tmpl_id"]])
        template_ids = list({
            b["product_tmpl_id"][0] if isinstance(b["product_tmpl_id"], (list, tuple))
            else b["product_tmpl_id"]
            for b in boms if b.get("product_tmpl_id")
        })

        if not template_ids:
            return []

        templates = self.execute("product.template", "read",
                                 [template_ids, ["id", "name", "default_code"]])
        return sorted(templates, key=lambda t: t.get("default_code") or t.get("name") or "")

    def find_bom_by_product(self, template_id: int) -> int | None:
        ids = self.execute("mrp.bom", "search", [[["product_tmpl_id", "=", template_id]]])
        return ids[0] if ids else None

    def get_bom_lines(self, bom_id: int) -> list[dict]:
        """Get BOM lines with product and quantity info."""
        line_ids = self.execute("mrp.bom.line", "search", [[["bom_id", "=", bom_id]]])
        if not line_ids:
            return []
        return self.execute("mrp.bom.line", "read", [line_ids, ["product_id", "product_qty"]])

    # ── Warehouse & Inventory ────────────────────────────────────────

    def get_warehouse_location_id(self, warehouse_name: str) -> int | None:
        """Find the root stock location for a warehouse by name.

        Tries matching by warehouse name first, then by warehouse code.
        Returns the lot_stock_id (main stock location) integer ID.
        """
        for field in ["name", "code"]:
            ids = self.execute("stock.warehouse", "search", [[[field, "=", warehouse_name]]])
            if ids:
                data = self.execute("stock.warehouse", "read", [ids[:1], ["lot_stock_id"]])
                if data and data[0].get("lot_stock_id"):
                    loc = data[0]["lot_stock_id"]
                    loc_id = loc[0] if isinstance(loc, (list, tuple)) else loc
                    logger.info("Warehouse '%s' → location_id=%d", warehouse_name, loc_id)
                    return loc_id
        return None

    def get_child_location_ids(self, parent_location_id: int) -> list[int]:
        """Recursively find all internal child locations under a parent.

        Returns the full list including the parent itself.
        """
        all_ids = [parent_location_id]
        to_check = [parent_location_id]

        while to_check:
            children = self.execute("stock.location", "search", [
                [["location_id", "in", to_check], ["usage", "=", "internal"]]
            ])
            if not children:
                break
            new_children = [c for c in children if c not in all_ids]
            if not new_children:
                break
            all_ids.extend(new_children)
            to_check = new_children

        logger.info("Location %d has %d total locations (incl. children)", parent_location_id, len(all_ids))
        return all_ids

    def get_stock_quants(self, product_ids: list[int], location_ids: list[int]) -> list[dict]:
        """Get stock quantities for given products in given locations.

        Returns list of {product_id, quantity, reserved_quantity}.
        product_id is returned as [id, name] tuple from Odoo.
        """
        if not product_ids or not location_ids:
            return []

        domain = [
            ["product_id", "in", product_ids],
            ["location_id", "in", location_ids],
        ]
        quant_ids = self.execute("stock.quant", "search", [domain])
        if not quant_ids:
            return []

        return self.execute("stock.quant", "read",
                            [quant_ids, ["product_id", "quantity", "reserved_quantity"]])

    # ── Health ───────────────────────────────────────────────────────

    def get_server_version(self) -> str:
        common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
        info = common.version()
        return info.get("server_version", "unknown")
