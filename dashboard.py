"""Local web dashboard for the DataFeel DevKit.

Run:  python dashboard.py      then open http://127.0.0.1:8000

Over Bluetooth it only connects to our El Jefe. Other kits nearby advertise too, so to use
a different one set e.g. DATAFEEL_BLE=DataFeel36,DataFeel3A (or DATAFEEL_BLE=any).
"""
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal, Optional, Union

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from datafeel.device import (
    Dot,
    VibrationMode,
    VibrationWaveforms,
    discover_devices,
)

MAX_ADDRESS = 4
_ble = os.environ.get("DATAFEEL_BLE", "DataFeel36")
BLE_NAMES = None if _ble.lower() == "any" else [n.strip() for n in _ble.split(",") if n.strip()]
# Safety limits for skin-contact use. The SDK allows -1..1 intensity; target temp is our own clamp.
MIN_TARGET_C, MAX_TARGET_C = 18.0, 40.0

# All dots share one serial port, and FastAPI runs sync endpoints on a thread pool,
# so every Modbus transaction must go through this lock.
bus = threading.RLock()
# Keyed "<link number>-<address>": dots on different El Jefes can share an address.
dots: dict[str, Dot] = {}


def scan():
    with bus:
        dots.clear()
        links: list[str] = []
        seen: set[str] = set()
        for d in discover_devices(MAX_ADDRESS, ble_names=BLE_NAMES):
            # USB is scanned first, so a module reachable both ways keeps its cable link.
            if d.serial_number in seen:
                continue
            seen.add(d.serial_number)
            if d.link not in links:
                links.append(d.link)
            dots[f"{links.index(d.link)}-{d.id}"] = d


def clean(s: str) -> str:
    # Device strings come back padded with NULs and spaces.
    return s.replace("\x00", "").strip()


def targets(dot_id: str) -> List[Dot]:
    if dot_id == "all":
        return list(dots.values())
    try:
        return [dots[dot_id]]
    except KeyError:
        raise HTTPException(404, f"No dot with id {dot_id}")


def safe_off(d: Dot):
    d.stop_vibration()
    d.disable_all_thermal()
    d.set_led_off()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scan()
    yield
    # Never leave a dot heating or buzzing after the server exits.
    with bus:
        for d in dots.values():
            try:
                safe_off(d)
            except Exception:
                pass
    try:
        from datafeel import ble
        ble.close_all()
    except ImportError:
        pass


app = FastAPI(lifespan=lifespan)
STATIC = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/waveforms")
def waveforms():
    return [w.name for w in VibrationWaveforms if w != VibrationWaveforms.END_SEQUENCE]


@app.get("/api/dots")
def list_dots():
    out = []
    with bus:
        for key, d in dots.items():
            info = {"id": key, "address": d.id, "link": d.link, "serial": clean(d.serial_number),
                    "firmware": clean(d.firmware_id), "ok": True}
            try:
                info |= {k: v.name if hasattr(v, "name") else v for k, v in d.get_status().items()}
            except Exception as e:
                info |= {"ok": False, "error": str(e)}
            out.append(info)
    return out


@app.post("/api/rescan")
def rescan():
    scan()
    return {"found": [f"Dot {d.id} ({d.link})" for d in dots.values()]}


class LedReq(BaseModel):
    mode: Literal["color", "breathe", "off"]
    r: int = Field(0, ge=0, le=255)
    g: int = Field(0, ge=0, le=255)
    b: int = Field(0, ge=0, le=255)
    index: Optional[int] = Field(None, ge=0, le=7)  # None = all 8 LEDs


@app.post("/api/dots/{dot_id}/led")
def led(dot_id: str, req: LedReq):
    with bus:
        for d in targets(dot_id):
            if req.mode == "off":
                d.set_led_off()
            elif req.mode == "breathe":
                d.set_led_breathe()
            elif req.index is None:
                d.set_led(req.r, req.g, req.b)
            else:
                # Switching into individual mode keeps the other LEDs' previous values.
                d.set_led(req.r, req.g, req.b, req.index)
    return {"ok": True}


class VibrateReq(BaseModel):
    frequency: float = Field(..., ge=1, le=500)
    intensity: float = Field(..., ge=0, le=1)


@app.post("/api/dots/{dot_id}/vibrate")
def vibrate(dot_id: str, req: VibrateReq):
    with bus:
        for d in targets(dot_id):
            d.play_frequency(req.frequency, req.intensity)
    return {"ok": True}


class SequenceReq(BaseModel):
    # Each step is a waveform name, or a number of seconds (0–1.27) to rest.
    steps: List[Union[str, float]] = Field(..., min_length=1, max_length=8)


@app.post("/api/dots/{dot_id}/sequence")
def sequence(dot_id: str, req: SequenceReq):
    seq = []
    for s in req.steps:
        if isinstance(s, str):
            try:
                seq.append(VibrationWaveforms[s])
            except KeyError:
                raise HTTPException(400, f"Unknown waveform {s}")
        else:
            try:
                seq.append(VibrationWaveforms.Rest(s))
            except ValueError as e:
                raise HTTPException(400, str(e))
    with bus:
        for d in targets(dot_id):
            d.play_vibration_sequence(seq)
    return {"ok": True}


@app.post("/api/dots/{dot_id}/vibration/stop")
def stop_vibration(dot_id: str):
    with bus:
        for d in targets(dot_id):
            d.stop_vibration()
            d.registers.set_vibration_mode(VibrationMode.OFF)
    return {"ok": True}


class ThermalReq(BaseModel):
    mode: Literal["off", "intensity", "target"]
    value: float = 0.0


@app.post("/api/dots/{dot_id}/thermal")
def thermal(dot_id: str, req: ThermalReq):
    if req.mode == "intensity" and not -1.0 <= req.value <= 1.0:
        raise HTTPException(400, "Intensity must be between -1 and 1")
    if req.mode == "target" and not MIN_TARGET_C <= req.value <= MAX_TARGET_C:
        raise HTTPException(400, f"Target must be between {MIN_TARGET_C} and {MAX_TARGET_C} °C")
    with bus:
        for d in targets(dot_id):
            if req.mode == "off":
                d.disable_all_thermal()
            elif req.mode == "intensity":
                d.activate_thermal_intensity_control(req.value)
            else:
                d.activate_thermal_temperature_control(req.value)
    return {"ok": True}


@app.post("/api/stop-all")
def stop_all():
    with bus:
        for d in dots.values():
            safe_off(d)
            d.registers.set_vibration_mode(VibrationMode.OFF)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
