import os
import re
from urllib.parse import urlparse, unquote

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

load_dotenv()

APP = FastAPI(title="TiendaColucci Price Kiosk Resolver")

# ===== Fixed store config =====
STORE_DOMAIN = "www.tiendacolucci.com.ar"
SALES_CHANNEL = "1"

# ===== VTEX credentials (must be set in Railway Variables) =====
VTEX_ACCOUNT = os.getenv("VTEX_ACCOUNT", "").strip()
VTEX_APP_KEY = os.getenv("VTEX_APP_KEY", "").strip()
VTEX_APP_TOKEN = os.getenv("VTEX_APP_TOKEN", "").strip()
DEFAULT_SELLER = os.getenv("DEFAULT_SELLER", "1").strip()

if not VTEX_ACCOUNT or not VTEX_APP_KEY or not VTEX_APP_TOKEN:
    raise RuntimeError(
        "Missing VTEX_ACCOUNT / VTEX_APP_KEY / VTEX_APP_TOKEN environment variables"
    )

VTEX_BASE = f"https://{VTEX_ACCOUNT}.vtexcommercestable.com.br"

# ===== CORS (frontend PWA calls backend) =====
APP.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # en prod podés restringir al dominio del frontend
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ResolveReq(BaseModel):
    url: HttpUrl


class ResolveResp(BaseModel):
    ok: bool
    url: str
    slug: str
    productName: str | None = None
    skuId: str | None = None
    seller: str | None = None
    imageUrl: str | None = None

    # prices in cents
    price: int | None = None
    listPrice: int | None = None
    sellingPrice: int | None = None

    currency: str = "ARS"
    source: str


def extract_slug_from_url(raw_url: str) -> str:
    """
    VTEX PDP típico:
      https://www.tiendacolucci.com.ar/<slug>/p
    Tomamos el primer segmento del path como slug.
    """
    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower()

    if host != STORE_DOMAIN:
        raise HTTPException(status_code=400, detail="URL domain not allowed")

    path = unquote(parsed.path or "").strip("/")
    if not path:
        raise HTTPException(status_code=400, detail="URL path is empty")

    first = path.split("/")[0].strip()
    slug = re.sub(r"[^a-zA-Z0-9\-_.]", "", first).strip().lower()

    if not slug:
        raise HTTPException(status_code=400, detail="Could not extract slug from URL")

    return slug


async def vtex_search_by_slug(slug: str) -> dict:
    """
    Search API público del store:
      1) /api/catalog_system/pub/products/search/{slug}/p
      2) fallback /api/catalog_system/pub/products/search?ft=
    """
    url1 = f"https://{STORE_DOMAIN}/api/catalog_system/pub/products/search/{slug}/p"
    params1 = {"sc": SALES_CHANNEL}

    async with httpx.AsyncClient(timeout=10) as client:
        r1 = await client.get(url1, params=params1)
        if r1.status_code == 200:
            data = r1.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0]

        url2 = f"https://{STORE_DOMAIN}/api/catalog_system/pub/products/search"
        params2 = {"ft": slug, "sc": SALES_CHANNEL}
        r2 = await client.get(url2, params=params2)

        if r2.status_code != 200:
            raise HTTPException(status_code=502, detail="VTEX search failed")

        data2 = r2.json()
        if not isinstance(data2, list) or len(data2) == 0:
            raise HTTPException(status_code=404, detail="Product not found by slug")

        # mejor match por linkText si existe
        for p in data2:
            if (p.get("linkText") or "").lower() == slug:
                return p

        return data2[0]


def pick_sku(product: dict) -> tuple[str, str, str | None, dict]:
    """
    Elige el primer SKU. También extrae precio de catálogo (commertialOffer)
    para fallback si simulation falla.
    """
    items = product.get("items") or []
    if not items:
        raise HTTPException(status_code=404, detail="No SKUs in product")

    item = items[0]
    sku_id = str(item.get("itemId") or item.get("id") or "")
    if not sku_id:
        raise HTTPException(status_code=404, detail="Could not find skuId in product")

    seller = DEFAULT_SELLER
    image_url = None
    offer_prices: dict = {}

    sellers = item.get("sellers") or []
    if sellers:
        s0 = sellers[0]
        if s0.get("sellerId"):
            seller = str(s0["sellerId"])

        comm = s0.get("commertialOffer") or {}
        # VTEX suele devolver valores en ARS como float, no en centavos
        offer_prices = {
            "Price": comm.get("Price"),
            "ListPrice": comm.get("ListPrice"),
        }

    images = item.get("images") or []
    if images and images[0].get("imageUrl"):
        image_url = images[0]["imageUrl"]

    return sku_id, seller, image_url, offer_prices


async def vtex_simulate_price(sku_id: str, seller: str) -> dict:
    """
    Checkout Simulation para precio final (con promos/reglas).
    Si falla, devolvemos un error con detalle (para debug).
    """
    endpoint = f"{VTEX_BASE}/api/checkout/pub/orderForms/simulation"
    params = {"sc": SALES_CHANNEL}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-VTEX-API-AppKey": VTEX_APP_KEY,
        "X-VTEX-API-AppToken": VTEX_APP_TOKEN,
    }

    body = {
        "items": [{"id": str(sku_id), "quantity": 1, "seller": str(seller)}],
        # OJO: a veces country rompe según configuración de cuenta.
        # "country": "ARG",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(endpoint, params=params, headers=headers, json=body)

        if r.status_code != 200:
            detail = (r.text or "").strip()
            raise HTTPException(
                status_code=502,
                detail=f"VTEX simulation failed ({r.status_code}): {detail}",
            )

        data = r.json()
        items = data.get("items") or []
        if not items:
            raise HTTPException(status_code=502, detail="VTEX simulation returned no items")

        it = items[0]
        return {
            "price": it.get("price"),
            "listPrice": it.get("listPrice"),
            "sellingPrice": it.get("sellingPrice"),
        }


def to_cents(v) -> int | None:
    if v is None:
        return None
    try:
        return int(round(float(v) * 100))
    except Exception:
        return None


@APP.get("/health")
def health():
    return {"ok": True, "domain": STORE_DOMAIN, "sc": SALES_CHANNEL}


@APP.post("/resolve", response_model=ResolveResp)
async def resolve(req: ResolveReq):
    slug = extract_slug_from_url(str(req.url))

    product = await vtex_search_by_slug(slug)
    product_name = product.get("productName") or None

    sku_id, seller, image_url, offer_prices = pick_sku(product)

    # 1) Intentamos simulation (promos)
    # 2) Si falla (tu caso), fallback a catálogo (commertialOffer)
    try:
        prices = await vtex_simulate_price(sku_id, seller)
        source = "search+simulation"
    except HTTPException as e:
        # fallback SOLO si falló simulation; si fue otra cosa, re-raise
        msg = str(e.detail or "")
        if "VTEX simulation failed" in msg or "VTEX simulation returned no items" in msg:
            prices = {
                "price": to_cents(offer_prices.get("Price")),
                "sellingPrice": to_cents(offer_prices.get("Price")),
                "listPrice": to_cents(offer_prices.get("ListPrice")),
            }
            source = "search+catalog_offer_fallback"
        else:
            raise

    return ResolveResp(
        ok=True,
        url=str(req.url),
        slug=slug,
        productName=product_name,
        skuId=sku_id,
        seller=seller,
        imageUrl=image_url,
        price=prices.get("price"),
        listPrice=prices.get("listPrice"),
        sellingPrice=prices.get("sellingPrice"),
        source=source,
    )
