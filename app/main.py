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

STORE_DOMAIN = "www.tiendacolucci.com.ar"
SALES_CHANNEL = "1"

VTEX_ACCOUNT = os.getenv("VTEX_ACCOUNT", "").strip()
VTEX_APP_KEY = os.getenv("VTEX_APP_KEY", "").strip()
VTEX_APP_TOKEN = os.getenv("VTEX_APP_TOKEN", "").strip()
DEFAULT_SELLER = os.getenv("DEFAULT_SELLER", "1").strip()

if not VTEX_ACCOUNT or not VTEX_APP_KEY or not VTEX_APP_TOKEN:
    raise RuntimeError("Missing VTEX_ACCOUNT / VTEX_APP_KEY / VTEX_APP_TOKEN in .env")

VTEX_BASE = f"https://{VTEX_ACCOUNT}.vtexcommercestable.com.br"

# CORS (para que la PWA en la tablet pueda llamar al backend)
APP.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # en prod: poné tu dominio del front o la IP de la tablet
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
    price: int | None = None         # centavos
    listPrice: int | None = None     # centavos
    sellingPrice: int | None = None  # centavos
    currency: str = "ARS"
    source: str


def extract_slug_from_url(raw_url: str) -> str:
    """
    VTEX PDP típico: https://www.tiendacolucci.com.ar/<slug>/p
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
    # limpieza defensiva
    slug = re.sub(r"[^a-zA-Z0-9\-_.]", "", first).strip().lower()

    if not slug:
        raise HTTPException(status_code=400, detail="Could not extract slug from URL")

    return slug


async def vtex_search_by_slug(slug: str) -> dict:
    """
    Usa el Search API público del store para traer el producto por slug.
    1) /{slug}/p
    2) fallback /search?ft=
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


def pick_sku(product: dict) -> tuple[str, str, str | None]:
    items = product.get("items") or []
    if not items:
        raise HTTPException(status_code=404, detail="No SKUs in product")

    item = items[0]
    sku_id = str(item.get("itemId") or item.get("id") or "")
    if not sku_id:
        raise HTTPException(status_code=404, detail="Could not find skuId in product")

    seller = DEFAULT_SELLER
    sellers = item.get("sellers") or []
    if sellers and sellers[0].get("sellerId"):
        seller = str(sellers[0]["sellerId"])

    image_url = None
    images = item.get("images") or []
    if images and images[0].get("imageUrl"):
        image_url = images[0]["imageUrl"]

    return sku_id, seller, image_url


async def vtex_simulate_price(sku_id: str, seller: str) -> dict:
    """
    OrderForm Simulation para precio final (con promos/reglas).
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
        "country": "ARG",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(endpoint, params=params, headers=headers, json=body)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"VTEX simulation failed ({r.status_code})")

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


@APP.get("/health")
def health():
    return {"ok": True, "domain": STORE_DOMAIN, "sc": SALES_CHANNEL}


@APP.post("/resolve", response_model=ResolveResp)
async def resolve(req: ResolveReq):
    slug = extract_slug_from_url(str(req.url))

    product = await vtex_search_by_slug(slug)
    product_name = product.get("productName") or None

    sku_id, seller, image_url = pick_sku(product)
    prices = await vtex_simulate_price(sku_id, seller)

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
        source="search+simulation",
    )
