"""Builds the engine (App) in a background thread and swaps it in when ready, so the web server starts at once,
keeps answering from the previous engine during rebuilds, and picks up new datasets and live rows."""
from __future__ import annotations

import logging
import threading
import time

from .assistant import Assistant
from .audit import Store
from .copilot import Copilot
from .datasets import Registry
from .state import App

log = logging.getLogger("rj.engine")


class Engine:
    def __init__(self, store: Store, registry: Registry, cache_dir, retrain_every_hours: int = 24,
                 live_seconds_per_hour: int = 3600):
        self.store, self.registry, self.cache_dir = store, registry, cache_dir
        self.retrain_every_hours = retrain_every_hours
        self.live_seconds_per_hour = live_seconds_per_hour
        self.core: App | None = None
        self.assistant: Assistant | None = None
        self.copilot = Copilot(None)
        self.state, self.message, self.error = "starting", "Starting the engine…", None
        self.version = 0
        self.building = False
        self.last_ingest: float | None = None
        self.last_build_seconds: float | None = None
        self._job: dict | None = None
        self._cv = threading.Condition()
        threading.Thread(target=self._worker, name="engine-builder", daemon=True).start()
        threading.Thread(target=self._watch_feed, name="feed-monitor", daemon=True).start()

    # ---------- requests ----------
    def request(self, dataset_id: str, refit: bool = True, reason: str = ""):
        """Queue a build. Several quick requests collapse into one; a refit request wins over a quick update."""
        with self._cv:
            if self._job and self._job["id"] == dataset_id:
                self._job["refit"] = self._job["refit"] or refit
            else:
                self._job = {"id": dataset_id, "refit": refit, "reason": reason}
            self._cv.notify()

    def after_ingest(self):
        """New live rows were stored: re-score on the current models, or refit when enough new hours have arrived."""
        self.last_ingest = time.time()
        core = self.core
        if not core:
            return
        if core.stale_reason:
            core.stale_reason = None
        rows = int(self.registry.get(core.dataset["id"]).get("rows") or len(core.df))
        refit = rows - core.fitted_rows >= self.retrain_every_hours
        self.request(core.dataset["id"], refit=refit, reason="retrain" if refit else "live update")

    @property
    def ready(self) -> bool:
        return self.core is not None

    def status(self) -> dict:
        c = self.core
        return {"state": self.state, "message": self.message, "error": self.error, "version": self.version,
                "building": self.building, "dataset": c.dataset if c else None,
                "rows": len(c.df) if c else None, "last_ts": c.ts_str[-1] if c else None,
                "live": bool(c and c.live), "stale": bool(c and c.stale_reason), "stale_reason": c.stale_reason if c else None,
                "last_ingest_age_s": round(time.time() - self.last_ingest, 1) if self.last_ingest else None,
                "fitted_rows": c.fitted_rows if c else None,
                "hours_until_retrain": max(0, self.retrain_every_hours - (len(c.df) - c.fitted_rows)) if c else None,
                "last_build_seconds": self.last_build_seconds}

    # ---------- background work ----------
    def _say(self, msg: str):
        self.message = msg

    def _worker(self):
        while True:
            with self._cv:
                while not self._job:
                    self._cv.wait()
                job, self._job = self._job, None
                self.building = True
            t0 = time.time()
            try:
                entry = self.registry.get(job["id"])
                if not entry:
                    raise ValueError(f"Dataset {job['id']} is not registered")
                prev = self.core if (not job["refit"] and self.core and self.core.dataset["id"] == job["id"]) else None
                if self.core is None:
                    self.state = "starting"
                log.info("building engine: dataset=%s refit=%s reason=%s", job["id"], job["refit"], job["reason"])
                app = App(self.registry.file(job["id"]), self.store, entry, self.cache_dir, prev=prev, progress=self._say)
                pv = app.policy_rec["version"]
                if self.store.current_policy()["version"] != pv:   # policy saved while we were building
                    app.save_policy_refresh()
                old = self.core
                assistant = Assistant(app)
                if self.assistant and old and old.dataset["id"] == app.dataset["id"]:
                    assistant.sessions = self.assistant.sessions
                self.core, self.assistant = app, assistant
                self.copilot.app = app
                if app.live and self.last_ingest is None:
                    self.last_ingest = time.time()
                self.version += 1
                self.state, self.error = "ready", None
                self.message = f"Ready · {entry['name']}"
                self.last_build_seconds = round(time.time() - t0, 1)
                self.store.set_kv("active_dataset", job["id"])
                if not old or old.dataset["id"] != app.dataset["id"]:
                    self.store.log("dataset_activated", {"id": job["id"], "name": entry["name"], "rows": len(app.df)})
                elif job["refit"] and prev is None and old:
                    self.store.log("models_retrained", {"id": job["id"], "rows": len(app.df),
                                                        "cached": app.fc.cached})
                log.info("engine ready in %.1fs (version %d, %d rows, models %s)", time.time() - t0, self.version,
                         len(app.df), "cached" if app.fc.cached else ("reused" if prev else "fitted"))
            except Exception as e:  # keep serving the previous engine
                log.exception("engine build failed")
                self.error = f"{type(e).__name__}: {e}"
                if self.core is None:
                    self.state, self.message = "error", f"Could not load the dataset: {e}"
                    if job["id"] != "demo" and self.registry.get("demo"):
                        self.message += " · falling back to the demo dataset"
                        self.request("demo", reason="fallback")
                else:
                    self.message = f"Update failed, still showing the previous data: {e}"
            finally:
                self.building = False

    def _watch_feed(self):
        """Circuit-breaker input: a live dataset whose feed has gone quiet for longer than the policy allows."""
        while True:
            time.sleep(2)
            c = self.core
            if not c or not c.live or not self.last_ingest:
                continue
            try:
                limit_h = float(c.policy["circuit_breaker"].get("stale_after_hours", 2))
            except Exception:
                limit_h = 2.0
            quiet_h = (time.time() - self.last_ingest) / self.live_seconds_per_hour
            c.stale_reason = (f"Stale feed: no new data for {quiet_h:.1f} h (limit {limit_h:g} h)"
                              if quiet_h > limit_h else None)
