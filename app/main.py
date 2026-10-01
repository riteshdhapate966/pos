import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import touche

app = FastAPI(title="POS")


@app.middleware("http")
async def no_stale_ui(request, call_next):
    # Without this the browser heuristically caches app.js, so a new index.html can run an old app.js and crash.
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response

# ponytail: in-memory state, lost on restart; move to a DB when running >1 worker or sessions must survive restarts
sessions: dict[str, dict] = {}
menu_index: dict[str, dict[tuple[str, str], dict]] = {}  # outlet -> (subMenuCode, itemCode) -> raw Touché item

VOID_REASON = {"Code": "GCOR", "Reason": "GUEST CANCELLED THE ORDER", "UserInputAllowed": "N"}  # from the Touché spec sample
SHIFT_NUMBER = 1


class SessionIn(BaseModel):
    outletCode: str = Field(min_length=1)
    tableNumber: str = Field(min_length=1)
    covers: int = Field(1, ge=0)
    checkNumber: str | None = None  # attach an already-open POS check (picked from the tables list)
    qrId: str | None = None


class OrderItem(BaseModel):
    itemCode: str
    subMenuCode: str  # needed: the same item can sit in several sub-menus, Touché rejects a wrong link
    quantity: float = Field(1, gt=0)
    comment: str | None = Field(None, max_length=100)


class OrderIn(BaseModel):
    items: list[OrderItem] = Field(min_length=1)


class SettleIn(BaseModel):
    settlementOptionCode: str
    amount: float | None = Field(None, gt=0)  # defaults to the full balance


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- outlet ----------

@app.get("/api/v1/outlets")
async def get_outlets():
    data = await touche.call("TOUCHE/GetAllOutlets", {})
    return [{"code": o["OutletCode"], "name": o["OutletName"]} for o in data.get("Outlets") or []]


async def load_menu(outlet: str) -> list[dict]:
    data = await touche.call("ToucheLite/GetOutletDetails", {"OutletCode": outlet, "UserId": touche.USER_ID})
    index, menus = {}, []
    for m in data.get("OutletMenus") or []:
        subs = []
        for sm in m.get("SubMenus") or []:
            # ponytail: plain menu items only; packages/combos/open items need extra line data in SaveOrder
            items = [i for i in sm.get("MenuItems") or [] if i.get("ItemType") == "MI"]
            for i in items:
                index[(i["SubMenuCode"], i["ItemCode"])] = i
            subs.append({"code": sm["SubMenuCode"], "name": sm["SubMenuName"], "items": [
                {
                    "itemCode": i["ItemCode"],
                    "subMenuCode": i["SubMenuCode"],
                    "name": i["ItemName"],
                    "description": i.get("ItemDescription"),
                    "price": i["ItemPrice"],
                    "priceWithTax": i.get("ItemPriceWithTax"),
                    "imageUrl": i.get("ItemImageUrl") or None,
                    "hasAddOns": bool(i.get("AddOnCategories")),
                }
                for i in items
            ]})
        menus.append({"code": m["MenuCode"], "name": m["MenuName"], "subMenus": subs})
    menu_index[outlet] = index
    return menus


@app.get("/api/v1/outlets/{outlet}/menu")
async def get_menu(outlet: str):
    return {"outletCode": outlet, "menus": await load_menu(outlet)}


@app.get("/api/v1/outlets/{outlet}/tables")
async def get_tables(outlet: str):
    """All tables of the outlet; occupied ones carry their open check."""
    data = await touche.call("ToucheLite/GetAllOpenChecks", {"OutletCode": outlet})
    return [
        {
            "tableNumber": c["TableNumber"],
            "covers": c.get("CoverNumber"),
            "checkNumber": c["CheckNumber"] or None,
            "checkStatus": c.get("CheckStatus") or None,
            "total": c.get("TotalOrderPrice") or 0,
            "openTime": c.get("CheckOpenTime") or None,
        }
        for c in data.get("Checks") or []
    ]


@app.get("/api/v1/outlets/{outlet}/settlement-options")
async def get_settlement_options(outlet: str):
    data = await touche.call("TOUCHE/GetSettlementOptions", {"OutletCode": outlet, "UserId": touche.USER_ID})
    return [
        {"code": o["SettlementOptionCode"], "name": o["SettlementOptionName"], "type": o["SettlementType"]}
        for o in data.get("SettlementOptions") or []
    ]


# ---------- sessions ----------

def get_session(sid: str, active: bool = True) -> dict:
    s = sessions.get(sid)
    if not s:
        raise HTTPException(404, "Session not found")
    if active and s["status"] != "ACTIVE":
        raise HTTPException(409, "Session is closed")
    return s


async def fetch_check(s: dict) -> dict:
    data = await touche.call("TOUCHE/GetCheckDetails", {"CheckNumber": s["checkNumber"], "CheckLock": "N"})
    return data["Check"]


@asynccontextmanager
async def locked_check(s: dict):
    """Lock the check like a POS device does before changing it, and always release it.

    Without the lock Touché rejects changes with "The status of the check may have changed.
    Please reselect the check" (same lock -> change -> release sequence as the Postman collection).
    """
    data = await touche.call("TOUCHE/GetCheckDetails", {"CheckNumber": s["checkNumber"], "CheckLock": "Y"})
    try:
        yield data["Check"]
    finally:
        with suppress(HTTPException):  # don't let a failed release hide the real error
            await touche.call("TOUCHE/ReleaseCheckLock", {"CheckNumber": s["checkNumber"]})


@app.post("/api/v1/sessions")
def open_session(body: SessionIn):
    """Create a session for a table, or return the active one (same QR/table -> same session -> same check)."""
    for s in sessions.values():
        if s["status"] == "ACTIVE" and s["outletCode"] == body.outletCode and s["tableNumber"] == body.tableNumber:
            if body.checkNumber and not s["checkNumber"]:
                s["checkNumber"] = body.checkNumber
            return s
    s = {"sessionId": uuid.uuid4().hex[:12], "status": "ACTIVE", **body.model_dump()}
    sessions[s["sessionId"]] = s
    return s


@app.get("/api/v1/sessions/{sid}")
def read_session(sid: str):
    return get_session(sid, active=False)


@app.post("/api/v1/sessions/{sid}/items")
async def add_items(sid: str, body: OrderIn):
    """SaveOrder. First order opens a new check; later ones reuse the session's check."""
    s = get_session(sid)
    if s["outletCode"] not in menu_index:
        await load_menu(s["outletCode"])
    index = menu_index[s["outletCode"]]

    lines = []
    for o in body.items:
        i = index.get((o.subMenuCode, o.itemCode))
        if not i:
            raise HTTPException(422, f"Item {o.itemCode} not in sub-menu {o.subMenuCode}")
        lines.append({
            "ItemCode": i["ItemCode"], "ItemName": i["ItemName"], "SubMenuCode": i["SubMenuCode"],
            "ItemType": i["ItemType"], "ItemPrice": i["ItemPrice"], "ItemAccount": i["ItemAccount"],
            "OrderedQuantity": o.quantity, "KotComment": o.comment,
            "IsHappyHour": None, "ItemStatus": None, "CourseNumber": 0, "CoverNumber": 0,
            "ParentItemCode": None, "ItemAccountName": None, "ParentItemSequenceNumber": 0,
            "PackageConstituentLineItems": None, "AddOnLineItems": None,
        })

    data = await touche.call("ToucheLite/SaveOrder", {
        "UserId": touche.USER_ID, "OutletCode": s["outletCode"],
        "GuestName": None, "TipAmount": 0, "CreateOnAuditDate": None, "DiscountSlab": None,
        "Check": {
            "TableNumber": s["tableNumber"], "Covers": s["covers"], "IsResident": "N", "IsNonChargeable": "N",
            "Comments": None, "CheckNumber": s["checkNumber"] or "", "GuestProfile": None,
            "OrderLineItems": lines,
        },
    })
    s["checkNumber"] = data["CheckNumber"]
    return {"sessionId": sid, "checkNumber": data["CheckNumber"], "checkStatus": data["CheckStatus"], "balance": data["Balance"]}


@app.get("/api/v1/sessions/{sid}/bill")
async def get_bill(sid: str):
    s = get_session(sid, active=False)
    if not s["checkNumber"]:
        return {"sessionId": sid, "checkNumber": None, "checkStatus": None,
                "subTotal": 0, "discount": 0, "taxes": 0, "balance": 0, "lines": []}
    c = await fetch_check(s)
    o = c["Order"]
    return {
        "sessionId": sid,
        "checkNumber": c["CheckNumber"],
        "checkStatus": c["CheckStatus"],
        "tableNumber": c["TableNumber"],
        "subTotal": o["SubTotal"],
        "discount": o["Discount"],
        "taxes": o["Taxes"],
        "balance": o["Balance"],
        "lines": [
            {
                "sequenceNumber": l["SequenceNumber"],
                "itemCode": l["ItemCode"],
                "subMenuCode": l["SubMenuCode"],
                "name": l["ItemName"],
                "quantity": l["OrderedQuantity"],
                "price": l["ItemPrice"],
                "status": l["ItemStatus"],  # PO saved, VT voided, XI/XO moved
                "kotNumber": l.get("KotNumber"),
            }
            for l in o.get("OrderLineItems") or []
        ],
    }


@app.delete("/api/v1/sessions/{sid}/items/{seq}")
async def remove_item(sid: str, seq: str, quantity: float | None = None):
    """Void a saved line (whole line, or `quantity` of it) via CancelItems."""
    # ponytail: UNVERIFIED. On staging (2026-09-22) CancelItems answered "Object reference not set"
    # and the outlet's check reads hung afterwards. Confirm the request format with Prologic before production.
    s = get_session(sid)
    if not s["checkNumber"]:
        raise HTTPException(409, "No check yet")
    spec_fields = ("ItemCode", "ItemName", "SubMenuCode", "ItemType", "ItemPrice", "IsHappyHour", "ItemStatus",
                   "CourseNumber", "CoverNumber", "KotComment", "ItemAccount", "ParentItemCode",
                   "ItemAccountName", "ParentItemSequenceNumber", "PackageConstituentLineItems",
                   "AddOnLineItems", "SequenceNumber")
    async with locked_check(s) as c:
        line = next((l for l in c["Order"].get("OrderLineItems") or []
                     if l["SequenceNumber"] == seq and l["ItemStatus"] == "PO"), None)
        if not line:
            raise HTTPException(404, f"No saved line {seq} on check {s['checkNumber']}")
        qty = quantity or line["OrderedQuantity"]
        if qty > line["OrderedQuantity"]:
            raise HTTPException(422, f"Line {seq} only has {line['OrderedQuantity']}")

        await touche.call("ToucheLite/CancelItems", {
            "UserId": touche.USER_ID, "OutletCode": s["outletCode"],
            "GuestName": None, "TipAmount": 0, "CreateOnAuditDate": None,
            "Check": {
                "TableNumber": c["TableNumber"], "Covers": c["Covers"], "IsResident": c["IsResident"],
                "IsNonChargeable": c["IsNonChargeable"], "Comments": None, "CheckNumber": s["checkNumber"],
                "GuestProfile": None,
                "OrderLineItems": [{**{k: line.get(k) for k in spec_fields}, "OrderedQuantity": qty}],
            },
            "VoidReason": VOID_REASON,
        })
    return await get_bill(sid)


@app.post("/api/v1/sessions/{sid}/settle")
async def settle(sid: str, body: SettleIn):
    """Pay the check at the POS (cashier-style). Full payment closes the session."""
    s = get_session(sid)
    if not s["checkNumber"]:
        raise HTTPException(409, "Nothing to settle")
    options = {o["code"]: o for o in await get_settlement_options(s["outletCode"])}
    option = options.get(body.settlementOptionCode)
    if not option:
        raise HTTPException(422, f"Unknown settlement option {body.settlementOptionCode}")

    async with locked_check(s) as c:
        if c["CheckStatus"] != "PO":
            raise HTTPException(409, f"Check {s['checkNumber']} is {c['CheckStatus']}, not open")
        balance = c["Order"]["Balance"]
        amount = body.amount or balance
        complete = amount >= balance
        await touche.call("ToucheLite/SettleOrder", {
            "userId": touche.USER_ID, "outletCode": s["outletCode"], "checkNumber": s["checkNumber"],
            "settlementOptionCode": option["code"], "settlementType": option["type"],
            "isCompletlySettled": "Y" if complete else "N",
            "receivedAmount": amount, "checkBalance": balance, "checkAmount": balance,
            "shiftNumber": SHIFT_NUMBER,
            "cardDetails": {"cardExpiryDate": None, "cardNumber": None, "cardStrip": None},
            "settleGuestDetails": {"accountId": None, "guestName": None, "roomNumber": None,
                                   "membershipId": None, "pointsRedeemed": 0, "guestProfileCode": 0},
        })
    if complete:
        s["status"] = "CLOSED"
    return {"sessionId": sid, "checkNumber": s["checkNumber"], "settled": amount,
            "change": round(max(amount - balance, 0), 2), "sessionStatus": s["status"]}


# UI last so /api and /health win.
app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "script", html=True), name="ui")
