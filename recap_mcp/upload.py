"""Phone photo upload page (spec 5.4 tool 4). Async handlers; blocking work goes to a thread."""
import html
import io
import os
import uuid
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

    def page(eid, token, error=""):
        ev, fields = Deps.store.event(eid), fields_of(eid)
        e = html.escape
        secs = []
        for slot in (*SINGLE, "crowd"):
            f = slot_field(fields, slot) if slot != "crowd" else None
            if slot != "crowd" and not f:
                continue
            rows = Deps.store.photos(eid, slot)
            label = "Event photo gallery" if slot == "crowd" else f.label
            head = f"Crowd: {len(rows)} of 5+" if slot == "crowd" else ""
            thumbs = "".join(
                f'<div class="ph"><img src="/upload/{token}/thumb/{p["id"]}" alt="">'
                f'<form method="post" action="/upload/{token}/delete/{p["id"]}"><button>Remove</button></form></div>'
                for p in rows)
            multi = " multiple" if slot == "crowd" else ""
            secs.append(
                f'<section><h2>{e(label)}</h2><p>{head}</p><div class="row">{thumbs}</div>'
                f'<form method="post" action="/upload/{token}/{slot}" enctype="multipart/form-data">'
                f'<input type="file" name="files" accept="image/*"{multi}> <button>Upload</button></form></section>')
        title = f"{e(ev['venue_name'])} - {e(ev['start_ts'][:10])}"
        return HTMLResponse(PAGE.substitute(title=title, sections="\n".join(secs),
                                            error=f'<p class="err">{e(error)}</p>' if error else ""))

    def back(token):
        return RedirectResponse(f"/upload/{token}", status_code=303)

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
        return await anyio.to_thread.run_sync(page, *c)

    async def upload(request: Request):
        c = ctx(request)
        slot = request.path_params["slot"]
        if not c or slot not in (*SINGLE, "crowd"):
            return Response("Not found.", status_code=404)
        eid, token = c
        fields = await anyio.to_thread.run_sync(fields_of, eid)
        f = slot_field(fields, slot) if slot != "crowd" else None
        if slot != "crowd" and not f:
            return Response("Not found.", status_code=404)
        form = await request.form()
        raw = []
        for u in form.getlist("files"):
            if not getattr(u, "filename", None):
                continue
            if (u.size or 0) > MAX_BYTES:
                return Response("File too large (25 MB max).", status_code=413)
            raw.append(await u.read())
        try:
            jpegs = [await anyio.to_thread.run_sync(normalise_image, d) for d in raw]
        except ValueError as e:
            return Response(str(e), status_code=400)
        if not jpegs:
            return back(token)
        if slot != "crowd":
            jpegs = jpegs[-1:]
        olds = Deps.store.photos(eid, slot) if slot != "crowd" else []
        for j in jpegs:
            p = data_dir() / "recaps" / eid / slot / f"{uuid.uuid4()}.jpg"
            await anyio.to_thread.run_sync(_write, p, j)
            Deps.store.add_photo(eid, slot, str(p))
        for old in olds:  # replace only after the new one is safe; recap docs are just dropped locally
            Deps.store.delete_photo(old["id"])
            Path(old["path"]).unlink(missing_ok=True)
        if f:
            Deps.store.set_answers(eid, {f.instance_id: "photo"}, set())
        return back(token)

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
                return await anyio.to_thread.run_sync(page, eid, token, str(e))
        Deps.store.delete_photo(p["id"])
        Path(p["path"]).unlink(missing_ok=True)
        f = slot_field(await anyio.to_thread.run_sync(fields_of, eid), p["slot"])
        if f and not Deps.store.photos(eid, p["slot"]):
            Deps.store.delete_answers(eid, [f.instance_id])
        return back(token)

    server.custom_route("/upload/{token}", methods=["GET"])(view)
    server.custom_route("/upload/{token}/thumb/{photo_id}", methods=["GET"])(thumb)
    server.custom_route("/upload/{token}/delete/{photo_id}", methods=["POST"])(delete)
    server.custom_route("/upload/{token}/{slot}", methods=["POST"])(upload)
