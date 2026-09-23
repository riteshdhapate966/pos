"""Full POS flow against a fake Touché. Run: uv run python test_app.py"""
from fastapi.testclient import TestClient

from app import main, touche

calls = []
line = {"SequenceNumber": "1", "ItemCode": "2006", "ItemName": "Fattoush", "SubMenuCode": "DISA", "ItemType": "MI",
        "ItemPrice": 40, "ItemStatus": "PO", "OrderedQuantity": 2, "ItemAccount": "FD", "KotNumber": "K1"}
check = {"CheckNumber": "A1", "CheckStatus": "PO", "TableNumber": "9", "Covers": 1, "IsResident": "N",
         "IsNonChargeable": "N", "Order": {"SubTotal": 80, "Discount": 0, "Taxes": 4, "Balance": 84, "OrderLineItems": [line]}}


async def fake_call(method, payload):
    calls.append((method, payload))
    return {
        "ToucheLite/GetOutletDetails": {"OutletMenus": [{"MenuCode": "FD", "MenuName": "FOOD", "SubMenus": [
            {"SubMenuCode": "DISA", "SubMenuName": "SALADS", "MenuItems": [
                {"ItemCode": "2006", "SubMenuCode": "DISA", "ItemName": "Fattoush", "ItemPrice": 40, "ItemType": "MI", "ItemAccount": "FD"},
                {"ItemCode": "PK1", "SubMenuCode": "DISA", "ItemName": "Package", "ItemPrice": 99, "ItemType": "FP", "ItemAccount": "FD"},
            ]}]}]},
        "ToucheLite/SaveOrder": {"CheckNumber": "A1", "Balance": 84, "CheckStatus": "PO"},
        "TOUCHE/GetCheckDetails": {"Check": check},
        "ToucheLite/CancelItems": {},
        "TOUCHE/GetSettlementOptions": {"SettlementOptions": [{"SettlementOptionCode": "CASH", "SettlementOptionName": "Cash", "SettlementType": "CA"}]},
        "ToucheLite/SettleOrder": {},
        "TOUCHE/ReleaseCheckLock": {},
    }[method]


def test_flow():
    touche.call = fake_call
    main.sessions.clear()
    c = TestClient(main.app)

    menu = c.get("/api/v1/outlets/DINE/menu").json()
    assert [i["itemCode"] for i in menu["menus"][0]["subMenus"][0]["items"]] == ["2006"]  # FP filtered out

    s = c.post("/api/v1/sessions", json={"outletCode": "DINE", "tableNumber": "9"}).json()
    assert c.post("/api/v1/sessions", json={"outletCode": "DINE", "tableNumber": "9"}).json()["sessionId"] == s["sessionId"]

    sid = s["sessionId"]
    order = {"items": [{"itemCode": "2006", "subMenuCode": "DISA", "quantity": 2}]}
    assert c.post(f"/api/v1/sessions/{sid}/items", json=order).json()["checkNumber"] == "A1"
    assert calls[-1][1]["Check"]["CheckNumber"] == ""  # first order opens a new check
    c.post(f"/api/v1/sessions/{sid}/items", json=order)
    assert calls[-1][1]["Check"]["CheckNumber"] == "A1"  # later orders reuse it
    assert c.post(f"/api/v1/sessions/{sid}/items", json={"items": [{"itemCode": "2006", "subMenuCode": "X"}]}).status_code == 422

    bill = c.get(f"/api/v1/sessions/{sid}/bill").json()
    assert bill["balance"] == 84 and bill["lines"][0]["sequenceNumber"] == "1"

    calls.clear()
    assert c.delete(f"/api/v1/sessions/{sid}/items/1?quantity=5").status_code == 422
    assert calls[-1][0] == "TOUCHE/ReleaseCheckLock"  # lock released even when we bail out
    assert c.delete(f"/api/v1/sessions/{sid}/items/1?quantity=1").status_code == 200
    methods = [m for m, _ in calls]
    i = methods.index("ToucheLite/CancelItems")
    assert calls[i - 1] == ("TOUCHE/GetCheckDetails", {"CheckNumber": "A1", "CheckLock": "Y"})  # locked first
    assert methods[i + 1] == "TOUCHE/ReleaseCheckLock"  # released after
    voided = calls[i][1]["Check"]["OrderLineItems"][0]
    assert (voided["SequenceNumber"], voided["OrderedQuantity"]) == ("1", 1)

    r = c.post(f"/api/v1/sessions/{sid}/settle", json={"settlementOptionCode": "CASH", "amount": 100}).json()
    methods = [m for m, _ in calls]
    i = methods.index("ToucheLite/SettleOrder")
    assert calls[i - 1][1].get("CheckLock") == "Y" and methods[i + 1] == "TOUCHE/ReleaseCheckLock"
    settle = calls[i][1]
    assert settle["isCompletlySettled"] == "Y" and settle["checkBalance"] == 84 and settle["settlementType"] == "CA"
    assert r["change"] == 16 and r["sessionStatus"] == "CLOSED"
    assert c.post(f"/api/v1/sessions/{sid}/items", json=order).status_code == 409  # closed session

    assert c.get("/").status_code == 200  # UI served


if __name__ == "__main__":
    test_flow()
    print("ok")
