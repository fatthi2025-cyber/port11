"""
تطبيق تكسيات (نموذج تجريبي) - Dash + SQLite

التشغيل محلياً:
    pip install -r requirements.txt
    python taxi_app.py
  - التطبيق العام (راكب + سائق):  http://127.0.0.1:8050
  - لوحة الإدارة (للإدارة فقط):    http://127.0.0.1:8050/admin/
      المستخدم: admin   وكلمة المرور: من متغير البيئة ADMIN_PASSWORD (الافتراضي admin123 للتجربة فقط)

النشر على Render (أمر التشغيل):
    gunicorn taxi_app:server --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT
  يجب أن يكون workers = 1 لأن حالة التكسيات الحية محفوظة في ذاكرة العملية.
  متغيرات البيئة: ADMIN_PASSWORD (إلزامي)، ADMIN_USER (اختياري)، TAXI_DB (مسار قاعدة البيانات).

ملاحظات:
  - المتصفحات لا تسمح بتحديد الموقع إلا على https أو localhost (Render يوفر https).
  - رقم الموبايل هنا غير مُتحقَّق منه برسالة SMS.
"""
import hmac
import math
import os
import random
import sqlite3
import threading
import time
import uuid

import plotly.graph_objects as go
from dash import Dash, dcc, html, dash_table, Input, Output, State, ctx, no_update
from flask import Response, request

# ======================= الإعدادات (عدّلها حسب منطقتك) =======================
START = (31.9030, 35.2040)   # (خط العرض، خط الطول) نقطة الانطلاق في وسط البلد
END = (31.9300, 35.1800)     # نقطة الوصول في الحي
N_TAXIS = 10                 # عدد التكسيات
DEFAULT_SEATS = 7            # عدد ركاب الحافلة
GRID = 0.004                 # حجم خلية تجميع الركاب (≈ 400م)
MIN_CLUSTER = 1              # أقل عدد ركاب ليصبح التجمع نقطة توقف في المسار
MAX_DETOUR = 0.01            # أقصى بعد جانبي عن الخط الرئيسي لإدخال تجمّع في المسار (≈ 1.1 كم)
PICKUP_RADIUS = 0.0015       # مسافة التقاط الراكب للتكسيات الآلية (≈ 160م)
LOAD_RADIUS = 0.006          # عند "تحميل" يدوي: الركاب المنتظرون ضمن هذه المسافة (≈ 650م) يُعتبرون محمّلين
STEP = 0.0012                # حركة التكسي الآلي في كل خطوة محاكاة
SIM_TICK = 2                 # فترة خطوة المحاكاة بالثواني
REFRESH_SECONDS = 20         # تحديث الدبابيس والشاشات كل 20 ثانية
REQUEST_TTL = 15 * 60        # ينتهي طلب الراكب تلقائياً بعد 15 دقيقة
DRIVER_TIMEOUT = 120         # إن لم تصل إشارة من السائق خلال هذه المدة (ثواني) يُحرَّر تكسيه
SIMULATE_PASSENGERS = True   # ركاب تجريبيون (اجعلها False عند الاعتماد على الركاب الحقيقيين فقط)
MAX_SIM_PASSENGERS = 40

DB_PATH = os.environ.get("TAXI_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "taxi.db"))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")   # غيّرها عند النشر!
PUBLIC_PORT = int(os.environ.get("PORT", 8050))

# نقاط تجمع محتملة للركاب التجريبيين: (موضعها على الخط من 0 إلى 1، إزاحة جانبية)
HOTSPOTS = [(0.02, 0.000), (0.20, 0.004), (0.38, -0.005),
            (0.55, 0.003), (0.72, -0.003), (0.90, 0.002)]
# ============================================================================

lock = threading.Lock()      # يحمي الحالة الحية (التكسيات والركاب المنتظرين)
db_lock = threading.Lock()   # يحمي قاعدة البيانات
waiting = []                 # طلبات الركاب المنتظرين
taxis = []                   # التكسيات
stats = {"delivered": 0, "next_sim": 1}
handled_clicks = {}          # آخر ضغطة طلب تمت معالجتها لكل راكب


# ================================ قاعدة البيانات ================================
def db_query(sql, args=()):
    with db_lock:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()


def db_exec(sql, args=()):
    with db_lock:
        con = sqlite3.connect(DB_PATH)
        try:
            cur = con.execute(sql, args)
            con.commit()
            return cur.lastrowid
        finally:
            con.close()


def init_db():
    db_exec("""CREATE TABLE IF NOT EXISTS users(
        id TEXT PRIMARY KEY, role TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL,
        created_at REAL NOT NULL, UNIQUE(role, phone))""")
    db_exec("""CREATE TABLE IF NOT EXISTS rides(
        id INTEGER PRIMARY KEY AUTOINCREMENT, passenger_id TEXT, lat REAL, lon REAL, status TEXT,
        taxi_id INTEGER, driver_id TEXT, requested_at REAL, picked_at REAL, delivered_at REAL)""")
    db_exec("""CREATE TABLE IF NOT EXISTS load_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, taxi_id INTEGER, driver_id TEXT, action TEXT,
        count INTEGER, riders_after INTEGER, capacity INTEGER, lat REAL, lon REAL)""")


def norm_phone(s):
    s = (s or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    digits = "".join(c for c in s if c.isdigit())
    return digits if 7 <= len(digits) <= 15 else None


def register_or_login(role, name, phone):
    """يرجع (المستخدم، هل هو تسجيل جديد). الهوية = الدور + رقم الموبايل."""
    rows = db_query("SELECT * FROM users WHERE role=? AND phone=?", (role, phone))
    if rows:
        return rows[0], False
    uid = str(uuid.uuid4())
    db_exec("INSERT INTO users(id, role, name, phone, created_at) VALUES (?,?,?,?,?)",
            (uid, role, name, phone, time.time()))
    return {"id": uid, "role": role, "name": name, "phone": phone}, True


def ride_create(uid, lat, lon):
    return db_exec("INSERT INTO rides(passenger_id, lat, lon, status, requested_at) VALUES (?,?,?,?,?)",
                   (uid, lat, lon, "waiting", time.time()))


def ride_set(ride_id, status, taxi_id=None, driver_id=None):
    if status == "picked":
        db_exec("UPDATE rides SET status=?, taxi_id=?, driver_id=?, picked_at=? WHERE id=?",
                (status, taxi_id, driver_id, time.time(), ride_id))
    elif status == "delivered":
        db_exec("UPDATE rides SET status=?, delivered_at=? WHERE id=?", (status, time.time(), ride_id))
    else:
        db_exec("UPDATE rides SET status=? WHERE id=?", (status, ride_id))


def active_ride(uid):
    rows = db_query("SELECT * FROM rides WHERE passenger_id=? AND status IN ('waiting','picked') "
                    "ORDER BY id DESC LIMIT 1", (uid,))
    return rows[0] if rows else None


def log_event(tx, action, n):
    db_exec("INSERT INTO load_events(ts, taxi_id, driver_id, action, count, riders_after, capacity, lat, lon) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), tx["id"], tx["driver_id"], action, n, tx["riders"], tx["capacity"],
             tx["lat"], tx["lon"]))


# ------------------------------- أدوات هندسية -------------------------------
def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def point_on_line(t, lateral=0.0):
    lat = START[0] + (END[0] - START[0]) * t
    lon = START[1] + (END[1] - START[1]) * t
    dx, dy = END[0] - START[0], END[1] - START[1]
    n = math.hypot(dx, dy)
    return lat + (-dy / n) * lateral, lon + (dx / n) * lateral


def project_t(p):
    dx, dy = END[0] - START[0], END[1] - START[1]
    return ((p[0] - START[0]) * dx + (p[1] - START[1]) * dy) / (dx * dx + dy * dy)


def lateral_dist(p):
    dx, dy = END[0] - START[0], END[1] - START[1]
    return abs((p[0] - START[0]) * dy - (p[1] - START[1]) * dx) / math.hypot(dx, dy)


# ================================ الحالة الحية ================================
def init_taxis():
    for i in range(1, N_TAXIS + 1):
        lat, lon = point_on_line(random.random())
        taxis.append({"id": i, "lat": lat, "lon": lon, "dir": random.choice([1, -1]),
                      "path": None, "idx": 0, "riders": 0, "capacity": DEFAULT_SEATS,
                      "driver_id": None, "driver_name": None, "ride_ids": [], "seen": 0})


def my_taxi(uid):
    """التكسي الذي يقوده هذا السائق الآن (يُستدعى والقفل `lock` محجوز)."""
    return next((t for t in taxis if t["driver_id"] == uid), None)


def clusters():
    """تجميع الركاب المنتظرين في خلايا وإرجاع [(lat, lon, count)] مرتبة بالأكثر كثافة."""
    cells = {}
    for p in waiting:
        cells.setdefault((round(p["lat"] / GRID), round(p["lon"] / GRID)), []).append(p)
    out = [(sum(p["lat"] for p in pts) / len(pts), sum(p["lon"] for p in pts) / len(pts), len(pts))
           for pts in cells.values()]
    return sorted(out, key=lambda c: -c[2])


def suggest_route():
    """مسار START → تجمّعات الركاب (مرتبة على امتداد الخط) → END."""
    hot = [c for c in clusters()
           if c[2] >= MIN_CLUSTER and 0 < project_t(c) < 1 and lateral_dist(c) <= MAX_DETOUR]
    hot.sort(key=project_t)
    return [START] + [(c[0], c[1]) for c in hot] + [END]


def make_path(tx):
    t = project_t((tx["lat"], tx["lon"]))
    mid = suggest_route()[1:-1]
    if tx["dir"] == 1:
        return [p for p in mid if project_t(p) > t] + [END]
    return [p for p in mid if project_t(p) < t][::-1] + [START]


def simulation_step():
    """التكسيات الآلية فقط تتحرك وتحمّل تلقائياً؛ تكسي السائق المسجّل يُدار بيده وبموقعه الفعلي."""
    now = time.time()
    for tx in taxis:                                  # سائق انقطعت إشاراته: يعود التكسي آلياً
        if tx["driver_id"] and now - tx["seen"] > DRIVER_TIMEOUT:
            tx.update(driver_id=None, driver_name=None, path=None)
    for p in [p for p in waiting if p["real"] and now - p["t"] > REQUEST_TTL]:
        waiting.remove(p)
        ride_set(p["ride_id"], "expired")

    if SIMULATE_PASSENGERS:
        sims = sum(1 for p in waiting if not p["real"])
        for _ in range(random.randint(0, 2)):
            if sims >= MAX_SIM_PASSENGERS:
                break
            t, off = random.choice(HOTSPOTS)
            lat, lon = point_on_line(t, off + random.uniform(-0.002, 0.002))
            waiting.append({"id": f"T{stats['next_sim']}", "lat": lat, "lon": lon,
                            "real": False, "pid": None, "ride_id": None, "t": now})
            stats["next_sim"] += 1
            sims += 1

    for tx in taxis:
        if tx["driver_id"]:
            continue
        if tx["path"] is None:
            tx["path"], tx["idx"] = make_path(tx), 0
        target = tx["path"][tx["idx"]]
        d = dist((tx["lat"], tx["lon"]), target)
        if d <= STEP:
            tx["lat"], tx["lon"] = target
            tx["idx"] += 1
            if tx["idx"] >= len(tx["path"]):          # انتهت الرحلة: إنزال الجميع
                stats["delivered"] += tx["riders"]
                for rid in tx["ride_ids"]:
                    ride_set(rid, "delivered")
                tx["riders"], tx["ride_ids"] = 0, []
                tx["dir"], tx["path"] = -tx["dir"], None
        else:
            tx["lat"] += (target[0] - tx["lat"]) * STEP / d
            tx["lon"] += (target[1] - tx["lon"]) * STEP / d
        for p in waiting[:]:
            if tx["riders"] < tx["capacity"] and dist((tx["lat"], tx["lon"]), (p["lat"], p["lon"])) < PICKUP_RADIUS:
                tx["riders"] += 1
                waiting.remove(p)
                if p["real"]:
                    tx["ride_ids"].append(p["ride_id"])
                    ride_set(p["ride_id"], "picked", tx["id"], None)


def simulation_loop():
    while True:
        with lock:
            simulation_step()
        time.sleep(SIM_TICK)


# ------------------------------ عمليات الراكب ------------------------------
def add_request(uid, lat, lon):
    with lock:
        for p in [p for p in waiting if p.get("pid") == uid]:       # طلب واحد لكل راكب
            waiting.remove(p)
            ride_set(p["ride_id"], "cancelled")
        rid = ride_create(uid, lat, lon)
        waiting.append({"id": rid, "lat": lat, "lon": lon, "real": True,
                        "pid": uid, "ride_id": rid, "t": time.time()})


def cancel_request(uid):
    with lock:
        found = False
        for p in [p for p in waiting if p.get("pid") == uid]:
            waiting.remove(p)
            ride_set(p["ride_id"], "cancelled")
            found = True
    return found


# ------------------------------ عمليات السائق ------------------------------
def ensure_taxi(user):
    """تكسي السائق؛ يُخصَّص له أول تكسي متاح تلقائياً عند دخوله (يُستدعى والقفل محجوز)."""
    tx = my_taxi(user["id"])
    if tx is None:
        tx = next((t for t in taxis if t["driver_id"] is None and t["riders"] == 0), None)
        if tx is None:
            return None
        tx.update(driver_id=user["id"], driver_name=user["name"], capacity=DEFAULT_SEATS,
                  path=None, idx=0, ride_ids=[])
    tx["seen"] = time.time()
    return tx


def release_taxi(user):
    """عند تبديل الحساب: يُحرَّر التكسي بشرط ألا يكون فيه ركاب. يرجع رسالة عند المنع."""
    with lock:
        tx = my_taxi(user["id"])
        if not tx:
            return None
        if tx["riders"] > 0:
            return f"أنزل الركاب أولاً (في الحافلة {tx['riders']} راكب) ثم بدّل الحساب."
        tx.update(driver_id=None, driver_name=None, path=None, ride_ids=[])
    return None


def load_unload(user, action, n):
    """تحميل/تنزيل: يحدد السائق عدد الركاب في كل عملية."""
    with lock:
        tx = ensure_taxi(user)
        if not tx:
            return "لا توجد حافلة متاحة الآن."
        cap = tx["capacity"]
        new = tx["riders"] + n if action == "load" else tx["riders"] - n
        if new < 0:
            return f"لا يوجد في الحافلة سوى {tx['riders']} راكب."
        if new > cap:
            return f"المقاعد المتاحة {cap - tx['riders']} فقط (سعة الحافلة {cap})."
        pos = (tx["lat"], tx["lon"])
        if action == "load":
            near = sorted((p for p in waiting if dist(pos, (p["lat"], p["lon"])) <= LOAD_RADIUS),
                          key=lambda p: (not p["real"], dist(pos, (p["lat"], p["lon"]))))[:n]
            for p in near:                     # الركاب المحمّلون يخرجون من قائمة الانتظار
                waiting.remove(p)
                if p["real"]:
                    ride_set(p["ride_id"], "picked", tx["id"], user["id"])
                    tx["ride_ids"].append(p["ride_id"])
        else:
            for rid in tx["ride_ids"][:n]:
                ride_set(rid, "delivered")
            tx["ride_ids"] = tx["ride_ids"][n:]
            stats["delivered"] += n
        tx["riders"] = new
        log_event(tx, action, n)
        word = "تحميل" if action == "load" else "تنزيل"
        return f"تم {word} {n}. الركاب الآن {new} من {cap}، المقاعد الشاغرة {cap - new}."


# ------------------------------ نصوص وجداول ------------------------------
STATUS_AR = {"waiting": "ينتظر", "picked": "في التكسي", "delivered": "تم التوصيل",
             "cancelled": "ملغى", "expired": "انتهى الطلب"}


def fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def stamp():
    return time.strftime("%H:%M:%S")


def taxi_status(tx):
    if tx["riders"] == 0:
        return "فارغ"
    if tx["riders"] >= tx["capacity"]:
        return "ممتلئ"
    return f"{tx['riders']} من {tx['capacity']}"


def taxi_direction(tx):
    return "ذهاب إلى الحي" if tx["dir"] == 1 else "عودة إلى وسط البلد"


def taxi_rows(admin=False):
    rows = []
    for t in taxis:
        row = {"التكسي": t["id"], "الركاب": taxi_status(t), "مقاعد شاغرة": t["capacity"] - t["riders"],
               "عدد المقاعد": t["capacity"], "الاتجاه": taxi_direction(t),
               "البعد عن وسط البلد (كم)": round(dist((t["lat"], t["lon"]), START) * 111, 1)}
        if admin:
            row["السائق"] = t["driver_name"] or "آلي (تجريبي)"
        rows.append(row)
    return rows


def cluster_rows(tx):
    ref = (tx["lat"], tx["lon"]) if tx else START
    return [{"التجمّع": i + 1, "عدد الركاب المنتظرين": c[2],
             "البعد عنك (كم)": round(dist(ref, (c[0], c[1])) * 111, 1)}
            for i, c in enumerate(clusters()[:10])]


def totals():
    real = sum(1 for p in waiting if p["real"])
    return {"waiting": len(waiting), "real": real, "sim": len(waiting) - real,
            "riding": sum(t["riders"] for t in taxis), "capacity": sum(t["capacity"] for t in taxis),
            "delivered": stats["delivered"]}


def info_text(role, user=None, tx=None):
    tt = totals()
    if role == "passenger":
        free = sum(1 for t in taxis if t["riders"] < t["capacity"])
        msg = f"تكسيات فيها مقاعد شاغرة: {free} من {len(taxis)}"
        ride = active_ride(user["id"]) if user else None
        if ride and ride["status"] == "picked":
            msg += f" | أنت الآن في التكسي رقم {ride['taxi_id']}"
        elif ride:
            mine = next((p for p in waiting if p["pid"] == user["id"]), None)
            cand = [dist((t["lat"], t["lon"]), (mine["lat"], mine["lon"]))
                    for t in taxis if t["riders"] < t["capacity"]] if mine else []
            msg += " | طلبك مسجّل وينتظر"
            if cand:
                msg += f"، أقرب تكسي شاغر ≈ {round(min(cand) * 111, 1)} كم"
        return f"{msg} | آخر تحديث {stamp()}"
    if role == "driver":
        head = f"إجمالي الركاب المنتظرين: {tt['waiting']} (طلبات حقيقية: {tt['real']})"
        if not tx:
            return f"{head} | لا توجد حافلة متاحة الآن، سيُخصَّص لك تكسي تلقائياً عند توفره | {stamp()}"
        return (f"{head} | تكسيك رقم {tx['id']}: الركاب {tx['riders']} من {tx['capacity']}، "
                f"شاغر {tx['capacity'] - tx['riders']} | محطات المسار المقترح: "
                f"{len(suggest_route()) - 2} | آخر تحديث {stamp()}")
    return (f"الركاب المنتظرون: {tt['waiting']} (حقيقي {tt['real']} / تجريبي {tt['sim']}) | "
            f"داخل التكسيات: {tt['riding']}/{tt['capacity']} | تم توصيلهم: {tt['delivered']} | "
            f"تكسيات يقودها سائقون مسجّلون: {sum(1 for t in taxis if t['driver_id'])} | آخر تحديث {stamp()}")


def columns_of(rows, default):
    return [{"name": k, "id": k} for k in (rows[0].keys() if rows else default)]


# ---------------------------------- الخريطة ----------------------------------
# plotly 6 استبدل *mapbox بـ *map؛ ندعم الإصدارين
NEW_MAP = hasattr(go, "Scattermap")
ScatterMap = go.Scattermap if NEW_MAP else go.Scattermapbox
DensityMap = go.Densitymap if NEW_MAP else go.Densitymapbox


def build_map(view, taxi_id=None, me=None):
    fig = go.Figure()
    ws = list(waiting)

    if view in ("driver", "admin"):
        if ws:
            fig.add_trace(DensityMap(
                lat=[p["lat"] for p in ws], lon=[p["lon"] for p in ws], z=[1] * len(ws),
                radius=30, colorscale="YlOrRd", opacity=0.55, showscale=False,
                name="كثافة الطلب", hoverinfo="skip"))
        route = suggest_route()
        fig.add_trace(ScatterMap(
            lat=[p[0] for p in route], lon=[p[1] for p in route], mode="lines",
            line=dict(width=4, color="#1565c0"), name="المسار المقترح"))
        fig.add_trace(ScatterMap(
            lat=[START[0], END[0]], lon=[START[1], END[1]], mode="markers+text",
            marker=dict(size=14, color="black"), text=["وسط البلد", "الحي"],
            textposition="top right", name="نقاط الرحلة"))

    if view == "driver":
        cl = clusters()
        if cl:
            fig.add_trace(ScatterMap(
                lat=[c[0] for c in cl], lon=[c[1] for c in cl], mode="markers+text",
                marker=dict(size=[16 + 4 * c[2] for c in cl], color="#e53935", opacity=0.75),
                text=[str(c[2]) for c in cl], textfont=dict(color="white", size=13),
                hovertext=[f"{c[2]} ركاب ينتظرون" for c in cl], name="ركاب منتظرون"))

    if view == "admin":
        for real, color, label in ((True, "#c62828", "طلبات حقيقية"), (False, "#fb8c00", "ركاب تجريبيون")):
            pts = [p for p in ws if p["real"] == real]
            if pts:
                fig.add_trace(ScatterMap(
                    lat=[p["lat"] for p in pts], lon=[p["lon"] for p in pts], mode="markers",
                    marker=dict(size=8, color=color), text=[f"طلب #{p['id']}" for p in pts], name=label))

    shown = [t for t in taxis if t["id"] == taxi_id] if view == "driver" else taxis
    colors = ["#2e7d32" if t["riders"] == 0 else ("#c62828" if t["riders"] >= t["capacity"] else "#f9a825")
              for t in shown]
    hover = [f"تكسي {t['id']} - {taxi_status(t)}" + (f" - {t['driver_name']}" if view == "admin" and t["driver_name"] else "")
             for t in shown]
    fig.add_trace(ScatterMap(
        lat=[t["lat"] for t in shown], lon=[t["lon"] for t in shown], mode="markers+text",
        marker=dict(size=18, color=colors), text=[str(t["id"]) for t in shown],
        textfont=dict(size=12, color="black"), hovertext=hover, name="التكسيات"))

    if view == "passenger" and me:
        fig.add_trace(ScatterMap(
            lat=[me["lat"]], lon=[me["lon"]], mode="markers+text",
            marker=dict(size=18, color="#1565c0"), text=["موقعك"], textposition="top center",
            name="موقعك"))

    layout_map = dict(style="open-street-map",
                      center=dict(lat=(START[0] + END[0]) / 2, lon=(START[1] + END[1]) / 2), zoom=12)
    fig.update_layout(
        **({"map": layout_map} if NEW_MAP else {"mapbox": layout_map}),
        margin=dict(l=0, r=0, t=0, b=0), height=560, legend=dict(x=0.01, y=0.99), uirevision="keep")
    return fig


# ================================ التطبيق العام ================================
BTN = {"fontSize": 17, "padding": "10px 18px", "borderRadius": 10, "border": "none",
       "cursor": "pointer", "color": "white"}
INP = {"padding": 8, "fontSize": 16, "borderRadius": 8, "border": "1px solid #bbb"}
ROW = {"display": "flex", "gap": 10, "alignItems": "center", "flexWrap": "wrap", "margin": "6px 0"}
HIDE = {"display": "none"}
ROUND = {"width": 54, "height": 54, "borderRadius": 27, "fontSize": 30, "padding": 0}

app = Dash(__name__, title="التكسيات")


def serve_layout():
    return html.Div(dir="rtl", style={"fontFamily": "Tahoma, Arial", "padding": 12}, children=[
        dcc.Store(id="user", storage_type="local"),     # الحساب يبقى محفوظاً في المتصفح
        dcc.Geolocation(id="geo", update_now=False, high_accuracy=True, maximum_age=0, timeout=15000),
        dcc.Interval(id="tick", interval=REFRESH_SECONDS * 1000),
        dcc.Interval(id="gps-tick", interval=REFRESH_SECONDS * 1000),
        html.H2("التكسيات: وسط البلد ⇄ الحي"),

        # ---- تسجيل الدخول (مرة واحدة) ----
        html.Div(id="login-box", style={"maxWidth": 420}, children=[
            html.H3("التسجيل / الدخول"),
            html.Div(style=ROW, children=[dcc.RadioItems(
                id="role", value="passenger", inline=True,
                options=[{"label": " راكب ", "value": "passenger"}, {"label": " سائق ", "value": "driver"}])]),
            html.Div(style=ROW, children=[dcc.Input(id="name", placeholder="الاسم", style=INP)]),
            html.Div(style=ROW, children=[dcc.Input(id="phone", placeholder="رقم الموبايل", type="tel", style=INP)]),
            html.Button("دخول", id="login-btn", n_clicks=0, style={**BTN, "background": "#1565c0"}),
            html.Div(id="login-msg", style={"marginTop": 8, "fontWeight": "bold"}),
        ]),

        # ---- الواجهة الرئيسية بعد الدخول ----
        html.Div(id="main-box", style=HIDE, children=[
            html.Div(style=ROW, children=[
                html.H4(id="welcome", style={"margin": 0}),
                html.Button("تبديل الحساب", id="logout-btn", n_clicks=0, style={**BTN, "background": "#757575"}),
                html.Span(id="top-msg", style={"fontWeight": "bold"}),
            ]),

            html.Div(id="passenger-box", style=HIDE, children=[
                html.Button("🚖 اطلب تكسي (تحديد موقعي تلقائياً)", id="req-btn", n_clicks=0,
                            style={**BTN, "background": "#2e7d32", "fontSize": 18}),
                html.Button("إلغاء الطلب", id="cancel-btn", n_clicks=0,
                            style={**BTN, "background": "#757575", "marginRight": 10}),
                html.Div(id="req-msg", style={"marginTop": 8, "fontWeight": "bold"}),
            ]),

            html.Div(id="driver-box", style=HIDE, children=[
                html.Div(style=ROW, children=[
                    html.Span("عدد الركاب:", style={"fontSize": 18}),
                    html.Button("−", id="minus-btn", n_clicks=0, style={**BTN, **ROUND, "background": "#546e7a"}),
                    dcc.Input(id="cnt", type="text", value="1", readOnly=True,
                              style={**INP, "width": 70, "textAlign": "center", "fontSize": 26, "fontWeight": "bold"}),
                    html.Button("+", id="plus-btn", n_clicks=0, style={**BTN, **ROUND, "background": "#546e7a"}),
                ]),
                html.Div(style=ROW, children=[
                    html.Button("⬆ تحميل ركاب", id="load-btn", n_clicks=0,
                                style={**BTN, "background": "#1565c0", "fontSize": 20, "padding": "14px 26px"}),
                    html.Button("⬇ تنزيل ركاب", id="unload-btn", n_clicks=0,
                                style={**BTN, "background": "#ef6c00", "fontSize": 20, "padding": "14px 26px"}),
                ]),
                html.Div(id="driver-msg", style={"fontWeight": "bold"}),
                html.Div(id="gps-msg", style={"color": "#555"}),
            ]),

            html.H4(id="info", style={"margin": "10px 0"}),
            dcc.Graph(id="map"),
            dash_table.DataTable(id="table", page_size=12,
                                 style_header={"fontWeight": "bold", "textAlign": "center"},
                                 style_cell={"textAlign": "center", "padding": 6}),
        ]),
    ])


app.layout = serve_layout


@app.callback(Output("user", "data"), Output("login-msg", "children"), Output("top-msg", "children"),
              Input("login-btn", "n_clicks"), Input("logout-btn", "n_clicks"),
              State("role", "value"), State("name", "value"), State("phone", "value"), State("user", "data"),
              prevent_initial_call=True)
def login_logout(_a, _b, role, name, phone, user):
    if ctx.triggered_id == "logout-btn":
        if user and user["role"] == "driver":
            msg = release_taxi(user)
            if msg:
                return no_update, "", msg           # لا خروج قبل إنزال الركاب
        return None, "", ""
    name = (name or "").strip()
    ph = norm_phone(phone)
    if len(name) < 2:
        return no_update, "أدخل اسمك.", no_update
    if not ph:
        return no_update, "أدخل رقم موبايل صحيحاً.", no_update
    u, is_new = register_or_login(role, name, ph)
    msg = "تم تسجيلك بنجاح." if is_new else f"أهلاً بعودتك {u['name']}."
    return {"id": u["id"], "name": u["name"], "phone": u["phone"], "role": u["role"]}, msg, ""


@app.callback(Output("login-box", "style"), Output("main-box", "style"),
              Output("passenger-box", "style"), Output("driver-box", "style"), Output("welcome", "children"),
              Input("user", "data"))
def route_ui(user):
    if not user:
        return {"maxWidth": 420}, HIDE, HIDE, HIDE, ""
    is_p = user["role"] == "passenger"
    label = "راكب" if is_p else "سائق"
    return HIDE, {}, ({"margin": "12px 0"} if is_p else HIDE), ({"margin": "12px 0"} if not is_p else HIDE), \
        f"👋 {user['name']} ({label})"


@app.callback(Output("geo", "update_now"),
              Input("req-btn", "n_clicks"), Input("gps-tick", "n_intervals"), Input("user", "data"))
def ask_location(_a, _b, user):
    if not user:
        return no_update
    if ctx.triggered_id == "req-btn":               # الراكب ضغط «اطلب تكسي»
        return True
    if user["role"] == "driver":                    # السائق: تخصيص تكسي + تحديث موقعه كل 20 ثانية
        with lock:
            ensure_taxi(user)
        return True
    return no_update


@app.callback(Output("req-msg", "children"), Output("gps-msg", "children"),
              Input("geo", "timestamp"), Input("geo", "position_error"), Input("cancel-btn", "n_clicks"),
              State("geo", "position"), State("req-btn", "n_clicks"), State("user", "data"),
              prevent_initial_call=True)
def handle_geo(_ts, err, _cancel, pos, clicks, user):
    if not user:
        return no_update, no_update
    uid = user["id"]
    bad = err or not pos or pos.get("lat") is None

    if user["role"] == "driver":                    # موقع السائق ← دبوس تكسيه
        with lock:
            tx = ensure_taxi(user)
            if not tx:
                return no_update, no_update
            if bad:
                return no_update, "تعذّر تحديد موقعك: فعّل خدمة الموقع ليتحدث دبوس تكسيك."
            tx["lat"], tx["lon"] = pos["lat"], pos["lon"]
        return no_update, f"تم تحديث موقع تكسيك {stamp()}"

    if ctx.triggered_id == "cancel-btn":
        return ("تم إلغاء طلبك." if cancel_request(uid) else "لا يوجد طلب لإلغائه."), no_update
    if not clicks or clicks <= handled_clicks.get(uid, 0):
        return no_update, no_update                 # تحديث موقع تلقائي عند فتح الصفحة وليس طلباً
    handled_clicks[uid] = clicks
    ride = active_ride(uid)
    if ride and ride["status"] == "picked":
        return f"أنت الآن داخل التكسي رقم {ride['taxi_id']}.", no_update
    if bad:
        return "تعذّر تحديد موقعك. فعّل خدمة الموقع واسمح للمتصفح بالوصول إليه ثم أعد المحاولة.", no_update
    add_request(uid, pos["lat"], pos["lon"])
    return "✅ تم تسجيل طلبك وتحديد موقعك، وسيظهر للسائقين والإدارة.", no_update


@app.callback(Output("cnt", "value"), Input("plus-btn", "n_clicks"), Input("minus-btn", "n_clicks"),
              State("cnt", "value"), prevent_initial_call=True)
def change_count(_p, _m, cnt):
    try:
        cnt = int(cnt)
    except (TypeError, ValueError):
        cnt = 1
    cnt += 1 if ctx.triggered_id == "plus-btn" else -1
    return str(max(1, min(DEFAULT_SEATS, cnt)))


@app.callback(Output("driver-msg", "children"),
              Input("load-btn", "n_clicks"), Input("unload-btn", "n_clicks"),
              State("user", "data"), State("cnt", "value"), prevent_initial_call=True)
def driver_actions(_a, _b, user, cnt):
    if not user or user["role"] != "driver":
        return no_update
    try:
        cnt = int(cnt)
    except (TypeError, ValueError):
        return "حدد عدد الركاب."
    return load_unload(user, "load" if ctx.triggered_id == "load-btn" else "unload", cnt)


@app.callback(Output("map", "figure"), Output("table", "data"), Output("table", "columns"),
              Output("info", "children"),
              Input("tick", "n_intervals"), Input("user", "data"), Input("req-msg", "children"),
              Input("driver-msg", "children"), Input("gps-msg", "children"))
def refresh(_n, user, _m1, _m2, _m3):
    if not user:
        return no_update, no_update, no_update, no_update
    with lock:
        if user["role"] == "driver":
            tx = ensure_taxi(user)
            fig = build_map("driver", tx["id"] if tx else None)
            rows, default = cluster_rows(tx), ["التجمّع", "عدد الركاب المنتظرين", "البعد عنك (كم)"]
            info = info_text("driver", user, tx)
        else:
            me = next((p for p in waiting if p["pid"] == user["id"]), None)
            fig = build_map("passenger", None, me)
            rows, default = taxi_rows(False), ["التكسي"]
            info = info_text("passenger", user)
    return fig, rows, columns_of(rows, default), info


# ============================ تطبيق الإدارة (منفصل) ============================
# لوحة الإدارة: تطبيق Dash ثانٍ على نفس الخادم بمسار /admin/ ومحمية بكلمة مرور
admin = Dash(__name__, server=app.server, url_base_pathname="/admin/", title="إدارة التكسيات")


@app.server.before_request
def require_admin_login():
    if not request.path.startswith("/admin"):
        return None
    a = request.authorization
    ok = (a is not None
          and hmac.compare_digest((a.username or "").encode(), ADMIN_USER.encode())
          and hmac.compare_digest((a.password or "").encode(), ADMIN_PASSWORD.encode()))
    if not ok:
        return Response("دخول الإدارة فقط", 401, {"WWW-Authenticate": 'Basic realm="Taxi Admin"'})
    return None


def datatable(title, rows, headers, page_size=10):
    return html.Div([
        html.H4(f"{title} ({len(rows)})"),
        dash_table.DataTable(data=rows, columns=columns_of(rows, headers), page_size=page_size,
                             style_header={"fontWeight": "bold", "textAlign": "center"},
                             style_cell={"textAlign": "center", "padding": 6})])


def admin_tables():
    with lock:
        on_shift = {t["driver_id"]: t["id"] for t in taxis if t["driver_id"]}
        t_rows = taxi_rows(admin=True)
        r_rows = [{"رقم الطلب": p["id"], "النوع": "حقيقي" if p["real"] else "تجريبي",
                   "مدة الانتظار (دقيقة)": round((time.time() - p["t"]) / 60, 1),
                   "خط العرض": round(p["lat"], 5), "خط الطول": round(p["lon"], 5)}
                  for p in sorted(waiting, key=lambda p: p["t"])]
    passengers = [{"الاسم": u["name"], "الجوال": u["phone"], "تاريخ التسجيل": fmt_ts(u["created_at"])}
                  for u in db_query("SELECT * FROM users WHERE role='passenger' ORDER BY created_at DESC")]
    drivers = [{"الاسم": u["name"], "الجوال": u["phone"], "تاريخ التسجيل": fmt_ts(u["created_at"]),
                "التكسي الحالي": on_shift.get(u["id"], "—")}
               for u in db_query("SELECT * FROM users WHERE role='driver' ORDER BY created_at DESC")]
    rides = [{"رقم": r["id"], "الراكب": r["pname"] or "—", "جوال الراكب": r["pphone"] or "—",
              "الحالة": STATUS_AR.get(r["status"], r["status"]), "التكسي": r["taxi_id"] or "—",
              "السائق": r["dname"] or "—", "وقت الطلب": fmt_ts(r["requested_at"]),
              "وقت الركوب": fmt_ts(r["picked_at"]), "وقت التوصيل": fmt_ts(r["delivered_at"])}
             for r in db_query(
                 "SELECT r.*, p.name AS pname, p.phone AS pphone, d.name AS dname FROM rides r "
                 "LEFT JOIN users p ON p.id=r.passenger_id LEFT JOIN users d ON d.id=r.driver_id "
                 "ORDER BY r.id DESC LIMIT 100")]
    events = [{"الوقت": fmt_ts(e["ts"]), "التكسي": e["taxi_id"], "السائق": e["dname"] or "—",
               "العملية": "تحميل" if e["action"] == "load" else "تنزيل", "العدد": e["count"],
               "الركاب بعدها": e["riders_after"], "عدد المقاعد": e["capacity"]}
              for e in db_query(
                  "SELECT e.*, d.name AS dname FROM load_events e LEFT JOIN users d ON d.id=e.driver_id "
                  "ORDER BY e.id DESC LIMIT 100")]
    return [
        datatable("حالة التكسيات", t_rows, ["التكسي"], 12),
        datatable("طلبات الركاب المنتظرين الآن", r_rows, ["رقم الطلب", "النوع"], 10),
        datatable("سجل التحميل والتنزيل", events, ["الوقت", "التكسي", "السائق"], 10),
        datatable("سجل الرحلات", rides, ["رقم", "الراكب"], 10),
        datatable("السائقون المسجّلون", drivers, ["الاسم", "الجوال"], 10),
        datatable("الركاب المسجّلون", passengers, ["الاسم", "الجوال"], 10),
    ]


def admin_layout():
    return html.Div(dir="rtl", style={"fontFamily": "Tahoma, Arial", "padding": 12}, children=[
        html.H2("لوحة إدارة التكسيات (للإدارة فقط)"),
        html.H4(id="a-info", style={"margin": "10px 0"}),
        dcc.Graph(id="a-map"),
        html.Div(id="a-tables"),
        dcc.Interval(id="a-tick", interval=REFRESH_SECONDS * 1000),
    ])


admin.layout = admin_layout


@admin.callback(Output("a-map", "figure"), Output("a-info", "children"), Output("a-tables", "children"),
                Input("a-tick", "n_intervals"))
def admin_refresh(_):
    with lock:
        fig, info = build_map("admin"), info_text("admin")
    return fig, info, admin_tables()


# ==================================== التشغيل ====================================
_started = False


def bootstrap():
    """يُستدعى مرة واحدة عند الاستيراد (يعمل مع python مباشرة ومع gunicorn)."""
    global _started
    if _started:
        return
    _started = True
    init_db()
    init_taxis()
    threading.Thread(target=simulation_loop, daemon=True).start()


bootstrap()
server = app.server          # لـ gunicorn:  gunicorn taxi_app:server

if __name__ == "__main__":
    print(f"التطبيق: http://127.0.0.1:{PUBLIC_PORT}   |   الإدارة: http://127.0.0.1:{PUBLIC_PORT}/admin/ "
          f"(المستخدم: {ADMIN_USER})   |   قاعدة البيانات: {DB_PATH}")
    app.run(host="0.0.0.0", port=PUBLIC_PORT, debug=False)
