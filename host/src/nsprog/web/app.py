"""Local web UI: ``nsprog web`` then open http://127.0.0.1:8765 .

One programmer session and one job at a time. Jobs run in a worker thread;
the page follows them through a WebSocket (``/ws``) and falls back to polling.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import threading
import time
import traceback
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .. import __version__, chipdb, jobs
from ..device import Device, connect
from ..flash import FlashDriver, detect, set_spi_clock
from ..link import find_ft232h_url, list_serial_ports

log = logging.getLogger(__name__)
STATIC = os.path.join(os.path.dirname(__file__), "static")


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.dev: Optional[Device] = None
        self.port: Optional[str] = None
        self.nand: Optional[FlashDriver] = None
        self.spi: Optional[FlashDriver] = None
        self.detect_msgs = []
        self.job: Optional[dict] = None
        self.cancel = threading.Event()
        self.result_path: Optional[str] = None
        self.version = 0          # bumped on every change, for the WebSocket

    def bump(self):
        self.version += 1

    def snapshot(self) -> dict:
        return {
            "connected": self.dev is not None,
            "port": self.port,
            "info": self.dev.info.__dict__ if self.dev and self.dev.info else None,
            "baud": self.dev.link.baudrate if self.dev else None,
            "nand": self.nand.info() if self.nand else None,
            "spi": self.spi.info() if self.spi else None,
            "messages": self.detect_msgs,
            "job": self.job,
            "version": __version__,
        }


def create_app(default_port: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="nsprog")
    st = State()
    app.state.nsprog = st

    # ------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
            return f.read()

    @app.get("/api/state")
    def state():
        return st.snapshot()

    @app.get("/api/ports")
    def ports():
        out = [{"device": p["device"], "description": p["description"], "likely": p["likely"]}
               for p in list_serial_ports()]
        if find_ft232h_url():
            out.insert(0, {"device": "ft232h", "description": "FT232H FIFO (fast)", "likely": True})
        out.append({"device": "emu", "description": "Software emulator (no hardware)", "likely": False})
        out.append({"device": "emu:spinand", "description": "Emulator with SPI NAND", "likely": False})
        return {"ports": out, "default": default_port}

    @app.get("/api/chips")
    def chips(q: str = "", type: str = ""):
        ql = q.lower()
        rows = [c for c in chipdb.all_chips()
                if (not type or c["type"] == type) and (not ql or ql in c["name"].lower() or ql in c["id"].lower())]
        return {"chips": rows[:500]}

    # ------------------------------------------------------------ session
    def _busy():
        return st.job is not None and st.job.get("state") == "running"

    @app.post("/api/connect")
    async def api_connect(req: Request):
        body = await req.json()
        port = body.get("port") or None
        if _busy():
            raise HTTPException(409, "a job is running")

        def work():
            with st.lock:
                if st.dev:
                    st.dev.close()
                    st.dev = None
                st.nand = st.spi = None
                st.detect_msgs = []
                st.dev = connect(port, negotiate=not body.get("slow_uart"))
                st.port = port or st.dev.link.name
                st.bump()
        try:
            await asyncio.get_running_loop().run_in_executor(None, work)
        except Exception as e:
            raise HTTPException(400, str(e))
        return st.snapshot()

    @app.post("/api/disconnect")
    def api_disconnect():
        if _busy():
            raise HTTPException(409, "a job is running")
        with st.lock:
            if st.dev:
                st.dev.close()
            st.dev = None
            st.nand = st.spi = None
            st.bump()
        return st.snapshot()

    @app.post("/api/detect")
    async def api_detect(req: Request):
        body = await req.json()
        if not st.dev:
            raise HTTPException(400, "not connected")
        if _busy():
            raise HTTPException(409, "a job is running")

        def work():
            with st.lock:
                det = detect(st.dev, nand_chip=body.get("nand_chip") or None,
                             spi_chip=body.get("spi_chip") or None,
                             use_rb=not body.get("no_rb"), ecc=bool(body.get("ecc")))
                st.nand, st.spi, st.detect_msgs = det.nand, det.spi, det.messages
                st.bump()
        try:
            await asyncio.get_running_loop().run_in_executor(None, work)
        except Exception as e:
            raise HTTPException(400, str(e))
        return st.snapshot()

    # ------------------------------------------------------------ jobs
    def _driver(target: str) -> FlashDriver:
        drv = st.nand if target == "nand" else st.spi
        if drv is None:
            raise HTTPException(400, "no %s chip detected" % target)
        return drv

    def _start(op: str, target: str, fn, *, result_file: Optional[str] = None):
        if _busy():
            raise HTTPException(409, "a job is running")
        st.cancel.clear()
        st.job = {"op": op, "target": target, "state": "running", "phase": "", "done": 0,
                  "total": 0, "messages": [], "report": None, "started": time.time(),
                  "download": None}
        st.bump()

        def progress(phase, done, total):
            st.job.update(phase=phase, done=done, total=total)
            st.bump()

        def message(text):
            st.job["messages"].append(text)
            st.bump()

        ctx = jobs.Context(progress=progress, cancel=st.cancel, message=message)

        def run():
            try:
                with st.lock:
                    rep = fn(ctx)
                st.job["report"] = rep.as_dict()
                st.job["state"] = "done" if rep.ok else "failed"
                if result_file and rep.ok:
                    st.result_path = result_file
                    st.job["download"] = "/api/result"
            except jobs.Cancelled:
                st.job["state"] = "cancelled"
            except Exception as e:
                log.debug("job failed", exc_info=True)
                st.job["state"] = "failed"
                st.job["messages"].append("error: %s" % e)
                st.job["trace"] = traceback.format_exc()
            st.bump()
        threading.Thread(target=run, daemon=True).start()
        return st.snapshot()

    def _q(req: Request, name, default=None, conv=str):
        v = req.query_params.get(name)
        if v in (None, ""):
            return default
        return conv(v)

    def _bool(v):
        return str(v).lower() in ("1", "true", "yes", "on")

    def _common(req: Request):
        target = _q(req, "target", "nand")
        drv = _driver(target)
        if drv.kind != "nand":
            set_spi_clock(st.dev, _q(req, "spi_mhz", 6.75, float))
        start = _q(req, "start", 0, int)
        count = _q(req, "count", None, int)
        return target, drv, start, count

    @app.post("/api/job/read")
    def job_read(req: Request):
        target, drv, start, count = _common(req)
        oob = _bool(_q(req, "oob", "1"))
        bb = _q(req, "bb", "keep")
        allow = _bool(_q(req, "allow_1v8", "0"))
        fd, path = tempfile.mkstemp(prefix="nsprog-", suffix=".bin")
        os.close(fd)

        def fn(ctx):
            with open(path, "wb") as f:
                return jobs.read(drv, f, start=start, count=count, oob=oob, bb=bb, ctx=ctx,
                                 allow_1v8=allow)
        return _start("read", target, fn, result_file=path)

    @app.post("/api/job/write")
    async def job_write(req: Request):
        data = await req.body()
        if not data:
            raise HTTPException(400, "empty file")
        target, drv, start, _ = _common(req)
        oob = _q(req, "oob", None)
        oob = None if oob in (None, "auto") else _bool(oob)
        bb = _q(req, "bb", "skip")
        erase = _bool(_q(req, "erase", "1"))
        verify = _bool(_q(req, "verify", "1"))
        allow = _bool(_q(req, "allow_1v8", "0"))
        if bb == "force" and not _bool(_q(req, "confirm", "0")):
            raise HTTPException(400, "bad-block mode 'force' needs confirmation")
        return _start("write", target, lambda ctx: jobs.write(
            drv, data, start=start, oob=oob, bb=bb, erase=erase, verify=verify, ctx=ctx,
            allow_1v8=allow))

    @app.post("/api/job/verify")
    async def job_verify(req: Request):
        data = await req.body()
        target, drv, start, _ = _common(req)
        oob = _q(req, "oob", None)
        oob = None if oob in (None, "auto") else _bool(oob)
        bb = _q(req, "bb", "skip")
        return _start("verify", target, lambda ctx: jobs.verify(drv, data, start=start, oob=oob,
                                                                 bb=bb, ctx=ctx))

    @app.post("/api/job/erase")
    def job_erase(req: Request):
        target, drv, start, count = _common(req)
        bb = _q(req, "bb", "skip")
        allow = _bool(_q(req, "allow_1v8", "0"))
        if bb == "force" and not _bool(_q(req, "confirm", "0")):
            raise HTTPException(400, "bad-block mode 'force' needs confirmation")
        return _start("erase", target, lambda ctx: jobs.erase(drv, start=start, count=count, bb=bb,
                                                               ctx=ctx, allow_1v8=allow))

    @app.post("/api/job/blank")
    def job_blank(req: Request):
        target, drv, start, count = _common(req)
        return _start("blank", target, lambda ctx: jobs.blank_check(drv, start=start, count=count,
                                                                     ctx=ctx))

    @app.post("/api/job/badblocks")
    def job_bad(req: Request):
        target, drv, _, _ = _common(req)
        return _start("badblocks", target, lambda ctx: jobs.bad_block_report(drv, ctx=ctx))

    @app.post("/api/job/cancel")
    def job_cancel():
        st.cancel.set()
        return st.snapshot()

    @app.get("/api/result")
    def result():
        if not st.result_path or not os.path.exists(st.result_path):
            raise HTTPException(404, "no result")
        name = "nsprog-%s.bin" % time.strftime("%Y%m%d-%H%M%S")
        return FileResponse(st.result_path, filename=name, media_type="application/octet-stream")

    # ------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        seen = -1
        try:
            while True:
                if st.version != seen:
                    seen = st.version
                    await sock.send_json(st.snapshot())
                await asyncio.sleep(0.15)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app


def serve(host="127.0.0.1", port=8765, open_browser=True, default_port=None) -> None:
    import uvicorn

    app = create_app(default_port)
    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port)
    print("nsprog web UI: %s  (Ctrl+C to stop)" % url)
    if open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
