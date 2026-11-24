"""The settings page's "Test connection": can we reach and log in to this DVR, and what is it?

Each check is reported separately in plain words, so a user can tell "wrong password" from "port 9000
blocked". Uses throwaway clients; nothing is kept and nothing is written to the DVR.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from .dvr import DvrApiError, DvrAuthError, DvrClient, DvrError
from .dvr9000 import Dvr9000AuthError, Dvr9000Error, MainSession


@dataclass(frozen=True)
class Target:
    host: str
    http_port: int
    media_port: int
    rtsp_port: int
    username: str
    password: str


async def tcp_probe(host: str, port: int, timeout: float = 3.0) -> str | None:
    """None if a TCP connection opens, else a short reason."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except asyncio.TimeoutError:
        return "no answer (timed out - a firewall may be dropping it)"
    except ConnectionRefusedError:
        return "refused (nothing listening on that port)"
    except OSError as e:
        return f"cannot connect ({e.strerror or type(e).__name__})"
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return None


async def run(t: Target, *, probe: Callable[[str, int], Awaitable[str | None]] = tcp_probe,
              http_transport: httpx.AsyncBaseTransport | None = None,
              media_factory: Callable[[Target], MainSession] | None = None) -> dict:
    checks: list[dict] = []
    info: dict = {}

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    ports = (("Web API", t.http_port), ("Live video (RTSP)", t.rtsp_port), ("Recordings (client port)", t.media_port))
    reach = await asyncio.gather(*(probe(t.host, p) for _, p in ports))
    for (label, port), problem in zip(ports, reach):
        add(f"{label}: port {port}", problem is None, "reachable" if problem is None else problem)

    if reach[0] is None:
        dvr = DvrClient(t.host, t.username, t.password, port=t.http_port, transport=http_transport,
                        min_relogin_interval=0, timeout=8.0)
        try:
            base = await dvr.base_info()
            dev = await dvr.system_info()
            channels = await dvr.channel_info()
            info = {"model": base.model or dev.device_type, "firmware": base.firmware, "hardware": base.hardware,
                    "channels": len(channels) or dev.channel_count}
            add("Log in to the web API", True,
                f"{info['model'] or 'DVR'}, firmware {info['firmware'] or 'unknown'}, {info['channels']} cameras")
        except DvrAuthError:
            add("Log in to the web API", False, "the DVR refused the username or password")
        except DvrApiError as e:
            add("Log in to the web API", False, f"logged in, but the DVR refused a request ({e.result}); "
                                                "this model may not be compatible")
        except DvrError as e:
            add("Log in to the web API", False, f"no usable answer from the DVR's API ({e})"[:200])
        finally:
            await dvr.close()

    if reach[2] is None:
        media = media_factory(t) if media_factory else MainSession(t.host, t.media_port, t.username, t.password,
                                                                   min_login_interval=0)
        try:
            await media.acquire()
            media.release()
            add("Log in for recordings", True, "recorded playback and downloads will work")
        except Dvr9000AuthError:
            add("Log in for recordings", False, "the DVR refused the username or password on its client port")
        except (Dvr9000Error, OSError) as e:
            add("Log in for recordings", False, f"the client port answered but the login failed ({e})"[:200])
        finally:
            await media.close()

    essential = [c for c in checks if c["check"].startswith(("Web API", "Log in to the web API"))]
    return {"ok": bool(essential) and all(c["ok"] for c in essential) and len(essential) == 2,
            "all_ok": all(c["ok"] for c in checks) and len(checks) == 5, "checks": checks, "dvr": info}
