import json
import os
import time
import uuid

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


def push_log(name: str):
    log = load_log()
    log.insert(0, {"name": name, "time": time.strftime("%m/%d %H:%M")})
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
    lines = [f"{it['name']}：{it['qty']}" for it in items]
    return "📦 目前庫存\n" + "\n".join(lines)


# ---------- API models ----------
class AdjustBody(BaseModel):
    delta: int


class RenameBody(BaseModel):
    name: str


class AddItemBody(BaseModel):
    name: str = "新項目"
    qty: int = 0


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
def rename_item(item_id: str, body: RenameBody):
    items = load_items()
    for it in items:
        if it["id"] == item_id:
            it["name"] = body.name
    save_items(items)
    return {"items": items}


@app.post("/api/items/{item_id}/adjust")
def adjust_item(item_id: str, body: AdjustBody):
    items = load_items()
    target = None
    for it in items:
        if it["id"] == item_id:
            it["qty"] = max(0, it["qty"] + body.delta)
            target = it
    if target is None:
        raise HTTPException(status_code=404, detail="item not found")
    save_items(items)
    log = load_log()
    if body.delta < 0:
        log = push_log(target["name"])
    return {"items": items, "log": log}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str):
    items = [it for it in load_items() if it["id"] != item_id]
    save_items(items)
    return {"items": items}


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
            "歡迎使用庫存記帳！點左下角的選單按鈕開啟介面，或輸入 /list 查看目前庫存。",
        )
    elif text == "/list":
        items = load_items()
        await tg_send_message(chat_id, format_items_text(items))
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
