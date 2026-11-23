"""Phone photo upload page (spec 5.4 tool 4). Async handlers; blocking work goes to a thread."""
import html
import io
import os
import uuid
from datetime import datetime
from pathlib import Path
from string import Template

import anyio.to_thread
from PIL import Image, ImageOps
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .site import SiteError

MAX_BYTES = 25 * 1024 * 1024
SINGLE = ("setup", "receipt", "sign")
PAGE = Template((Path(__file__).parent / "upload.html").read_text(encoding="utf-8"))


def data_dir() -> Path:
    return Path(os.environ.get("RECAP_DATA") or Path(__file__).resolve().parent.parent / "data")


def normalise_image(data: bytes) -> bytes:
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except Exception as e:  # Pillow raises many types for bad input
        raise ValueError("That file is not a readable image.") from e
    img.thumbnail((2000, 2000))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def register_upload_routes(server) -> None:
    from .server import Deps, _load, slot_field  # late: server imports this module

    def fields_of(eid):
        return _load(eid)[2]

    def ctx(request):  # (events_id, token) or None
        t = request.path_params["token"]
        eid = Deps.store.link_event(t)
        return (eid, t) if eid else None

    def slots(fields):  # (slot, field or None for crowd, label) in page order
        for slot in SINGLE:
            if (f := slot_field(fields, slot)):
                yield slot, f, f.label
        yield "crowd", None, "Crowd photos"

    def page(eid, token, error="", done=0, status=200):
        ev, fields = Deps.store.event(eid), fields_of(eid)
        e = html.escape
        secs = []
        for slot, f, label in slots(fields):
            rows = Deps.store.photos(eid, slot)
            crowd = slot == "crowd"
            thumbs = "".join(
                f'<figure class="ph"><img src="/upload/{token}/thumb/{p["id"]}" alt="{e(label)}, photo {i}">'
                f'<form class="rm" method="post" action="/upload/{token}/delete/{p["id"]}">'
                f'<button class="x" aria-label="Remove photo {i}">&times;</button></form></figure>'
                for i, p in enumerate(rows, 1))
            badge = (f'{len(rows)} added' if crowd else "Added") if rows else "Not added"
            hint = ("These go straight to the event's photo gallery. Aim for 5 or more." if crowd
                    else "One photo. A new one replaces the old one." if rows else "")
            pick = "Add photos" if crowd else "Replace photo" if rows else "Add photo"
            secs.append(
                f'<section class="card"><div class="head"><h2>{e(label)}</h2>'
                f'<span class="badge{" done" if rows else ""}">{badge}</span></div>'
                + (f'<p class="hint">{hint}</p>' if hint else "") +
                f'<div class="thumbs">{thumbs}</div><div class="chosen" aria-live="polite"></div>'
                f'<label class="pick"><input class="sr" type="file" name="{slot}" accept="image/*" form="up"'
                f'{" multiple" if crowd else ""}><span>{pick}</span></label></section>')
        notice = (f'<p class="msg err" role="alert">{e(error)}</p>' if error else
                  f'<p class="msg ok" role="status">Uploaded {done} photo{"s" if done != 1 else ""}.</p>' if done else "")
        try:
            d = datetime.strptime(ev["start_ts"][:16], "%Y-%m-%d %H:%M")
            when = f"{d:%a}, {d:%b} {d.day} &middot; {d.hour % 12 or 12}:{d:%M} {d:%p}"
        except ValueError:
            when = e(ev["start_ts"][:10])
        return HTMLResponse(PAGE.substitute(title=e(ev["venue_name"]), when=when, notice=notice,
                                            sections="\n".join(secs), action=f"/upload/{token}"),
                            status_code=status)

    def back(token, done=0):
        return RedirectResponse(f"/upload/{token}" + (f"?done={done}" if done else ""), status_code=303)

    def photo_of(c, request):
        # only this event's photos are searched, so another shift's photo id never matches
        if not c:
            return None
        pid = request.path_params["photo_id"]
        return next((p for p in Deps.store.photos(c[0]) if str(p["id"]) == pid), None)

    async def view(request: Request):
        c = ctx(request)
        if not c:
            return Response("Link expired or unknown.", status_code=404)
        try:
            done = int(request.query_params.get("done") or 0)
        except ValueError:
            done = 0
        return await anyio.to_thread.run_sync(lambda: page(*c, done=done))

    async def upload(request: Request):  # one form, one file input per slot (named after the slot)
        c = ctx(request)
        if not c:
            return Response("Link expired or unknown.", status_code=404)
        eid, token = c
        fields = await anyio.to_thread.run_sync(fields_of, eid)
        form = await request.form()
        errors, done = [], 0
        for slot, f, label in slots(fields):  # each slot saved on its own: one bad file never loses the rest
            jpegs = []
            for u in form.getlist(slot):
                if not getattr(u, "filename", None):
                    continue
                if (u.size or 0) > MAX_BYTES:
                    errors.append(f"{label}: {u.filename} is too large (25 MB max).")
                    continue
                try:
                    jpegs.append(await anyio.to_thread.run_sync(normalise_image, await u.read()))
                except ValueError:
                    errors.append(f"{label}: {u.filename} is not a readable image.")
            if not jpegs:
                continue
            if f:
                jpegs = jpegs[-1:]
            olds = Deps.store.photos(eid, slot) if f else []
            for j in jpegs:
                p = data_dir() / "recaps" / eid / slot / f"{uuid.uuid4()}.jpg"
                await anyio.to_thread.run_sync(_write, p, j)
                Deps.store.add_photo(eid, slot, str(p))
            for old in olds:  # replace only after the new one is safe; recap docs are just dropped locally
                Deps.store.delete_photo(old["id"])
                Path(old["path"]).unlink(missing_ok=True)
            if f:
                Deps.store.set_answers(eid, {f.instance_id: "photo"}, set())
            done += len(jpegs)
        if errors:
            msg = (f"Uploaded {done}. " if done else "") + " ".join(errors) + " Please choose those again."
            return await anyio.to_thread.run_sync(lambda: page(eid, token, msg, status=400))
        return back(token, done)

    async def thumb(request: Request):
        p = photo_of(ctx(request), request)
        if not p:
            return Response("Not found.", status_code=404)
        try:
            data = await anyio.to_thread.run_sync(Path(p["path"]).read_bytes)
        except OSError:
            return Response("Not found.", status_code=404)
        return Response(data, media_type="image/jpeg")

    async def delete(request: Request):
        c = ctx(request)
        p = photo_of(c, request)
        if not p:
            return Response("Not found.", status_code=404)
        eid, token = c
        if p["slot"] == "crowd" and p["site_document_id"]:
            try:
                await anyio.to_thread.run_sync(Deps.site.remove_gallery_photos, [p["site_document_id"]])
            except SiteError as e:
                return await anyio.to_thread.run_sync(lambda: page(eid, token, str(e), status=502))
        Deps.store.delete_photo(p["id"])
        Path(p["path"]).unlink(missing_ok=True)
        f = slot_field(await anyio.to_thread.run_sync(fields_of, eid), p["slot"])
        if f and not Deps.store.photos(eid, p["slot"]):
            Deps.store.delete_answers(eid, [f.instance_id])
        return back(token)

    server.custom_route("/upload/{token}", methods=["GET"])(view)
    server.custom_route("/upload/{token}/thumb/{photo_id}", methods=["GET"])(thumb)
    server.custom_route("/upload/{token}/delete/{photo_id}", methods=["POST"])(delete)
    server.custom_route("/upload/{token}", methods=["POST"])(upload)
