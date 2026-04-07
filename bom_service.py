"""BOM explosion and inventory availability service.

Handles recursive BOM flattening, hierarchical BOM tree,
stock availability checks, and maximum producible quantity calculation.
"""

import copy
import logging
import math

from odoo_client import OdooClient

logger = logging.getLogger(__name__)


class BomService:
    """Explode BOMs and check component availability against warehouse stock."""

    def __init__(self, odoo: OdooClient, warehouse_name: str = "01WH", bom_cache=None):
        self.odoo = odoo
        self.warehouse_name = warehouse_name
        self._bom_cache = bom_cache
        self._location_ids: list[int] | None = None

    def _get_location_ids(self) -> list[int]:
        """Get (and cache) all location IDs for the warehouse."""
        if self._location_ids is None:
            root_id = self.odoo.get_warehouse_location_id(self.warehouse_name)
            if not root_id:
                raise ValueError(f"Warehouse '{self.warehouse_name}' not found in Odoo")
            self._location_ids = self.odoo.get_child_location_ids(root_id)
        return self._location_ids

    # ── Cached BOM structure ───────────────────────────────────────────

    def _get_bom_structure(self, template_id: int) -> tuple[list, list]:
        """Get BOM tree + flat list, from cache if available. Always at qty=1.0."""
        if self._bom_cache:
            cached = self._bom_cache.get_bom(template_id)
            if cached:
                logger.info("BOM cache hit for template_id=%d", template_id)
                return cached["tree"], cached["flat"]

        tree = self.explode_bom_tree(template_id, 1.0)
        flat = self.explode_bom(template_id, 1.0)

        if self._bom_cache:
            self._bom_cache.set_bom(template_id, tree, flat)

        return tree, flat

    @staticmethod
    def _scale_tree(tree: list, qty: float) -> list:
        """Deep-copy tree and scale qty_needed by qty."""
        scaled = copy.deepcopy(tree)
        BomService._scale_tree_nodes(scaled, qty)
        return scaled

    @staticmethod
    def _scale_tree_nodes(nodes: list, qty: float) -> None:
        for node in nodes:
            node["qty_needed"] = node.get("qty_needed", 0) * qty
            if node.get("children"):
                BomService._scale_tree_nodes(node["children"], qty)

    @staticmethod
    def _scale_flat(flat: list, qty: float) -> list:
        scaled = copy.deepcopy(flat)
        for comp in scaled:
            comp["qty_needed"] = comp.get("qty_needed", 0) * qty
        return scaled

    # ── Flat explosion (for max calculation) ─────────────────────────

    def explode_bom(self, template_id: int, qty: float = 1.0) -> list[dict]:
        """Recursively explode a BOM into a flat list of leaf components."""
        components: dict[int, dict] = {}
        self._explode_recursive(template_id, qty, components, visited=set())
        return list(components.values())

    def _explode_recursive(self, template_id: int, qty: float,
                           components: dict[int, dict], visited: set[int]) -> None:
        if template_id in visited:
            logger.warning("Circular BOM reference detected at template_id=%d, skipping", template_id)
            return
        visited.add(template_id)

        bom_id = self.odoo.find_bom_by_product(template_id)
        if not bom_id:
            return

        lines = self.odoo.get_bom_lines(bom_id)
        for line in lines:
            product_ref = line.get("product_id")
            if not product_ref:
                continue

            variant_id = product_ref[0] if isinstance(product_ref, (list, tuple)) else product_ref
            product_name = product_ref[1] if isinstance(product_ref, (list, tuple)) else ""
            line_qty = line.get("product_qty", 1.0) * qty

            child_template_id = self.odoo.get_product_template_for_variant(variant_id)
            if child_template_id and child_template_id not in visited:
                child_bom_id = self.odoo.find_bom_by_product(child_template_id)
                if child_bom_id:
                    self._explode_recursive(child_template_id, line_qty, components, visited.copy())
                    continue

            if variant_id in components:
                components[variant_id]["qty_needed"] += line_qty
            else:
                default_code = ""
                if child_template_id:
                    tmpl_data = self.odoo.execute("product.template", "read",
                                                  [[child_template_id], ["default_code"]])
                    if tmpl_data:
                        default_code = tmpl_data[0].get("default_code") or ""

                components[variant_id] = {
                    "variant_id": variant_id,
                    "template_id": child_template_id,
                    "product_name": product_name,
                    "default_code": default_code,
                    "qty_needed": line_qty,
                }

        visited.discard(template_id)

    # ── Hierarchical BOM tree ────────────────────────────────────────

    def explode_bom_tree(self, template_id: int, qty: float = 1.0) -> list[dict]:
        """Build a hierarchical BOM tree preserving sub-assembly structure.

        Returns a list of nodes. Each node:
        {
            variant_id, template_id, product_name, default_code,
            qty_per_assembly, qty_needed,
            is_assembly, purchase_ok, sale_ok,
            children: [...] (only for sub-assemblies)
        }
        """
        tree = []
        self._build_tree(template_id, qty, tree, visited=set())
        return tree

    def _build_tree(self, template_id: int, qty: float,
                    tree: list[dict], visited: set[int]) -> None:
        if template_id in visited:
            return
        visited.add(template_id)

        bom_id = self.odoo.find_bom_by_product(template_id)
        if not bom_id:
            return

        lines = self.odoo.get_bom_lines(bom_id)
        for line in lines:
            product_ref = line.get("product_id")
            if not product_ref:
                continue

            variant_id = product_ref[0] if isinstance(product_ref, (list, tuple)) else product_ref
            product_name = product_ref[1] if isinstance(product_ref, (list, tuple)) else ""
            line_qty_per = line.get("product_qty", 1.0)
            line_qty = line_qty_per * qty

            child_template_id = self.odoo.get_product_template_for_variant(variant_id)

            # Get template info for sale_ok/purchase_ok
            tmpl_info = None
            if child_template_id:
                tmpl_info = self.odoo.get_product_template_info(child_template_id)

            default_code = (tmpl_info or {}).get("default_code", "") or ""
            sale_ok = (tmpl_info or {}).get("sale_ok", False)
            purchase_ok = (tmpl_info or {}).get("purchase_ok", True)

            # Check if sub-assembly
            is_assembly = False
            children = []
            if child_template_id and child_template_id not in visited:
                child_bom_id = self.odoo.find_bom_by_product(child_template_id)
                if child_bom_id:
                    is_assembly = True
                    self._build_tree(child_template_id, line_qty, children, visited.copy())

            node = {
                "variant_id": variant_id,
                "template_id": child_template_id,
                "product_name": product_name,
                "default_code": default_code,
                "qty_per_assembly": line_qty_per,
                "qty_needed": line_qty,
                "is_assembly": is_assembly,
                "purchase_ok": purchase_ok,
                "sale_ok": sale_ok,
            }
            if is_assembly:
                node["children"] = children

            tree.append(node)

        visited.discard(template_id)

    # ── Availability check (now with tree + type info) ───────────────

    def check_availability(self, template_id: int, qty: float) -> dict:
        """Check component availability with hierarchical BOM tree.

        Returns {
            assembly, requested_qty,
            bom_tree: [...hierarchical nodes with availability...],
            flat_components: [...leaf components with availability...],
            all_available, short_count, max_producible,
            purchase_short_count, sale_short_count
        }
        """
        # Get assembly info
        tmpl_data = self.odoo.execute("product.template", "read",
                                      [[template_id], ["id", "name", "default_code"]])
        if not tmpl_data:
            raise ValueError(f"Product template {template_id} not found")
        assembly = tmpl_data[0]

        # Get BOM structure (cached at qty=1, scaled to requested qty)
        tree_base, flat_base = self._get_bom_structure(template_id)
        bom_tree = self._scale_tree(tree_base, qty)
        flat_components = self._scale_flat(flat_base, qty)

        if not flat_components:
            return {
                "assembly": assembly,
                "requested_qty": qty,
                "bom_tree": [],
                "flat_components": [],
                "all_available": True,
                "short_count": 0,
                "purchase_short_count": 0,
                "sale_short_count": 0,
                "max_producible": 0,
            }

        # Fetch stock for all components
        location_ids = self._get_location_ids()

        # Collect ALL variant_ids from tree (includes sub-assemblies too)
        all_variant_ids = set()
        self._collect_variant_ids(bom_tree, all_variant_ids)
        for c in flat_components:
            all_variant_ids.add(c["variant_id"])

        quants = self.odoo.get_stock_quants(list(all_variant_ids), location_ids)

        # Build available qty map
        available_map: dict[int, float] = {}
        for q in quants:
            vid = q["product_id"][0] if isinstance(q["product_id"], (list, tuple)) else q["product_id"]
            stock = q.get("quantity", 0.0) - q.get("reserved_quantity", 0.0)
            available_map[vid] = available_map.get(vid, 0.0) + stock

        # Fetch open purchase order lines
        po_map: dict[int, dict] = {}
        try:
            po_lines = self.odoo.get_open_po_lines(list(all_variant_ids))
            for line in po_lines:
                vid = line["product_id"][0] if isinstance(line["product_id"], (list, tuple)) else line["product_id"]
                remaining = (line.get("product_qty", 0) or 0) - (line.get("qty_received", 0) or 0)
                if remaining <= 0:
                    continue
                if vid not in po_map:
                    po_map[vid] = {"qty_ordered": 0.0, "earliest_arrival": None}
                po_map[vid]["qty_ordered"] += remaining
                arrival = line.get("date_planned")
                if arrival:
                    # date_planned can be string or datetime
                    arrival_str = str(arrival)[:10] if arrival else None
                    if arrival_str:
                        prev = po_map[vid]["earliest_arrival"]
                        if prev is None or arrival_str < prev:
                            po_map[vid]["earliest_arrival"] = arrival_str
        except Exception as e:
            logger.warning("Failed to fetch PO lines: %s", e)

        # Enrich tree with availability + PO data
        self._enrich_tree_availability(bom_tree, available_map, po_map)

        # Enrich flat components with availability + type info
        tmpl_ids = [c["template_id"] for c in flat_components if c.get("template_id")]
        tmpl_map = self.odoo.batch_get_product_template_info(tmpl_ids) if tmpl_ids else {}

        result_flat = []
        short_count = 0
        purchase_short = 0
        sale_short = 0
        for comp in flat_components:
            vid = comp["variant_id"]
            available = max(0.0, available_map.get(vid, 0.0))
            needed = comp["qty_needed"]
            short = max(0.0, needed - available)
            status = "ok" if short == 0 else "short"

            tmpl_info = tmpl_map.get(comp.get("template_id"), {})
            purchase_ok = tmpl_info.get("purchase_ok", True)
            sale_ok = tmpl_info.get("sale_ok", False)

            po_data = po_map.get(vid, {})

            if short > 0:
                short_count += 1
                if purchase_ok:
                    purchase_short += 1
                if sale_ok:
                    sale_short += 1

            result_flat.append({
                "variant_id": vid,
                "template_id": comp.get("template_id"),
                "product_name": comp["product_name"],
                "default_code": comp["default_code"],
                "qty_needed": needed,
                "qty_available": available,
                "qty_short": short,
                "status": status,
                "purchase_ok": purchase_ok,
                "sale_ok": sale_ok,
                "qty_ordered": po_data.get("qty_ordered", 0),
                "earliest_arrival": po_data.get("earliest_arrival"),
            })

        # Sort: purchase items first (short first within each group), then sale items
        result_flat.sort(key=lambda c: (
            0 if c["purchase_ok"] else 1,  # purchase first
            0 if c["status"] == "short" else 1,  # short first
            c["default_code"],
        ))

        max_prod = self._calc_max_from_components(flat_components, available_map)

        return {
            "assembly": assembly,
            "requested_qty": qty,
            "bom_tree": bom_tree,
            "flat_components": result_flat,
            "all_available": short_count == 0,
            "short_count": short_count,
            "purchase_short_count": purchase_short,
            "sale_short_count": sale_short,
            "max_producible": max_prod,
        }

    def _collect_variant_ids(self, tree: list[dict], ids: set[int]) -> None:
        """Recursively collect all variant_ids from a BOM tree."""
        for node in tree:
            ids.add(node["variant_id"])
            if node.get("children"):
                self._collect_variant_ids(node["children"], ids)

    def _enrich_tree_availability(self, tree: list[dict], available_map: dict[int, float],
                                   po_map: dict[int, dict] | None = None) -> None:
        """Add qty_available, qty_short, status, and PO data to each tree node."""
        for node in tree:
            vid = node["variant_id"]
            available = max(0.0, available_map.get(vid, 0.0))
            needed = node["qty_needed"]
            short = max(0.0, needed - available)
            node["qty_available"] = available
            node["qty_short"] = short
            node["status"] = "ok" if short == 0 else "short"

            po_data = (po_map or {}).get(vid, {})
            node["qty_ordered"] = po_data.get("qty_ordered", 0)
            node["earliest_arrival"] = po_data.get("earliest_arrival")

            if node.get("children"):
                self._enrich_tree_availability(node["children"], available_map, po_map)

    # ── Max producible ───────────────────────────────────────────────

    def calculate_max_producible(self, template_id: int) -> dict:
        """Calculate maximum number of assemblies producible with current stock."""
        _, components = self._get_bom_structure(template_id)
        components = copy.deepcopy(components)
        if not components:
            return {"template_id": template_id, "max_producible": 0, "bottleneck": None}

        location_ids = self._get_location_ids()
        variant_ids = [c["variant_id"] for c in components]
        quants = self.odoo.get_stock_quants(variant_ids, location_ids)

        available_map: dict[int, float] = {}
        for q in quants:
            vid = q["product_id"][0] if isinstance(q["product_id"], (list, tuple)) else q["product_id"]
            stock = q.get("quantity", 0.0) - q.get("reserved_quantity", 0.0)
            available_map[vid] = available_map.get(vid, 0.0) + stock

        max_prod = self._calc_max_from_components(components, available_map)

        bottleneck = None
        for comp in components:
            vid = comp["variant_id"]
            available = max(0.0, available_map.get(vid, 0.0))
            per_unit = comp["qty_needed"]
            if per_unit > 0:
                possible = math.floor(available / per_unit)
                if possible == max_prod:
                    bottleneck = {
                        "product_name": comp["product_name"],
                        "default_code": comp["default_code"],
                        "available": available,
                        "per_unit": per_unit,
                    }
                    break

        return {
            "template_id": template_id,
            "max_producible": max_prod,
            "bottleneck": bottleneck,
        }

    def _calc_max_from_components(self, components: list[dict],
                                  available_map: dict[int, float]) -> int:
        """Calculate max producible from exploded components and stock map."""
        if not components:
            return 0

        max_prod = float("inf")
        for comp in components:
            vid = comp["variant_id"]
            available = max(0.0, available_map.get(vid, 0.0))
            per_unit = comp["qty_needed"]
            if per_unit > 0:
                possible = math.floor(available / per_unit)
                max_prod = min(max_prod, possible)

        return int(max_prod) if max_prod != float("inf") else 0
