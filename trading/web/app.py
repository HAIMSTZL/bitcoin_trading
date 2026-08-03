"""Web 面板：FastAPI + WebSocket 实时推送交易状态。"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="Gate 现货网格交易系统")


def _engine():
    return app.state.engine


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(_engine().state())


@app.post("/api/control")
async def api_control(req: dict) -> JSONResponse:
    """运行控制：action = start（开始/恢复）| pause（暂停）| stop（停止交易循环）。

    stop 只停止交易引擎，Web 服务保持运行，之后仍可通过 start 重新启动；
    彻底退出进程请在终端 Ctrl+C。
    """
    engine = _engine()
    action = (req or {}).get("action")
    if action in ("start", "resume"):
        status = engine.start()
    elif action == "pause":
        status = engine.pause()
    elif action == "stop":
        status = engine.shutdown()
    else:
        return JSONResponse(
            {"ok": False, "error": f"未知操作: {action}"}, status_code=400
        )
    return JSONResponse({"ok": True, "run_status": status})


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            state = _engine().state()
            # 前端增量更新用：WS 推送不含完整历史，减小流量
            state.pop("equity_history", None)
            await websocket.send_text(json.dumps(state))
            await asyncio.sleep(2)
    except (WebSocketDisconnect, RuntimeError):
        pass
