import os
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


APP = FastAPI(title="Kiosco Precios VTEX", version="1.0.0")


# ============
# ENV CONFIG
# ============
VTEX_ACCOUNT = os.getenv("VTEX_ACCOUNT")  # ej: "tiendacolucci"
VTEX_APP_KEY = os.getenv("VTEX_APP_KEY")
VTEX_APP_TOKEN = os.getenv("VTEX_APP_TOKEN")

STORE_DOMAIN = os.getenv("STORE_DOMAIN", "www.tiendacolucci.com.ar")
SALES_CHANNEL = os.getenv("SALES_CHANNEL", "1")  # sc=1

if not VTEX_ACCOUNT or not VTEX_APP_KEY or not VTEX_APP_TOKEN:
    raise RuntimeError("Missing VTEX_ACCOUNT / VTEX_APP_KEY / VTEX_APP_TOKEN environment variables")


VTEX_BASE = f"https://{VTEX_ACCOUNT}.vtexcommercestable.com.br"


class ResolveIn(BaseModel):
    url: str


# =========================
# Helpers: URL -> SKU ID
# =========================
def extract_sku_id_from_url(raw_url: str) -> Optional[int]:
    """
    Intenta sacar skuId/itemId/ITEM_ID desde:
      - query params: skuId, skuid, itemId, item_id, ITEM_ID
      - path: .../sku/12345...  (por si lo usan así)
    """
    try:
        u = urlparse(raw_url)
    except Exception:
        return None

    qs = parse_qs(u.query or "")

    # Preferencias típicas
    candidates = [
        "skuId",
        "skuid",
        "itemId",
        "item_id",
        "ITEM_ID",
        "SkuId",
        "ItemId",
    ]
    for k in candidates:
        if k in qs and qs[k]:
            v = qs[k][0]
            if v and str(v).isdigit():
                return int(v)

    # fallback: buscar número grande en path con patrón
    m = re.search(r"(?:sku|skuid|item|itemid|item_id)[=/\-](\d+)", (u.path or "") + "?" + (u.query or ""), re.IGNORECASE)
    if m:
        return int(m.group(1))

    return None


def validate_domain(raw_url: str) -> None:
    """
    Valida que el QR apunte a tu dominio (o subdominio).
    """
    try:
        u = urlparse(raw_url)
    except Exception:
        raise HTTPException(status_code=400, detail="URL inválida")

    host = (u.netloc or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="URL sin dominio")

    # Acepta el dominio exacto o subdominios
    if host == STORE_DOMAIN.lower() or host.endswith("." + STORE_DOMAIN.lower()):
        return

    raise HTTPException(status_code=400, detail="Dominio no permitido")


def vtex_headers() -> Dict[str, str]:
    # VTEX autentica con AppKey/AppToken en headers :contentReference[oaicite:1]{index=1}
    return {
        "X-VTEX-API-AppKey": VTEX_APP_KEY,
        "X-VTEX-API-AppToken": VTEX_APP_TOKEN,
    }


# =========================
# VTEX calls
# =========================
async def get_sku_basic(client: httpx.AsyncClient, sku_id: int) -> Dict[str, Any]:
    """
    Trae info básica del SKU (privado), útil para:
      - product name
      - productId
      - si está activo
    """
    url = f"{VTEX_BASE}/api/catalog_system/pvt/sku/stockkeepingunitbyid/{sku_id}"
    r = await client.get(url, headers=vtex_headers(), timeout=20)
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="SKU no existe")
    r.raise_for_status()
    return r.json()


async def get_pricing_price_any_visibility(client: httpx.AsyncClient, sku_id: int, sc: str) -> Dict[str, Any]:
    """
    Pricing API (privada): devuelve precio aunque no esté publicado / sin stock.
    Endpoint típico: /api/pricing/prices/{itemId}
    """
    url = f"{VTEX_BASE}/api/pricing/prices/{sku_id}"
    r = await client.get(url, headers=vtex_headers(), timeout=20)
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="SKU sin precio configurado")
    r.raise_for_status()
    data = r.json()

    # data suele tener basePrice y fixedPrices (por tradePolicyId)
    # Buscamos fixedPrices para el sales channel (sc) si existe:
    fixed = None
    fixed_list = data.get("fixedPrices") or data.get("FixedPrices") or []
    for fp in fixed_list:
        # tradePolicyId suele venir como string
        tpid = fp.get("tradePolicyId") or fp.get("TradePolicyId")
        if tpid is not None and str(tpid) == str(sc):
            fixed = fp
            break

    # Elegimos precio "selling" como fixedPrice.value si existe, si no basePrice
    base_price = data.get("basePrice") or data.get("BasePrice")
    list_price = data.get("listPrice") or data.get("ListPrice")  # a veces no está

    selling = None
    if fixed is not None:
        selling = fixed.get("value") or fixed.get("Value")
        # en fixedPrices a veces también viene listPrice
        list_price = fixed.get("listPrice") or fixed.get("ListPrice") or list_price

    if selling is None:
        selling = base_price

    if selling is None:
        raise HTTPException(status_code=404, detail="SKU sin precio configurado")

    return {
        "selling": float(selling),
        "list": float(list_price) if list_price is not None else None,
        "raw": data,
    }


async def get_stock_quantity_if_possible(client: httpx.AsyncClient, sku_id: int) -> Tuple[Optional[int], Optional[bool]]:
    """
    Intenta obtener stock (privado) sin afectar precio.
    Si falla, devuelve (None, None).
    """
    try:
        # Endpoint común de inventory: /api/logistics/pvt/inventory/skus/{skuId}
        # (si no tenés permisos, puede dar 403)
        url = f"{VTEX_BASE}/api/logistics/pvt/inventory/skus/{sku_id}"
        r = await client.get(url, headers=vtex_headers(), timeout=20)
        if r.status_code in (403, 404):
            return None, None
        r.raise_for_status()
        data = r.json()

        # Estructuras típicas: balance por warehouse
        # Sumamos totalQuantity (si existe) o reserved/available (según tienda)
        total = 0
        found_any = False

        balances = data.get("balance") or data.get("Balance") or []
        if isinstance(balances, list):
            for b in balances:
                # algunas tiendas: totalQuantity / availableQuantity
                tq = b.get("totalQuantity")
                aq = b.get("availableQuantity")
                if tq is not None:
                    total += int(tq)
                    found_any = True
                elif aq is not None:
                    total += int(aq)
                    found_any = True

        if not found_any:
            return None, None

        return total, total > 0
    except Exception:
        return None, None


# =========================
# Routes
# =========================
@APP.get("/health")
async def health():
    return {"ok": True, "domain": STORE_DOMAIN, "sc": str(SALES_CHANNEL)}


@APP.post("/resolve")
async def resolve(payload: ResolveIn):
    validate_domain(payload.url)

    sku_id = extract_sku_id_from_url(payload.url)
    if not sku_id:
        raise HTTPException(
            status_code=400,
            detail="No pude detectar el SKU/ITEM_ID en la URL del QR. Usá un QR que incluya ?ITEM_ID=12345 (o skuId=12345).",
        )

    async with httpx.AsyncClient() as client:
        sku = await get_sku_basic(client, sku_id)

        # Nombre “humano” (fallbacks)
        product_name = (
            sku.get("NameComplete")
            or sku.get("Name")
            or sku.get("name")
            or sku.get("nameComplete")
            or f"SKU {sku_id}"
        )

        pricing = await get_pricing_price_any_visibility(client, sku_id, sc=str(SALES_CHANNEL))

        # Intentamos stock (si no hay permisos o no aplica, no rompe)
        qty, in_stock = await get_stock_quantity_if_possible(client, sku_id)

        # Front trabaja en centavos
        selling_cents = int(round(pricing["selling"] * 100))
        list_cents = int(round(pricing["list"] * 100)) if pricing["list"] is not None else None

        return {
            "skuId": sku_id,
            "productName": product_name,
            "sellingPrice": selling_cents,
            "listPrice": list_cents,
            "availableQuantity": qty,   # puede ser None
            "inStock": in_stock,        # puede ser None
            "source": "pricing_api",
        }