import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from upstash_redis import Redis

# ---------- 環境變數 ----------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
UPSTASH_REDIS_REST_URL = os.environ["UPSTASH_REDIS_REST_URL"]
UPSTASH_REDIS_REST_TOKEN = os.environ["UPSTASH_REDIS_REST_TOKEN"]
# 允許呼叫這支 API 的網頁來源，預設放你的 GitHub Pages 網址
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://sadaouo.github.io")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

redis = Redis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)

ITEMS_KEY = "ledger:items"
LOG_KEY = "ledger:log"
LOG_MAX = 50

SALE_RATE_KEY = "sale:rate"
SALE_TX_KEY = "sale:transactions"
SALE_TX_MAX = 2000

TW_TZ = timezone(timedelta(hours=8))

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 資料存取 ----------
def load_items():
    raw = redis.get(ITEMS_KEY)
    if not raw:
        # 第一次啟動時的預設項目，之後都以 Redis 內容為準
        default = [
            {"id": str(uuid.uuid4())[:8], "name": "東西1", "qty": 10},
            {"id": str(uuid.uuid4())[:8], "name": "東西2", "qty": 9},
            {"id": str(uuid.uuid4())[:8], "name": "東西3", "qty": 8},
        ]
        redis.set(ITEMS_KEY, json.dumps(default, ensure_ascii=False))
        return default
    return json.loads(raw)


def save_items(items):
    redis.set(ITEMS_KEY, json.dumps(items, ensure_ascii=False))


def load_log():
    raw = redis.get(LOG_KEY)
    return json.loads(raw) if raw else []


def push_log(name: str, amount: int = 1):
    log = load_log()
    log.insert(0, {"name": name, "amount": amount, "time": time.strftime("%m/%d %H:%M")})
    log = log[:LOG_MAX]
    redis.set(LOG_KEY, json.dumps(log, ensure_ascii=False))
    return log


# ---------- Telegram 小工具 ----------
async def tg_send_message(chat_id: int, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )


def format_items_text(items):
    if not items:
        return "目前沒有任何項目。"
    lines = [f"{i+1}. {it['name']}：{it['qty']}" for i, it in enumerate(items)]
    return "📦 目前庫存\n" + "\n".join(lines)


def apply_delta_by_id(item_id: str, delta: int):
    """依 item id 調整數量，delta 可正可負，負數時記錄扣減紀錄。"""
    items = load_items()
    target = None
    for it in items:
        if it["id"] == item_id:
            it["qty"] = max(0, it["qty"] + delta)
            target = it
    if target is None:
        return None, None
    save_items(items)
    log = load_log()
    if delta < 0:
        log = push_log(target["name"], abs(delta))
    return items, log


def apply_delta_by_position(position: int, amount: int):
    """依畫面上第 N 個項目(1-based)扣減 amount 次(每次 -1)。"""
    items = load_items()
    if position < 1 or position > len(items):
        return None, None, None
    item = items[position - 1]
    item["qty"] = max(0, item["qty"] - amount)
    save_items(items)
    log = push_log(item["name"], amount)
    return items, log, item


# ---------- 代儲交易記錄：資料存取 ----------
def today_str():
    return datetime.now(TW_TZ).strftime("%Y-%m-%d")


def now_str():
    return datetime.now(TW_TZ).strftime("%H:%M")


def load_rate():
    raw = redis.get(SALE_RATE_KEY)
    return json.loads(raw) if raw else None


def save_rate(rmb_rate: float, buy_rate: float):
    rate = {"date": today_str(), "rmb_rate": rmb_rate, "buy_rate": buy_rate}
    redis.set(SALE_RATE_KEY, json.dumps(rate, ensure_ascii=False))
    return rate


def load_transactions():
    raw = redis.get(SALE_TX_KEY)
    return json.loads(raw) if raw else []


def save_transactions(tx_list):
    redis.set(SALE_TX_KEY, json.dumps(tx_list, ensure_ascii=False))


def compute_cost(usd: float, buy_rate: float, rmb_rate: float):
    return round(usd * buy_rate * rmb_rate, 2)


def add_transaction(customer: str, received: float, usd: float):
    """新增一筆代儲交易，使用目前設定的匯率計算成本與淨賺。"""
    rate = load_rate()
    if rate is None:
        return None, None
    cost = compute_cost(usd, rate["buy_rate"], rate["rmb_rate"])
    profit = round(received - cost, 2)
    tx = {
        "id": str(uuid.uuid4())[:8],
        "date": today_str(),
        "time": now_str(),
        "customer": customer,
        "received": received,
        "usd": usd,
        "rmb_rate": rate["rmb_rate"],
        "buy_rate": rate["buy_rate"],
        "cost": cost,
        "profit": profit,
    }
    tx_list = load_transactions()
    tx_list.append(tx)
    tx_list = tx_list[-SALE_TX_MAX:]
    save_transactions(tx_list)
    return tx, tx_list


def get_today_summary():
    date = today_str()
    today_tx = [t for t in load_transactions() if t["date"] == date]
    total_received = round(sum(t["received"] for t in today_tx), 2)
    total_cost = round(sum(t["cost"] for t in today_tx), 2)
    total_profit = round(sum(t["profit"] for t in today_tx), 2)
    return {
        "date": date,
        "rate": load_rate(),
        "transactions": list(reversed(today_tx)),
        "count": len(today_tx),
        "total_received": total_received,
        "total_cost": total_cost,
        "total_profit": total_profit,
    }


def format_summary_text(summary):
    if summary["count"] == 0:
        return f"📅 {summary['date']} 目前還沒有交易紀錄。"
    lines = [f"📅 {summary['date']} 日結"]
    for t in summary["transactions"]:
        lines.append(f"{t['time']} {t['customer']}：收{t['received']}／賺{t['profit']}")
    lines.append(f"\n共 {summary['count']} 筆，總收款 {summary['total_received']}，總成本 {summary['total_cost']}，總淨賺 {summary['total_profit']}")
    return "\n".join(lines)


class AdjustBody(BaseModel):
    delta: int


class UpdateItemBody(BaseModel):
    name: str | None = None
    qty: int | None = None


class AddItemBody(BaseModel):
    name: str = "新項目"
    qty: int = 0


class SetRateBody(BaseModel):
    rmb_rate: float
    buy_rate: float


class AddTransactionBody(BaseModel):
    customer: str
    received: float
    usd: float


# ---------- 庫存 API（給 Mini App 網頁呼叫）----------
@app.get("/api/items")
def get_items():
    return {"items": load_items(), "log": load_log()}


@app.post("/api/items")
def add_item(body: AddItemBody):
    items = load_items()
    new_item = {"id": str(uuid.uuid4())[:8], "name": body.name, "qty": body.qty}
    items.append(new_item)
    save_items(items)
    return {"items": items}


@app.patch("/api/items/{item_id}")
def update_item(item_id: str, body: UpdateItemBody):
    items = load_items()
    found = False
    for it in items:
        if it["id"] == item_id:
            if body.name is not None:
                it["name"] = body.name
            if body.qty is not None:
                it["qty"] = max(0, body.qty)
            found = True
    if not found:
        raise HTTPException(status_code=404, detail="item not found")
    save_items(items)
    return {"items": items}


@app.post("/api/items/{item_id}/adjust")
def adjust_item(item_id: str, body: AdjustBody):
    items, log = apply_delta_by_id(item_id, body.delta)
    if items is None:
        raise HTTPException(status_code=404, detail="item not found")
    return {"items": items, "log": log}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str):
    items = [it for it in load_items() if it["id"] != item_id]
    save_items(items)
    return {"items": items}


# ---------- 代儲交易記錄 API（給 Mini App 網頁呼叫）----------
@app.get("/api/sales/today")
def api_get_today_summary():
    return get_today_summary()


@app.post("/api/sales/rate")
def api_set_rate(body: SetRateBody):
    rate = save_rate(body.rmb_rate, body.buy_rate)
    return {"rate": rate}


@app.post("/api/sales/transactions")
def api_add_transaction(body: AddTransactionBody):
    tx, tx_list = add_transaction(body.customer, body.received, body.usd)
    if tx is None:
        raise HTTPException(status_code=400, detail="今天還沒設定匯率")
    return get_today_summary()


@app.delete("/api/sales/transactions/{tx_id}")
def api_delete_transaction(tx_id: str):
    tx_list = [t for t in load_transactions() if t["id"] != tx_id]
    save_transactions(tx_list)
    return get_today_summary()


# ---------- Telegram Webhook ----------
@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if TELEGRAM_WEBHOOK_SECRET and x_telegram_bot_api_secret_token != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="invalid secret token")

    update = await request.json()
    message = update.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text == "/start":
        await tg_send_message(
            chat_id,
            "歡迎使用庫存記帳！點左下角的選單按鈕開啟介面，或輸入 /list 查看目前庫存。\n"
            "也可以直接打 -N 扣第 N 個項目 1 個，例如 -2；打 -N*M 扣 M 個，例如 -1*4。\n\n"
            "代儲記帳：先設定今日匯率 /匯率 人民幣匯率 買進匯率，例如 /匯率 4.36 5.05；\n"
            "之後直接打「客戶 收款 美金價格」新增一筆，例如 柯宇 2000 86；\n"
            "打 /日結 查看今天總覽。",
        )
    elif text == "/list":
        items = load_items()
        await tg_send_message(chat_id, format_items_text(items))
    elif text == "/日結":
        summary = get_today_summary()
        await tg_send_message(chat_id, format_summary_text(summary))
    else:
        rate_match = re.fullmatch(r"/匯率\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)", text.strip())
        adjust_match = re.fullmatch(r"-(\d+)(?:\*(\d+))?", text.strip())
        tx_match = re.fullmatch(r"(\S+)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)", text.strip())

        if rate_match:
            rmb_rate = float(rate_match.group(1))
            buy_rate = float(rate_match.group(2))
            save_rate(rmb_rate, buy_rate)
            await tg_send_message(chat_id, f"已設定今日匯率：人民幣 {rmb_rate}／買進 {buy_rate}")
        elif adjust_match:
            position = int(adjust_match.group(1))
            amount = int(adjust_match.group(2)) if adjust_match.group(2) else 1
            items, log, item = apply_delta_by_position(position, amount)
            if items is None:
                await tg_send_message(chat_id, f"沒有第 {position} 個項目，目前只有 {len(load_items())} 個。")
            else:
                await tg_send_message(
                    chat_id,
                    f"已扣減「{item['name']}」{amount} 個，剩餘 {item['qty']}。",
                )
        elif tx_match:
            customer = tx_match.group(1)
            received = float(tx_match.group(2))
            usd = float(tx_match.group(3))
            tx, tx_list = add_transaction(customer, received, usd)
            if tx is None:
                await tg_send_message(chat_id, "今天還沒設定匯率，先打 /匯率 人民幣匯率 買進匯率，例如 /匯率 4.36 5.05")
            else:
                summary = get_today_summary()
                await tg_send_message(
                    chat_id,
                    f"已記錄「{customer}」：收款 {received}，成本 {tx['cost']}，淨賺 {tx['profit']}\n"
                    f"今日累計 {summary['count']} 筆，總淨賺 {summary['total_profit']}",
                )
        elif "web_app_data" in message:
            # 保留相容：如果之後有地方還是用 sendData 送資料回來
            try:
                data = json.loads(message["web_app_data"]["data"])
                if "items" in data:
                    save_items(data["items"])
                await tg_send_message(chat_id, "已收到並更新庫存資料 ✅")
            except Exception:
                await tg_send_message(chat_id, "收到資料，但格式無法解析。")

    return {"ok": True}


@app.get("/")
def health():
    return {"status": "ok"}
