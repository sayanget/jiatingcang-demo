import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import psycopg
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

STATUS_OPTIONS = ["已收到面单", "已打印", "已打包", "已交USPS/UPS"]
SHIPPED_STATUS = "已交USPS/UPS"
INBOUND_STATUSES = ["预报中", "已入库"]
CARRIERS = ["USPS", "UPS"]
CUTOFF_PLACEHOLDER = "太平洋时间下午 4:00"  # editable placeholder


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv(ENV_FILE)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "jiatingcang")
    user = os.environ.get("POSTGRES_USER", "jiatingcang")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    DATABASE_URL = f"postgresql://{user}:{password}@{host}:{port}/{db}"

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_COOKIE = "jc_admin"


def db():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory (
                id SERIAL PRIMARY KEY,
                seller_code TEXT NOT NULL,
                sku TEXT NOT NULL,
                product_name TEXT NOT NULL DEFAULT '',
                qty_on_hand INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (seller_code, sku)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inbounds (
                id SERIAL PRIMARY KEY,
                inbound_no TEXT UNIQUE NOT NULL,
                seller_code TEXT NOT NULL,
                seller_name TEXT NOT NULL DEFAULT '',
                contact TEXT,
                sku TEXT NOT NULL,
                product_name TEXT NOT NULL DEFAULT '',
                qty_declared INTEGER NOT NULL,
                qty_received INTEGER NOT NULL DEFAULT 0,
                inbound_tracking TEXT,
                status TEXT NOT NULL DEFAULT '预报中',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                order_no TEXT UNIQUE NOT NULL,
                seller_code TEXT NOT NULL,
                seller_name TEXT NOT NULL DEFAULT '',
                contact TEXT,
                sku TEXT NOT NULL,
                qty INTEGER NOT NULL,
                carrier TEXT NOT NULL,
                note TEXT,
                label_files TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT '已收到面单',
                outbound_tracking TEXT,
                inventory_deducted BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    import time

    last = None
    for _ in range(30):
        try:
            init_db()
            last = None
            break
        except Exception as e:
            last = e
            time.sleep(2)
    if last:
        print("DB init warning:", last)
    yield


app = FastAPI(title="加州家庭仓｜一件代发", lifespan=lifespan)
_template_dirs = [BASE_DIR / "templates", Path.cwd() / "templates"]
_template_dir = next((d for d in _template_dirs if d.is_dir()), BASE_DIR / "templates")
templates = Jinja2Templates(directory=str(_template_dir))
print("templates_dir", _template_dir, "exists", _template_dir.is_dir())


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx = {"request": request, **ctx}
    try:
        # Starlette >=0.37 prefers (request, name, context)
        resp = templates.TemplateResponse(request, name, ctx)
    except TypeError:
        resp = templates.TemplateResponse(name, ctx)
    resp.status_code = status_code
    return resp


def new_no(prefix: str) -> str:
    day = datetime.now(timezone.utc).strftime("%y%m%d")
    return f"{prefix}{day}{secrets.token_hex(3).upper()}"


def is_admin(request: Request) -> bool:
    return request.cookies.get(ADMIN_COOKIE) == "1" and bool(ADMIN_PASSWORD)


def require_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="未授权 / Unauthorized")


def parse_labels(raw: str) -> List[str]:
    try:
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]


async def save_uploads(files: List[UploadFile]) -> List[str]:
    saved = []
    for f in files or []:
        if not f or not f.filename:
            continue
        data = await f.read()
        if not data:
            continue
        if len(data) > 8_000_000:
            raise HTTPException(400, "文件过大 / File too large (max 8MB each)")
        fname = f"{secrets.token_hex(4)}_{safe_filename(f.filename)}"
        (UPLOAD_DIR / fname).write_bytes(data)
        saved.append(fname)
    return saved


def bump_inventory(conn, seller_code: str, sku: str, product_name: str, delta: int):
    conn.execute(
        """
        INSERT INTO inventory (seller_code, sku, product_name, qty_on_hand, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (seller_code, sku) DO UPDATE SET
            qty_on_hand = inventory.qty_on_hand + EXCLUDED.qty_on_hand,
            product_name = CASE WHEN EXCLUDED.product_name <> '' THEN EXCLUDED.product_name ELSE inventory.product_name END,
            updated_at = NOW()
        """,
        (seller_code, sku, product_name or "", delta),
    )


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render(request, "home.html", cutoff=CUTOFF_PLACEHOLDER, statuses=STATUS_OPTIONS)


@app.get("/inbound", response_class=HTMLResponse)
def inbound_form(request: Request):
    return render(request, "inbound.html", cutoff=CUTOFF_PLACEHOLDER)


@app.post("/inbound")
async def inbound_submit(
    request: Request,
    seller_code: str = Form(...),
    seller_name: str = Form(""),
    contact: str = Form(""),
    sku: str = Form(...),
    product_name: str = Form(""),
    qty_declared: int = Form(...),
    inbound_tracking: str = Form(""),
):
    if qty_declared < 1 or qty_declared > 100000:
        raise HTTPException(400, "数量无效")
    inbound_no = new_no("IB")
    with db() as conn:
        for _ in range(5):
            try:
                conn.execute(
                    """
                    INSERT INTO inbounds (
                        inbound_no, seller_code, seller_name, contact, sku, product_name,
                        qty_declared, inbound_tracking, status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'预报中')
                    """,
                    (
                        inbound_no,
                        seller_code.strip().upper(),
                        seller_name.strip(),
                        contact.strip(),
                        sku.strip().upper(),
                        product_name.strip(),
                        qty_declared,
                        inbound_tracking.strip(),
                    ),
                )
                break
            except psycopg.errors.UniqueViolation:
                inbound_no = new_no("IB")
    return render(request, "inbound_done.html", inbound_no=inbound_no, cutoff=CUTOFF_PLACEHOLDER)


@app.get("/order", response_class=HTMLResponse)
def order_form(request: Request):
    return render(request, "order.html", cutoff=CUTOFF_PLACEHOLDER, carriers=CARRIERS)


@app.post("/order")
async def order_submit(
    request: Request,
    seller_code: str = Form(...),
    seller_name: str = Form(""),
    contact: str = Form(""),
    sku: str = Form(...),
    qty: int = Form(...),
    carrier: str = Form(...),
    note: str = Form(""),
    labels: List[UploadFile] = File(None),
):
    if qty < 1 or qty > 9999:
        raise HTTPException(400, "数量无效")
    if carrier not in CARRIERS:
        raise HTTPException(400, "承运商无效 / Invalid carrier")
    files = labels if isinstance(labels, list) else ([labels] if labels else [])
    saved = await save_uploads(files)
    if not saved:
        raise HTTPException(400, "请至少上传一个面单 PDF / Please upload at least one shipping label PDF")

    order_no = new_no("JC")
    seller_code_u = seller_code.strip().upper()
    sku_u = sku.strip().upper()
    with db() as conn:
        inv = conn.execute(
            "SELECT qty_on_hand FROM inventory WHERE seller_code=%s AND sku=%s",
            (seller_code_u, sku_u),
        ).fetchone()
        on_hand = inv[0] if inv else 0
        # Soft warning only — still accept order (seller may have inbound in transit)
        warn = on_hand < qty
        for _ in range(5):
            try:
                conn.execute(
                    """
                    INSERT INTO orders (
                        order_no, seller_code, seller_name, contact, sku, qty, carrier,
                        note, label_files, status
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'已收到面单')
                    """,
                    (
                        order_no,
                        seller_code_u,
                        seller_name.strip(),
                        contact.strip(),
                        sku_u,
                        qty,
                        carrier,
                        note.strip(),
                        json.dumps(saved),
                    ),
                )
                break
            except psycopg.errors.UniqueViolation:
                order_no = new_no("JC")

    return render(request, "order_done.html", order_no=order_no, warn=warn, on_hand=on_hand, cutoff=CUTOFF_PLACEHOLDER)


@app.get("/track", response_class=HTMLResponse)
def track(request: Request, q: str = ""):
    qn = (q or "").strip().upper()
    order = None
    orders = []
    inventory = []
    inbound = []
    error = None
    if qn:
        with db() as conn:
            row = conn.execute(
                """
                SELECT order_no, seller_code, sku, qty, carrier, status, outbound_tracking, created_at, updated_at
                FROM orders WHERE order_no=%s
                """,
                (qn,),
            ).fetchone()
            if row:
                order = {
                    "order_no": row[0],
                    "seller_code": row[1],
                    "sku": row[2],
                    "qty": row[3],
                    "carrier": row[4],
                    "status": row[5],
                    "outbound_tracking": row[6],
                    "created_at": row[7],
                    "updated_at": row[8],
                }
            # seller code lookup
            orders = [
                {
                    "order_no": r[0],
                    "sku": r[1],
                    "qty": r[2],
                    "carrier": r[3],
                    "status": r[4],
                    "outbound_tracking": r[5],
                    "created_at": r[6],
                }
                for r in conn.execute(
                    """
                    SELECT order_no, sku, qty, carrier, status, outbound_tracking, created_at
                    FROM orders WHERE seller_code=%s ORDER BY id DESC LIMIT 50
                    """,
                    (qn,),
                ).fetchall()
            ]
            inventory = [
                {"sku": r[0], "product_name": r[1], "qty_on_hand": r[2], "updated_at": r[3]}
                for r in conn.execute(
                    """
                    SELECT sku, product_name, qty_on_hand, updated_at
                    FROM inventory WHERE seller_code=%s ORDER BY sku
                    """,
                    (qn,),
                ).fetchall()
            ]
            inbound = [
                {
                    "inbound_no": r[0],
                    "sku": r[1],
                    "product_name": r[2],
                    "qty_declared": r[3],
                    "qty_received": r[4],
                    "inbound_tracking": r[5],
                    "status": r[6],
                    "created_at": r[7],
                }
                for r in conn.execute(
                    """
                    SELECT inbound_no, sku, product_name, qty_declared, qty_received, inbound_tracking, status, created_at
                    FROM inbounds WHERE seller_code=%s OR inbound_no=%s ORDER BY id DESC LIMIT 50
                    """,
                    (qn, qn),
                ).fetchall()
            ]
        if not order and not orders and not inventory and not inbound:
            error = "未找到 / Not found — try order no (JC…), inbound no (IB…), or seller code"
    return render(request, "track.html", q=qn, order=order, orders=orders, inventory=inventory, inbound=inbound, error=error, cutoff=CUTOFF_PLACEHOLDER)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if not is_admin(request):
        return render(request, "admin_login.html", error=None)
    with db() as conn:
        orders = [
            {
                "id": r[0],
                "order_no": r[1],
                "seller_code": r[2],
                "seller_name": r[3],
                "sku": r[4],
                "qty": r[5],
                "carrier": r[6],
                "note": r[7],
                "label_files": parse_labels(r[8]),
                "status": r[9],
                "outbound_tracking": r[10],
                "inventory_deducted": r[11],
                "created_at": r[12],
            }
            for r in conn.execute(
                """
                SELECT id, order_no, seller_code, seller_name, sku, qty, carrier, note,
                       label_files, status, outbound_tracking, inventory_deducted, created_at
                FROM orders ORDER BY id DESC LIMIT 200
                """
            ).fetchall()
        ]
        inbounds = [
            {
                "id": r[0],
                "inbound_no": r[1],
                "seller_code": r[2],
                "seller_name": r[3],
                "sku": r[4],
                "product_name": r[5],
                "qty_declared": r[6],
                "qty_received": r[7],
                "inbound_tracking": r[8],
                "status": r[9],
                "created_at": r[10],
            }
            for r in conn.execute(
                """
                SELECT id, inbound_no, seller_code, seller_name, sku, product_name,
                       qty_declared, qty_received, inbound_tracking, status, created_at
                FROM inbounds ORDER BY id DESC LIMIT 200
                """
            ).fetchall()
        ]
        inventory = [
            {
                "id": r[0],
                "seller_code": r[1],
                "sku": r[2],
                "product_name": r[3],
                "qty_on_hand": r[4],
                "updated_at": r[5],
            }
            for r in conn.execute(
                """
                SELECT id, seller_code, sku, product_name, qty_on_hand, updated_at
                FROM inventory ORDER BY seller_code, sku LIMIT 500
                """
            ).fetchall()
        ]
    return render(request, "admin.html", orders=orders, inbounds=inbounds, inventory=inventory, statuses=STATUS_OPTIONS, admin_ready=bool(ADMIN_PASSWORD))


@app.post("/admin/set-password")
async def admin_set_password(old_password: str = Form(...), new_password: str = Form(...)):
    """Change ADMIN_PASSWORD when the current password is known. Writes .env."""
    global ADMIN_PASSWORD
    if not ADMIN_PASSWORD or old_password != ADMIN_PASSWORD:
        raise HTTPException(401, "旧密码错误 / Wrong current password")
    new_password = (new_password or "").strip()
    if len(new_password) < 8:
        raise HTTPException(400, "Password too short")
    ENV_FILE.write_text(f"ADMIN_PASSWORD={new_password}\n", encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)
    except Exception:
        pass
    ADMIN_PASSWORD = new_password
    return {"ok": True, "admin_configured": True}


@app.post("/admin/setup")
async def admin_setup(password: str = Form(...)):
    """One-time bootstrap when ADMIN_PASSWORD is unset. Writes .env and arms admin auth."""
    global ADMIN_PASSWORD
    if ADMIN_PASSWORD:
        raise HTTPException(400, "Already configured")
    password = (password or "").strip()
    if len(password) < 8:
        raise HTTPException(400, "Password too short")
    ENV_FILE.write_text(f"ADMIN_PASSWORD={password}\n", encoding="utf-8")
    try:
        ENV_FILE.chmod(0o600)
    except Exception:
        pass
    ADMIN_PASSWORD = password
    return {"ok": True, "admin_configured": True}


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if not ADMIN_PASSWORD or password != ADMIN_PASSWORD:
        return render(request, "admin_login.html", status_code=401, error="密码错误 / Wrong password")
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie(ADMIN_COOKIE, "1", httponly=True, samesite="lax", max_age=60 * 60 * 12)
    return resp


@app.post("/admin/logout")
def admin_logout():
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


@app.post("/admin/inbound/receive")
def admin_inbound_receive(
    request: Request,
    inbound_id: int = Form(...),
    qty_received: int = Form(...),
):
    require_admin(request)
    if qty_received < 0:
        raise HTTPException(400, "数量无效")
    with db() as conn:
        row = conn.execute(
            """
            SELECT seller_code, sku, product_name, qty_received, status
            FROM inbounds WHERE id=%s FOR UPDATE
            """,
            (inbound_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "入库单不存在")
        seller_code, sku, product_name, prev_received, status = row
        delta = qty_received - int(prev_received or 0)
        conn.execute(
            """
            UPDATE inbounds
            SET qty_received=%s,
                status=CASE WHEN %s>0 THEN '已入库' ELSE status END,
                updated_at=NOW()
            WHERE id=%s
            """,
            (qty_received, qty_received, inbound_id),
        )
        if delta != 0:
            bump_inventory(conn, seller_code, sku, product_name, delta)
    return RedirectResponse("/admin#inbounds", status_code=303)


@app.post("/admin/order/update")
def admin_order_update(
    request: Request,
    order_id: int = Form(...),
    status: str = Form(...),
    outbound_tracking: str = Form(""),
):
    require_admin(request)
    if status not in STATUS_OPTIONS:
        raise HTTPException(400, "无效状态")
    with db() as conn:
        row = conn.execute(
            """
            SELECT seller_code, sku, qty, status, inventory_deducted
            FROM orders WHERE id=%s FOR UPDATE
            """,
            (order_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "订单不存在")
        seller_code, sku, qty, old_status, deducted = row
        conn.execute(
            """
            UPDATE orders
            SET status=%s, outbound_tracking=%s, updated_at=NOW()
            WHERE id=%s
            """,
            (status, outbound_tracking.strip() or None, order_id),
        )
        if status == SHIPPED_STATUS and not deducted:
            bump_inventory(conn, seller_code, sku, "", -int(qty))
            conn.execute(
                "UPDATE orders SET inventory_deducted=TRUE, updated_at=NOW() WHERE id=%s",
                (order_id,),
            )
    return RedirectResponse("/admin#orders", status_code=303)


@app.post("/admin/order/delete")
def admin_order_delete(request: Request, order_id: int = Form(...)):
    require_admin(request)
    with db() as conn:
        row = conn.execute("SELECT label_files FROM orders WHERE id=%s", (order_id,)).fetchone()
        conn.execute("DELETE FROM orders WHERE id=%s", (order_id,))
    if row:
        for fname in parse_labels(row[0]):
            p = UPLOAD_DIR / fname
            if p.exists():
                p.unlink()
    return RedirectResponse("/admin#orders", status_code=303)


@app.post("/admin/inbound/delete")
def admin_inbound_delete(request: Request, inbound_id: int = Form(...)):
    require_admin(request)
    with db() as conn:
        conn.execute("DELETE FROM inbounds WHERE id=%s", (inbound_id,))
    return RedirectResponse("/admin#inbounds", status_code=303)


@app.post("/admin/inventory/delete")
def admin_inventory_delete(request: Request, inventory_id: int = Form(...)):
    require_admin(request)
    with db() as conn:
        conn.execute("DELETE FROM inventory WHERE id=%s", (inventory_id,))
    return RedirectResponse("/admin#inventory", status_code=303)


@app.get("/admin/label/{filename}")
def admin_label_download(request: Request, filename: str):
    require_admin(request)
    safe = Path(filename).name
    path = UPLOAD_DIR / safe
    if not path.exists():
        raise HTTPException(404, "文件不存在")
    return FileResponse(path, filename=safe)


@app.get("/ping")
def ping():
    return {
        "ok": True,
        "base_dir": str(BASE_DIR),
        "templates_dir": str(_template_dir),
        "templates_exist": _template_dir.is_dir(),
        "cwd": str(Path.cwd()),
        "admin_configured": bool(ADMIN_PASSWORD),
        "template_files": sorted([x.name for x in _template_dir.glob("*.html")]) if _template_dir.is_dir() else [],
    }


@app.get("/health")
def health():
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "db": True, "admin_configured": bool(ADMIN_PASSWORD)}
    except Exception as e:
        return JSONResponse({"ok": False, "db": False, "error": str(e)}, status_code=503)
