"""Web 面板：FastAPI + WebSocket 实时推送交易状态。

多策略架构：app.state.engines = {策略名: Engine}，
所有接口通过 ?s=策略名 或请求体 strategy 字段路由。
"""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="MSTZL 现货网格交易系统")


def _engines() -> dict:
    return app.state.engines


def _pick(name: str | None):
    engines = _engines()
    if name and name in engines:
        return engines[name]
    return engines.get(app.state.default) or next(iter(engines.values()))


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/strategies")
async def api_strategies() -> JSONResponse:
    return JSONResponse({
        "default": app.state.default,
        "list": [{"name": n, "label": e.profile.label}
                 for n, e in _engines().items()],
    })


@app.get("/api/state")
async def api_state(s: str | None = None) -> JSONResponse:
    return JSONResponse(_pick(s).state())


@app.post("/api/control")
async def api_control(req: dict) -> JSONResponse:
    """运行控制：action = start（开始/恢复）| pause（暂停）| stop（停止交易循环）。

    stop 只停止交易引擎，Web 服务保持运行，之后仍可通过 start 重新启动；
    彻底退出进程请在终端 Ctrl+C。
    """
    engine = _pick((req or {}).get("strategy"))
    action = (req or {}).get("action")
    try:
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
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500
        )
    return JSONResponse({"ok": True, "run_status": status})


@app.get("/api/settings")
async def api_settings() -> JSONResponse:
    from trading import settings
    return JSONResponse(settings.current())


@app.post("/api/settings")
async def api_settings_update(req: dict) -> JSONResponse:
    """修改运行参数：即时生效并持久化到本地（对所有策略全局生效）。"""
    from trading import settings
    try:
        accepted = settings.apply((req or {}).get("updates", {}))
    except (ValueError, TypeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "accepted": accepted})


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            data = {}
            for name, engine in _engines().items():
                state = engine.state()
                # WS 推送不含完整权益历史，减小流量
                state.pop("equity_history", None)
                data[name] = state
            await websocket.send_text(json.dumps({
                "default": app.state.default,
                "list": [{"name": n, "label": e.profile.label}
                         for n, e in _engines().items()],
                "data": data,
            }))
            await asyncio.sleep(2)
    except (WebSocketDisconnect, RuntimeError):
        pass
