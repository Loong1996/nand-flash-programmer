"""Local web UI: ``nsprog web`` then open http://127.0.0.1:8765 .

One programmer session and one job at a time. Jobs run in a worker thread;
the page follows them through a WebSocket (``/ws``) and falls back to polling.
History and settings are kept in ``~/.nsprog`` (or ``$NSPROG_HOME``).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import traceback
import zipfile
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response

from .. import __version__, chipdb, config, jobs
from ..device import Device, connect
from ..flash import FlashDriver, ParallelNand, SpiNor, detect, set_spi_clock
from ..link import find_ft232h_url, list_serial_ports

log = logging.getLogger(__name__)
STATIC = os.path.join(os.path.dirname(__file__), "static")

DEFAULT_SETTINGS = {"theme": "auto", "spi_mhz": 6.75, "fast_uart": True, "use_rb": True,
                    "spinand_ecc": False, "verify": True, "erase": True,
                    "nand_timing": "safe", "spi_quad": "auto"}


home_dir = config.home_dir
_load_json = config.load_json
_save_json = config.save_json


def _zip_tree(root: str, zpath: str) -> None:
    """Zip a directory tree, keeping symlinks as links."""
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, dirs, files in os.walk(root):
            for name in dirs + files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                if os.path.islink(full):
                    info = zipfile.ZipInfo(rel)
                    info.external_attr = 0o120777 << 16
                    z.writestr(info, os.readlink(full))
                elif os.path.isdir(full):
                    z.write(full, rel + "/")
                else:
                    z.write(full, rel)


class ToolReport(jobs.Report):
    """Report for offline tools: carries preformatted text."""

    def __init__(self, operation: str, ok: bool, text: str, **extra):
        super().__init__(operation)
        self.ok = ok
        self.text = text
        self.extra.update(extra)

    def summary(self) -> str:
        return self.text


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
        self.result_name: str = "nsprog.bin"
        self.result_meta: dict = {}
        self.badmap: dict = {}          # target -> {"blocks": n, "bad": [...], "new": [...], "time": t}
        self.pins: Optional[dict] = None
        self.pintest = 0                # PIN_TEST mode currently set on the FPGA
        self.fsview: list = []          # file systems of the last "browse" tool run (for downloads)
        self.version = 0
        self.settings = dict(DEFAULT_SETTINGS, **_load_json("settings.json", {}))
        self.history = _load_json("history.json", [])

    def bump(self):
        self.version += 1

    def snapshot(self) -> dict:
        info = self.dev.info.__dict__ if self.dev and self.dev.opened else None
        return {
            "connected": self.dev is not None,
            "port": self.port,
            "link": ("FT232H" if info and info.get("port") else "UART") if info else None,
            "info": info,
            "baud": self.dev.link.baudrate if self.dev else None,
            "nand": self.nand.info() if self.nand else None,
            "spi": self.spi.info() if self.spi else None,
            "messages": self.detect_msgs,
            "job": self.job,
            "result": dict(self.result_meta, available=bool(self.result_path)) if self.result_path else None,
            "badmap": self.badmap,
            "pins": self.pins,
            "pintest": self.pintest,
            "settings": self.settings,
            "history_count": len(self.history),
            "version": __version__,
            "app_mode": app_mode(),
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
            out.insert(0, {"device": "ft232h-sync", "description": "FT232H sync FIFO (fastest)",
                           "likely": False})
            out.insert(0, {"device": "ft232h", "description": "FT232H FIFO (fast)", "likely": True})
        out.append({"device": "emu", "description": "Software emulator (no hardware)", "likely": False})
        out.append({"device": "emu:spinand", "description": "Emulator with SPI NAND", "likely": False})
        return {"ports": out, "default": default_port}

    @app.get("/api/chips")
    def chips(q: str = "", type: str = "", limit: int = 500):
        ql = q.lower()
        rows = [c for c in chipdb.all_chips()
                if (not type or c["type"] == type)
                and (not ql or ql in c["name"].lower() or ql in c["id"].lower())]
        return {"chips": rows[:limit], "total": len(rows)}

    @app.get("/api/settings")
    def get_settings():
        return st.settings

    @app.post("/api/settings")
    async def set_settings(req: Request):
        body = await req.json()
        for k, v in body.items():
            if k in DEFAULT_SETTINGS:
                st.settings[k] = v
        _save_json("settings.json", st.settings)
        st.bump()
        return st.settings

    @app.get("/api/history")
    def history():
        return {"history": list(reversed(st.history))}

    @app.delete("/api/history")
    def clear_history():
        st.history = []
        _save_json("history.json", st.history)
        st.bump()
        return {"history": []}

    # ------------------------------------------------------------ session
    def _need_dev() -> Device:
        if st.dev is None:
            raise HTTPException(400, "not connected")
        return st.dev

    def _busy():
        return st.job is not None and st.job.get("state") == "running"

    async def _in_thread(fn):
        try:
            return await asyncio.get_running_loop().run_in_executor(None, fn)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/connect")
    async def api_connect(req: Request):
        body = await req.json()
        port = body.get("port") or None
        if _busy():
            raise HTTPException(409, "a job is running")
        slow = body.get("slow_uart", not st.settings.get("fast_uart", True))

        def work():
            with st.lock:
                if st.dev:
                    st.dev.close()
                    st.dev = None
                st.nand = st.spi = None
                st.detect_msgs = []
                st.pins = None
                st.pintest = 0
                dev = connect(port, negotiate=not slow)
                st.dev = dev
                st.port = port or dev.link.name
                st.bump()
        await _in_thread(work)
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
            st.pins = None
            st.pintest = 0
            st.bump()
        return st.snapshot()

    @app.post("/api/detect")
    async def api_detect(req: Request):
        body = await req.json()
        if not st.dev:
            raise HTTPException(400, "not connected")
        if _busy():
            raise HTTPException(409, "a job is running")
        use_rb = not body.get("no_rb", not st.settings.get("use_rb", True))
        ecc = bool(body.get("ecc", st.settings.get("spinand_ecc", False)))

        def work():
            with st.lock:
                _pintest_off(_need_dev())
                det = detect(_need_dev(), nand_chip=body.get("nand_chip") or None,
                             spi_chip=body.get("spi_chip") or None, use_rb=use_rb, ecc=ecc)
                st.nand, st.spi, st.detect_msgs = det.nand, det.spi, det.messages
                st.bump()
        await _in_thread(work)
        return st.snapshot()

    @app.get("/api/pins")
    async def api_pins():
        if not st.dev:
            raise HTTPException(400, "not connected")
        if _busy():
            raise HTTPException(409, "a job is running")

        def work():
            with st.lock:
                p = _need_dev().pins()
                st.pins = {"rb_ready": p.rb_ready, "spi_do": p.spi_do, "nand_io": p.nand_io,
                           "ft_oe_n": p.ft_oe_n, "ft_siwu_n": p.ft_siwu_n,
                           "ft_clkout_active": p.ft_clkout_active, "port_ft": p.port_ft}
                st.bump()
                return st.pins
        return await _in_thread(work)

    # ------------------------------------------------------------ wiring / doctor
    def _pintest_off(dev: Device) -> None:
        """Hand the pins back to normal operation before any other command."""
        if st.pintest:
            from .. import doctor
            doctor.stop(dev)
            st.pintest = 0

    @app.get("/api/wiring")
    def api_wiring():
        from .. import wiring
        return {"packages": wiring.PACKAGES,
                "signals": [s.as_dict() for s in wiring.SIGNALS],
                "idle": wiring.idle_levels()}

    @app.post("/api/pintest")
    async def api_pintest(req: Request):
        """{pin, mode}: 0 off, 1 release all (live levels), 2 low, 3 high, 4 toggle."""
        from .. import doctor
        body = await req.json()
        mode = int(body.get("mode", 0))
        pin = int(body.get("pin", 0))
        if _busy():
            raise HTTPException(409, "a job is running")

        def work():
            with st.lock:
                dev = _need_dev()
                if not doctor.supports_pin_test(dev):
                    raise HTTPException(400, "gateware %s has no pin test; update it with "
                                             "'nsprog fpga-flash'" % dev.info.gw_version)
                lv = doctor.pin_test(dev, pin, mode)
                if st.pintest != mode:
                    st.pintest = mode
                    st.bump()
                return {"levels": lv, "mode": mode, "pin": pin}
        return await _in_thread(work)

    @app.post("/api/doctor")
    async def api_doctor(req: Request):
        from .. import doctor
        body = await req.json()
        if _busy():
            raise HTTPException(409, "a job is running")

        def work():
            with st.lock:
                dev = _need_dev()
                st.pintest = 0
                checks = doctor.run(dev, drive=bool(body.get("drive", True)))
                st.bump()
                return {"checks": [c.as_dict() for c in checks], "summary": doctor.summary(checks)}
        return await _in_thread(work)

    @app.get("/api/troubleshoot")
    def api_troubleshoot_list():
        from .. import troubleshoot as T
        return {"symptoms": [x.as_dict() for x in T.SYMPTOMS]}

    @app.post("/api/troubleshoot")
    async def api_troubleshoot(req: Request):
        from .. import troubleshoot as T
        body = await req.json()
        try:
            T.symptom(str(body.get("symptom", "")))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if _busy():
            raise HTTPException(409, "a job is running")

        def work():
            with st.lock:
                st.pintest = 0
                findings = T.diagnose(body["symptom"], st.dev)
                st.bump()
                return T.as_dict(body["symptom"], findings)
        return await _in_thread(work)

    @app.post("/api/ft232h/tune")
    async def api_ft232h_tune():
        from .. import ft232h as F
        if _busy():
            raise HTTPException(409, "a job is running")

        def work():
            with st.lock:
                dev = _need_dev()
                try:
                    if not F.phase_supported(dev):
                        raise ValueError("固件 %s 不支持调相，需要 1.3" % dev.info.gw_version)
                    rep = F.tune(dev)
                except ValueError as e:
                    raise HTTPException(400, str(e)) from None
                if rep.best is not None:
                    F.save_phase(dev.link.name, rep.best)
                st.bump()
                return {"results": [{"phase": p, "ok": err is None, "error": err} for p, err in rep.results],
                        "window": rep.window, "best": rep.best, "summary": rep.summary()}
        return await _in_thread(work)

    @app.post("/api/selftest")
    async def api_selftest():
        if not st.dev:
            raise HTTPException(400, "not connected")
        if _busy():
            raise HTTPException(409, "a job is running")
        import random

        from .. import protocol as P

        def work():
            with st.lock:
                rng = random.Random()
                t0 = time.monotonic()
                total = 0
                for _ in range(5):
                    b = P.Batch()
                    vals = [rng.randrange(256) for _ in range(500)]
                    rs = [b.echo(v) for v in vals]
                    x = b.spi_xfer(bytes(rng.randrange(256) for _ in range(4000)))
                    _need_dev().run(b)
                    if [r.value for r in rs] != vals:
                        raise RuntimeError("echo mismatch: link unreliable")
                    total += 1000 + 8000 + len(x.value)
                dt = time.monotonic() - t0
                return {"ok": True, "bytes": total, "seconds": dt, "rate": total / dt}
        return await _in_thread(work)

    # ------------------------------------------------------------ jobs
    def _driver(target: str) -> FlashDriver:
        drv = st.nand if target == "nand" else st.spi
        if drv is None:
            raise HTTPException(400, "no %s chip detected" % target)
        return drv

    def _record(op, target, name, rep_dict, state_):
        entry = {"op": op, "target": target, "chip": name, "state": state_,
                 "time": time.time(),
                 "seconds": (rep_dict or {}).get("seconds"),
                 "bytes": (rep_dict or {}).get("bytes"),
                 "summary": (rep_dict or {}).get("summary") or ""}
        st.history.append(entry)
        st.history = st.history[-500:]
        _save_json("history.json", st.history)

    def _start(op: str, target: str, fn, *, result_file: Optional[str] = None,
               result_name: str = "nsprog.bin", result_meta: Optional[dict] = None,
               chip_name: str = "", on_done=None):
        if _busy():
            raise HTTPException(409, "a job is running")
        st.cancel.clear()
        if st.dev is not None and target != "file":
            _pintest_off(st.dev)
        job: dict = {"op": op, "target": target, "chip": chip_name, "state": "running", "phase": "",
                     "done": 0, "total": 0, "messages": [], "report": None, "started": time.time(),
                     "download": None}
        st.job = job
        st.bump()

        def progress(phase, done, total):
            job.update(phase=phase, done=done, total=total)
            st.bump()

        def message(text):
            job["messages"].append(text)
            st.bump()

        ctx = jobs.Context(progress=progress, cancel=st.cancel, message=message)

        def run():
            # Everything else is filled in first; "state" changes last, because
            # clients treat a non-running state as "all results available".
            rep_dict = None
            try:
                with st.lock:
                    rep = fn(ctx)
                rep_dict = rep.as_dict()
                job["report"] = rep_dict
                if on_done:
                    on_done(rep)
                if result_file and rep.ok:
                    st.result_path = result_file
                    st.result_name = result_name
                    st.result_meta = dict(result_meta or {}, name=result_name,
                                          size=os.path.getsize(result_file))
                    job["download"] = "/api/result"
                final = "done" if rep.ok else "failed"
            except jobs.Cancelled:
                final = "cancelled"
            except Exception as e:
                log.debug("job failed", exc_info=True)
                job["messages"].append("error: %s" % e)
                job["trace"] = traceback.format_exc()
                final = "failed"
            job["finished"] = time.time()
            _record(op, target, chip_name, rep_dict, final)
            job["state"] = final
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
            set_spi_clock(_need_dev(), _q(req, "spi_mhz", st.settings.get("spi_mhz", 6.75), float))
        if isinstance(drv, ParallelNand):
            drv.set_timing(_q(req, "nand_timing", st.settings.get("nand_timing", "safe")))
        if isinstance(drv, SpiNor):
            drv.quad = _q(req, "spi_quad", st.settings.get("spi_quad", "auto"))
        start = _q(req, "start", 0, int)
        count = _q(req, "count", None, int)
        return target, drv, start, count

    def _tmp(suffix=".bin"):
        fd, path = tempfile.mkstemp(prefix="nsprog-", suffix=suffix)
        os.close(fd)
        return path

    @app.post("/api/job/read")
    def job_read(req: Request):
        target, drv, start, count = _common(req)
        oob = _bool(_q(req, "oob", "1")) and drv.spare_size > 0
        bb = _q(req, "bb", "keep")
        allow = _bool(_q(req, "allow_1v8", "0"))
        path = _tmp()
        name = "%s-%s.bin" % (drv.name.replace(" ", "_"), time.strftime("%Y%m%d-%H%M%S"))
        meta = {"chip": drv.name, "page": drv.page_size, "oob": drv.spare_size if oob else 0,
                "ppb": drv.pages_per_block, "start_block": start, "target": target}

        def fn(ctx):
            with open(path, "wb") as f:
                return jobs.read(drv, f, start=start, count=count, oob=oob, bb=bb, ctx=ctx,
                                 allow_1v8=allow)
        return _start("read", target, fn, result_file=path, result_name=name, result_meta=meta,
                      chip_name=drv.name)

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
            allow_1v8=allow), chip_name=drv.name)

    @app.post("/api/job/verify")
    async def job_verify(req: Request):
        data = await req.body()
        target, drv, start, _ = _common(req)
        oob = _q(req, "oob", None)
        oob = None if oob in (None, "auto") else _bool(oob)
        bb = _q(req, "bb", "skip")
        return _start("verify", target, lambda ctx: jobs.verify(drv, data, start=start, oob=oob,
                                                                 bb=bb, ctx=ctx), chip_name=drv.name)

    def _store_bad(target, drv):
        def done(rep):
            st.badmap[target] = {"blocks": drv.blocks, "bad": list(rep.bad_blocks),
                                 "new": list(rep.new_bad_blocks), "chip": drv.name,
                                 "time": time.time()}
        return done

    @app.post("/api/job/erase")
    def job_erase(req: Request):
        target, drv, start, count = _common(req)
        bb = _q(req, "bb", "skip")
        allow = _bool(_q(req, "allow_1v8", "0"))
        if bb == "force" and not _bool(_q(req, "confirm", "0")):
            raise HTTPException(400, "bad-block mode 'force' needs confirmation")
        return _start("erase", target, lambda ctx: jobs.erase(drv, start=start, count=count, bb=bb,
                                                               ctx=ctx, allow_1v8=allow),
                      chip_name=drv.name)

    @app.post("/api/job/blank")
    def job_blank(req: Request):
        target, drv, start, count = _common(req)
        return _start("blank", target, lambda ctx: jobs.blank_check(drv, start=start, count=count,
                                                                     ctx=ctx), chip_name=drv.name)

    @app.post("/api/job/badblocks")
    def job_bad(req: Request):
        target, drv, _, _ = _common(req)
        return _start("badblocks", target, lambda ctx: jobs.bad_block_report(drv, ctx=ctx),
                      chip_name=drv.name, on_done=_store_bad(target, drv))

    @app.post("/api/job/cancel")
    def job_cancel():
        st.cancel.set()
        return st.snapshot()

    # ------------------------------------------------------------ results
    @app.get("/api/result")
    def result():
        if not st.result_path or not os.path.exists(st.result_path):
            raise HTTPException(404, "no result")
        media = "application/zip" if st.result_name.endswith(".zip") else "application/octet-stream"
        return FileResponse(st.result_path, filename=st.result_name, media_type=media)

    @app.get("/api/result/bytes")
    def result_bytes(offset: int = 0, length: int = 4096):
        if not st.result_path or not os.path.exists(st.result_path):
            raise HTTPException(404, "no result")
        length = max(0, min(length, 1 << 20))
        with open(st.result_path, "rb") as f:
            f.seek(max(0, offset))
            data = f.read(length)
        return Response(content=data, media_type="application/octet-stream")

    @app.get("/api/result/find")
    def result_find(q: str, start: int = 0, hex: bool = False):
        if not st.result_path:
            raise HTTPException(404, "no result")
        try:
            needle = bytes.fromhex(q.replace(" ", "")) if hex else q.encode("utf-8")
        except ValueError:
            raise HTTPException(400, "not a hex string: %r" % q) from None
        if not needle:
            raise HTTPException(400, "empty search")
        with open(st.result_path, "rb") as f:
            f.seek(start)
            pos, carry, base = -1, b"", start
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                buf = carry + chunk
                i = buf.find(needle)
                if i >= 0:
                    pos = base - len(carry) + i
                    break
                carry = buf[-(len(needle) - 1):] if len(needle) > 1 else b""
                base += len(chunk)
        return {"offset": pos}

    # ------------------------------------------------------------ offline tools
    @app.post("/api/tools/{tool}")
    async def tools(tool: str, req: Request):
        data = await req.body()
        if not data:
            raise HTTPException(400, "upload a file first")
        src = _tmp()
        with open(src, "wb") as f:
            f.write(data)
        page = _q(req, "page", 2048, int)
        oob = _q(req, "oob", 64, int)
        ppb = _q(req, "ppb", 64, int)
        ecc_spec = _q(req, "ecc", "bch8")
        ecc_off = _q(req, "ecc_offset", None, int)
        fname = _q(req, "name", "image.bin")
        base = os.path.splitext(os.path.basename(fname))[0] or "image"

        from .. import image, ubi
        from ..ecc import layout

        geo = image.Geometry(page, oob, ppb)
        other = None
        if tool == "diff":
            split = _q(req, "split", None, int)
            if split is None or not 0 < split < len(data):
                raise HTTPException(400, "diff needs two files (image A followed by image B, split=len(A))")
            other = _tmp()
            with open(src, "wb") as f:
                f.write(data[:split])
            with open(other, "wb") as f:
                f.write(data[split:])

        def main_bytes() -> bytes:
            if oob > 0:
                buf = io.BytesIO()
                with open(src, "rb") as f:
                    image.strip_oob(f, buf, geo)
                return buf.getvalue()
            with open(src, "rb") as f:
                return f.read()

        def _tool_diff() -> ToolReport:
            from ..diff import diff_files

            assert other is not None
            drep = diff_files(src, other, geo)
            kind_zh = {"bitflip": "位翻转", "erased": "一侧为空", "changed": "内容不同"}
            tables = [] if drep.identical else [
                {"title": "有差异的块", "cols": ["块", "位翻转页", "一侧为空", "内容不同"],
                 "rows": [[b["block"], b["bitflip"], b["erased"], b["changed"]]
                          for b in drep.block_rows()[:500]]},
                {"title": "有差异的页（前 500）",
                 "cols": ["页", "块", "类型", "主数据字节", "OOB 字节", "1→0 位", "0→1 位"],
                 "rows": [[d.page, d.page // ppb, kind_zh[d.kind], d.main_bytes, d.oob_bytes, d.bits_10,
                           d.bits_01] for d in drep.diffs[:500]]}]
            return ToolReport("image diff", drep.identical, drep.summary(), tables=tables, kinds=drep.kinds)

        def _tool_scan() -> ToolReport:
            from ..scan import scan

            srep = scan(main_bytes())
            tables = [dict(srep.table(), title="发现")]
            if srep.partitions:
                tables.append(dict(srep.partition_table(), title="分区表"))
            if srep.env:
                tables.append({"title": "U-Boot 环境变量", "cols": ["变量", "值"],
                               "rows": [[k, v] for k, v in srep.env.items()]})
            return ToolReport("scan", True, srep.summary(), tables=tables)

        def _tool_fs(browse: bool) -> ToolReport:
            from .. import fs as fsmod

            found = fsmod.find_all(main_bytes())
            if not found:
                return ToolReport("file systems", False, "no SquashFS / JFFS2 / UBIFS / UBI found" +
                                  (" (is the OOB size right?)" if oob else " (raw image? set the OOB size)"))
            lines = []
            for fnd in found:
                lines.append("%s: %s" % (fnd.label, fnd.error or ", ".join(
                    "%s=%s" % kv for kv in (fnd.fs.info().items() if fnd.fs else []))))
            if browse:
                st.fsview = found
                rows: list = []
                for i, fnd in enumerate(found):
                    if fnd.fs is None:
                        continue
                    for e in fnd.fs.listing(20000):
                        cell: dict = {"text": e.path + (" → " + e.target if e.target else "")}
                        if e.kind == "file":
                            cell["href"] = "/api/fs/file?i=%d&path=%s" % (i, quote(e.path))
                        short = "%s @0x%X" % (fnd.fs.kind, fnd.offset)
                        rows.append([cell, e.size if e.kind == "file" else "", e.mode_str, short])
                return ToolReport("file systems", True, "\n".join(lines), tables=[
                    {"title": "文件（点文件名下载）", "cols": ["路径", "大小", "权限", "文件系统"],
                     "rows": rows, "wrap": 0}])
            tmpdir = tempfile.mkdtemp(prefix="nsprog-fs-")
            try:
                for fnd in found:
                    if fnd.fs is None:
                        continue
                    c = fnd.fs.extract(os.path.join(tmpdir, fnd.label))
                    lines.append("  %s: %d files, %d dirs, %d symlinks%s" % (
                        fnd.label, c["files"], c["dirs"], c["symlinks"],
                        ", %d errors" % c["errors"] if c["errors"] else ""))
                zpath = _tmp(".zip")
                _zip_tree(tmpdir, zpath)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
            set_result(zpath, base + "-files.zip")
            return ToolReport("file systems", True, "\n".join(lines))

        def fn(ctx):
            try:
                if tool == "diff":
                    return _tool_diff()
                if tool == "scan":
                    return _tool_scan()
                if tool in ("fs_list", "fs_extract"):
                    return _tool_fs(tool == "fs_list")
                if tool == "info":
                    with open(src, "rb") as f:
                        inf = image.info(f, geo, raw=oob > 0)
                    return ToolReport("image info", True, inf.summary(), bad_blocks=inf.bad_blocks,
                                      blocks=inf.blocks)
                if tool == "strip":
                    out = _tmp()
                    with open(src, "rb") as f, open(out, "wb") as o:
                        n = image.strip_oob(f, o, geo, skip_bad=_bool(_q(req, "skip_bad", "0")))
                    set_result(out, base + "-main.bin")
                    return ToolReport("strip OOB", True, "%d pages of main-area data" % n)
                if tool == "merge":
                    out = _tmp()
                    lay = layout(ecc_spec, ecc_off) if _q(req, "ecc") else None
                    with open(src, "rb") as f, open(out, "wb") as o:
                        n = image.merge(f, None, o, geo, lay)
                    set_result(out, base + "-raw.bin")
                    return ToolReport("add OOB", True, "%d raw pages%s" % (
                        n, (", ECC: " + lay.describe()) if lay else ""))
                if tool in ("ecc_check", "ecc_fix"):
                    lay = layout(ecc_spec, ecc_off)
                    out = _tmp() if tool == "ecc_fix" else None
                    strip = _bool(_q(req, "strip", "0"))
                    with open(src, "rb") as f:
                        fixed = open(out, "wb") if out else None
                        try:
                            rep = image.ecc_check(f, geo, lay, fixed, strip=strip)
                        finally:
                            if fixed:
                                fixed.close()
                    if out:
                        set_result(out, base + ("-fixed-main.bin" if strip else "-fixed.bin"))
                    return ToolReport("ECC " + ("fix" if out else "check"), rep.ok,
                                      lay.describe() + "\n" + rep.summary(),
                                      histogram=rep.histogram)
                if tool in ("ubi_info", "ubi_extract"):
                    with open(src, "rb") as f:
                        img = ubi.parse(f, _q(req, "peb", None, int))
                        text = img.summary()
                        if tool == "ubi_extract":
                            d = tempfile.mkdtemp(prefix="nsprog-ubi-")
                            paths = ubi.extract(f, img, d)
                            out = _tmp(".zip")
                            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                                for p in paths:
                                    z.write(p, os.path.basename(p))
                                    text += "\n  %s (%s)" % (os.path.basename(p), ubi.detect_content(p))
                            shutil.rmtree(d, ignore_errors=True)
                            set_result(out, base + "-ubi.zip")
                    return ToolReport("UBI", True, text)
                raise ValueError("unknown tool %r" % tool)
            finally:
                os.unlink(src)
                if other:
                    os.unlink(other)

        def set_result(path, name):
            st.result_path = path
            st.result_name = name
            st.result_meta = {"name": name, "size": os.path.getsize(path), "page": page,
                              "oob": 0 if name.endswith(("-main.bin", "-fixed-main.bin")) else oob,
                              "ppb": ppb, "target": "file", "chip": fname}
            if st.job is not None:
                st.job["download"] = "/api/result"

        return _start("tool:" + tool, "file", fn, chip_name=fname)

    @app.get("/api/fs/file")
    def fs_file(i: int, path: str):
        if not 0 <= i < len(st.fsview) or st.fsview[i].fs is None:
            raise HTTPException(404, "browse the image again (tools: 浏览文件系统)")
        for e in st.fsview[i].fs.entries():
            if e.path == path and e.kind == "file" and e.read:
                data = e.read()
                name = os.path.basename(path) or "file"
                disp = "attachment; filename*=UTF-8''%s" % quote(name)
                return Response(data, media_type="application/octet-stream",
                                headers={"Content-Disposition": disp})
        raise HTTPException(404, "no such file")

    @app.post("/api/quit")
    def api_quit():
        """Stop the server (only in desktop-app mode, where there is no terminal)."""
        if not app_mode():
            raise HTTPException(403, "only available in the desktop app")
        if _busy():
            raise HTTPException(409, "a job is running")
        if st.dev:
            st.dev.close()
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return {"ok": True}

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


def app_mode() -> bool:
    """Started as a desktop app (no terminal to press Ctrl+C in)."""
    return os.environ.get("NSPROG_APP") == "1"


def running_instance(port: int) -> Optional[str]:
    """URL of an nsprog web UI already listening on ``port``, else None."""
    import urllib.request

    url = "http://127.0.0.1:%d/" % port
    try:
        with urllib.request.urlopen(url + "api/state", timeout=0.5) as r:
            if "version" in json.loads(r.read().decode("utf-8")):
                return url
    except Exception:
        pass
    return None


def serve(host="127.0.0.1", port=8765, open_browser=True, default_port=None) -> None:
    import uvicorn

    app = create_app(default_port)
    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, port)
    print("nsprog web UI: %s  (Ctrl+C to stop)" % url)
    if open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
