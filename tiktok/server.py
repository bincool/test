import asyncio
import contextlib
import json
import os
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, CommentEvent, GiftEvent, FollowEvent, JoinEvent
from TikTokLive.client.web.routes.fetch_room_id_live_html import FetchRoomIdLiveHTMLRoute
from TikTokLive.client.errors import UserOfflineError

# —— 历史文件路径
HISTORY_FILES = {"gift": "history_gifts.jsonl"}

# —— 动态加载需提示音礼物 ID
ALERT_GIFT_IDS = set()
_alert_cfg = os.path.join(os.path.dirname(__file__), "static", "alert_gifts.json")
try:
    with open(_alert_cfg, encoding="utf-8") as f:
        ALERT_GIFT_IDS = {g["gift_id"] for g in json.load(f).get("alertGifts", [])}
    print(f"已从 {_alert_cfg} 读取到 ALERT_GIFT_IDS={ALERT_GIFT_IDS}")
except Exception as e:
    print(f"⚠️ 无法读取 alert_gifts.json: {e}")

# —— 全局状态 ——
new_targets: asyncio.Queue[str]
total_diamonds = 0

# —— FastAPI + Socket.IO 初始化 ——
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = FastAPI()

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global new_targets
    new_targets = asyncio.Queue()
    task = asyncio.create_task(tiktok_manager())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

app.router.lifespan_context = lifespan

async def tiktok_manager():
    global total_diamonds
    current_client = None
    current_task   = None
    loop = asyncio.get_running_loop()

    while True:
        # 等待新目标
        unique_id = await new_targets.get()

        # —— 切换直播间时重置钻石 ——
        total_diamonds = 0
        await sio.emit("diamonds", {"total_diamonds": total_diamonds})
        await sio.emit("listening", {"target": unique_id})

        # 停掉旧客户端
        if current_client and current_task:
            await current_client.disconnect()
            try:
                await current_task
            except Exception:
                pass

        # 新建客户端 & 抓房间 ID
        client  = TikTokLiveClient(unique_id=unique_id)
        fetcher = FetchRoomIdLiveHTMLRoute(client._web)
        while True:
            try:
                await fetcher(unique_id)
                break
            except UserOfflineError:
                await asyncio.sleep(5)

        # 注册事件
        @client.on(ConnectEvent)
        async def on_connect(evt):
            await sio.emit("connect", {"msg": "connected"})

        @client.on(CommentEvent)
        async def on_comment(evt):
            await sio.emit("comment", {
                "nickname": getattr(evt.user, "nickname", "匿名用户"),
                "comment":  evt.comment
            })

        @client.on(GiftEvent)
        async def on_gift(evt):
            global total_diamonds
            gift         = evt.gift
            streakable   = getattr(gift, "streakable", False)
            in_streaking = getattr(evt, "streaking", False)

            # —— 连击完成时 ——
            if streakable and not in_streaking:
                cnt         = evt.repeat_count or 1
                diamond_cnt = (gift.diamond_count or 0) * cnt
                total_diamonds += diamond_cnt
                data = {
                    "nickname":       getattr(evt.user, "nickname", "匿名用户"),
                    "gift_id":        gift.id,
                    "gift_name":      gift.name or f"ID:{gift.id}",
                    "combo_count":    cnt,
                    "combo_diamond":  diamond_cnt,
                    "total_diamonds": total_diamonds
                }
                # if gift.id in ALERT_GIFT_IDS:
                with open(HISTORY_FILES["gift"], "a", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False) + "\n")
                await sio.emit("combo_gift", data)
                await sio.emit("diamonds", {"total_diamonds": total_diamonds})

            # —— 普通一次性礼物 ——
            elif not streakable:
                cnt         = evt.repeat_count or 1
                diamond_cnt = (gift.diamond_count or 0) * cnt
                total_diamonds += diamond_cnt
                data = {
                    "nickname":       getattr(evt.user, "nickname", "匿名用户"),
                    "gift_id":        gift.id,
                    "gift_name":      gift.name or f"ID:{gift.id}",
                    "count":          cnt,
                    "diamond":        diamond_cnt,
                    "total_diamonds": total_diamonds
                }
                # if gift.id in ALERT_GIFT_IDS:
                with open(HISTORY_FILES["gift"], "a", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False) + "\n")
                await sio.emit("gift", data)
                await sio.emit("diamonds", {"total_diamonds": total_diamonds})

        @client.on(FollowEvent)
        async def on_follow(evt):
            await sio.emit("follow", {"nickname": getattr(evt.user, "nickname", "匿名用户")})

        @client.on(JoinEvent)
        async def on_join(evt):
            await sio.emit("join", {"nickname": getattr(evt.user, "nickname", "匿名用户")})

        # 启动异步客户端
        current_client = client
        current_task   = asyncio.create_task(client.start())

@app.post("/api/listen")
async def api_listen(req: Request):
    body = await req.json()
    raw  = body.get("target", "").strip()
    if not raw:
        raise HTTPException(422, "请提供 target")
    if raw.startswith("http"):
        raw = raw.split("@",1)[1].split("/",1)[0]
    raw = raw.lstrip("@")
    await new_targets.put(raw)
    return {"status":"ok","listening":raw}

@app.get("/api/status")
async def api_status():
    return {"queue_size": new_targets.qsize()}

# 导出 ASGI 应用
asgi_app = socketio.ASGIApp(
    socketio_server=sio,
    other_asgi_app=app,
    socketio_path="ws/socket.io",
    static_files={}  # 禁用 engine.io 自带静态
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:asgi_app", host="0.0.0.0", port=8000, reload=True)
