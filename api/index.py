"""
api/index.py — PayPal Charge API (Vercel edition)
GiveWP + PayPal Commerce flow only. No Playwright.
"""

from __future__ import annotations

import sys
import os
import time
import traceback
from typing import List, Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ensure local module import works on Vercel
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from . import paypal_engine as engine
except ImportError:
    import paypal_engine as engine

app = FastAPI(title="PayPal Charge API", version="1.0-vercel")


class ChargeItem(BaseModel):
    cc: str = Field(..., description="CC|MM|YY|CVC")
    amount: float = Field(1.00)
    site: Optional[str] = None
    button_id: Optional[str] = None
    proxy: Optional[str] = None


class BatchRequest(BaseModel):
    items: List[ChargeItem]


@app.get("/")
def index():
    return {
        "service": "paypal-charger",
        "edition": "vercel",
        "version": "1.0",
        "default_site": engine.GIVEWP_SITES[0]["name"],
        "default_amount": "1.00",
        "default_currency": "USD",
        "note": "Donate-button flow (Playwright) not available on Vercel.",
        "endpoints": {
            "GET /charge": "/charge?cc=4242...|12|28|123&amount=1.00",
            "POST /charge-batch": "body: {items:[...]}",
            "GET /discover": "probe sites",
            "GET /test": "order creation test (no card)",
        },
        "Owner": "t.me/RASHIK_69",
    }


@app.get("/charge")
def charge(
    cc: str = Query(..., description="CC|MM|YY|CVC"),
    amount: float = Query(1.00),
    site: Optional[str] = Query(None),
    button_id: Optional[str] = Query(None),
    proxy: Optional[str] = Query(None),
):
    if button_id:
        return JSONResponse({
            "response": "ERROR",
            "message": "Donate-button flow requires Playwright; not supported on Vercel. "
                       "Deploy to Railway/Render/Docker for this feature.",
            "Owner": "t.me/RASHIK_69",
        }, status_code=400)
    charger = engine.PayPalCharger(site=site, amount=amount, proxy=proxy)
    return JSONResponse(content=charger.charge(cc))


@app.post("/charge-batch")
def charge_batch(req: BatchRequest):
    results = []
    for i, item in enumerate(req.items, 1):
        charger = engine.PayPalCharger(site=item.site, amount=item.amount, proxy=item.proxy)
        res = charger.charge(item.cc)
        res["batch_index"] = i
        results.append(res)
        time.sleep(0.5)
    return JSONResponse(content={"count": len(results), "results": results})


@app.get("/discover")
def discover():
    sites = engine.discover_sites()
    working = [s for s in sites if s.get("tokens_extractable")]
    return JSONResponse(content={"checked": len(sites),
                                 "working_count": len(working),
                                 "sites": sites, "working": working})


@app.get("/test")
def test_flow(site: Optional[str] = Query(None),
              amount: float = Query(1.00),
              proxy: Optional[str] = Query(None)):
    return JSONResponse(content=engine.test_order_creation(site=site, amount=amount, proxy=proxy))

# Vercel uses module-level `app` as WSGI/ASGI handler. No uvicorn here.
