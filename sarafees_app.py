"""
حي بطن الهوي - سرفيس: تطبيق ركاب وسائقين (Dash + Leaflet)  ← نسخة معدّلة من f.py

التشغيل محلياً:
    pip install -r requirements.txt
    python sarafees_app.py            ثم افتح http://127.0.0.1:8050

النشر على Render (أمر التشغيل):
    gunicorn sarafees_app:server --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT
    (workers = 1 لأن الحالة الحية محفوظة في ذاكرة العملية)

متغيرات البيئة (اختيارية):
    DRIVER_CODES     رموز السائقين مفصولة بفاصلة، مثال: 4821,7730,1956   (لكل رمز حافلة)
    ADMIN_CODE       رمز دخول لوحة الإدارة (الافتراضي: 2468؛ غيّره قبل النشر برمز طويل)
    REFRESH_SECONDS  فترة تحديث الدبابيس بالثواني (الافتراضي 20)

ملاحظة: تحديد الموقع في المتصفح يعمل على https أو localhost فقط (Render يوفر https).
"""
import hmac
import math
import os
import secrets
import threading
import time
import uuid

import dash
import dash_leaflet as dl
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update

# ================================ الإعدادات ================================
CENTER = [31.9038, 35.2070]      # مركز الخريطة الابتدائي (كما في الكود الأصلي)
CAPACITY = 7                     # عدد ركاب الحافلة
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", 20))
PASSENGER_TTL = 15 * 60          # ينتهي طلب الراكب تلقائياً بعد 15 دقيقة
BUS_TTL = 120                    # حافلة لم تصل منها إشارة خلال دقيقتين تُخفى
LOAD_RADIUS = 0.006              # عند «تحميل راكب» يُحذف أقرب طلب ضمن ≈ 650 م
DRIVER_CODES = [c.strip() for c in os.environ.get("DRIVER_CODES", "1234,5678,9999").split(",") if c.strip()]
ADMIN_CODE = os.environ.get("ADMIN_CODE", "2468").strip()
# ===========================================================================

lock = threading.Lock()
passengers = {}                  # معرّف المتصفح -> {name, lat, lon, t}
request_records = {}             # سجل واحد لكل طلب (معرّف الطلب rid)، يتحدث دون تكرار الأحداث
admin_tokens = set()             # جلسات الإدارة الصالحة (تُفقد عند إعادة تشغيل الخادم)
buses = {code: {"label": f"حافلة {i}", "lat": None, "lon": None, "riders": 0, "seen": 0.0}
         for i, code in enumerate(DRIVER_CODES, 1)}

app = dash.Dash(
    __name__, suppress_callback_exceptions=True, title=" سرفيس حي بطن الهوى",
    meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}],
)
server = app.server

app.index_string = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
{%metas%}<title>{%title%}</title>{%favicon%}{%css%}
<style>
  body{margin:0;font-family:Tahoma,Arial,sans-serif;background:#f4f6f8;color:#2c3e50}
  .navbar{display:flex;justify-content:space-between;align-items:center;padding:10px 16px;
          background:#fff;border-bottom:2px solid #eee;position:sticky;top:0;z-index:1000}
  .brand{font-weight:bold;font-size:22px}
  .pill{background:#e67e22;color:#fff;padding:8px 16px;border-radius:20px;text-decoration:none;
        font-weight:bold;border:none;font-size:15px;cursor:pointer}
  .title{text-align:center;margin:16px 8px 8px}
  .card{max-width:460px;margin:14px auto;padding:16px;background:#fff;border-radius:14px;
        box-shadow:0 1px 6px rgba(0,0,0,.12);box-sizing:border-box;width:calc(100% - 24px)}
  .field{
width:100%;
box-sizing:border-box;
padding:12px;
height:50px;
line-height:24px;
font-size:15px;
border:1px solid #ccc;
border-radius:10px;
margin:6px 0 10px;
}
  .btn{display:block;width:100%;padding:14px;font-size:18px;border:none;border-radius:10px;
       color:#fff;margin-top:10px;cursor:pointer;font-family:inherit}
  .btn:active{opacity:.8}
  .blue{background:#3498db}.green{background:#2ecc71}.red{background:#e74c3c}
  .purple{background:#5322e6}.orange{background:#ef6c00}
  .status{text-align:center;font-weight:bold;margin-top:10px;min-height:20px}
  .err{color:#c0392b}
  .mapwrap{width:calc(100% - 16px);max-width:1400px;margin:8px auto 16px;border-radius:12px;overflow:hidden}
  .passenger-card{max-width:760px;margin:8px auto;padding:10px 16px}
  .passenger-layout{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(300px,.8fr);gap:12px;align-items:start;width:calc(100% - 20px);max-width:1500px;margin:8px auto}
  .passenger-layout .mapwrap{width:100%;max-width:none;margin:0}
  .passenger-layout .card{width:100%;max-width:none;margin:0}
  @media(max-width:850px){.passenger-layout{grid-template-columns:1fr}.passenger-layout .mapwrap{order:0}.passenger-layout .card{order:1}}
  .leaflet-container{direction:ltr}
  .row{display:flex;gap:10px}.row .btn{margin-top:0}
  .big{font-size:30px;font-weight:bold;text-align:center;margin:6px 0}
  @media(min-width:900px){.card{max-width:520px}}
  .admin-layout{display:grid;grid-template-columns:minmax(0,1.7fr) minmax(300px,.9fr);gap:12px;align-items:start;
                width:calc(100% - 20px);max-width:1600px;margin:8px auto}
  .admin-layout .mapwrap{width:100%;max-width:none;margin:0}
  .sidepanel{background:#fff;border-radius:14px;box-shadow:0 1px 6px rgba(0,0,0,.12);padding:8px 12px;box-sizing:border-box}
  .sec{font-size:13px;font-weight:bold;margin:8px 0 4px;padding-bottom:3px;border-bottom:1px solid #eee}
  .logbox{max-height:18vh;overflow:auto}
  .logline{font-size:12px;padding:3px 0;border-bottom:1px dotted #e3e3e3;line-height:1.5}
  .small{font-size:12px;font-weight:normal;margin-top:4px}
  .mini{border:1px solid #e3e3e3;border-radius:10px;padding:8px;margin:6px 0;font-size:13px}
  .mini .field{height:38px;font-size:13px;margin:6px 0}
  .mini .btn{padding:8px;font-size:14px;margin-top:0}
  .ok{color:#1e8449}.no{color:#c0392b}
  @media(max-width:850px){.admin-layout{grid-template-columns:1fr}}
</style>
</head>
<body>
{%app_entry%}
<footer>{%config%}{%scripts%}{%renderer%}</footer>
</body>
</html>"""


# ================================ أدوات ================================
def stamp():
    return time.strftime("%H:%M:%S")


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def prune():
    """يُستدعى والقفل محجوز: حذف الطلبات القديمة (الطلب المنتهي دون قرار يُسجَّل «منتهي»)."""
    now = time.time()
    for pid in [k for k, p in passengers.items() if now - p["t"] > PASSENGER_TTL]:
        if passengers[pid].get("status") in ("pending", "accepted"):
            update_record(passengers[pid]["rid"], status="expired")
        del passengers[pid]


def new_record(rid, name, lat, lon):
    request_records[rid] = {"rid": rid, "name": name, "lat": lat, "lon": lon, "status": "pending",
                            "eta": "", "bus": "", "t": time.time(), "time": stamp()}
    while len(request_records) > 300:                       # نحتفظ بآخر 300 طلب فقط
        del request_records[next(iter(request_records))]


def update_record(rid, **fields):
    rec = request_records.get(rid)
    if rec:
        rec.update(fields)
        rec["t"], rec["time"] = time.time(), stamp()


def valid_admin(adm):
    return bool(adm) and adm.get("token") in admin_tokens


def circle(lat, lon, color, radius, tip):
    return dl.CircleMarker(center=[lat, lon], radius=radius, color=color, fillColor=color, fillOpacity=0.8,
                           children=[dl.Tooltip(tip), dl.Popup(tip)])


def bus_of(drv):
    if not drv:
        return None
    return buses.get(drv.get("code"))


def navbar(right=None):
    return html.Div([
        html.Div("🚌  سرفيس حي بطن الهوي", className="brand"),
        html.Div([dcc.Link("🏠 الرئيسية", href="/", className="pill"),
                  right or dcc.Link("👨‍✈️ دخول السائق", href="/driver-login", className="pill"),
                  dcc.Link("🛠 الإدارة", href="/admin", className="pill")], className="row"),
    ], className="navbar")


# ================================ واجهة الراكب ================================
def passenger_layout():
    return html.Div([
        navbar(),
        html.H2("واجهة الراكب - طلب سرفيس", className="title"),
        html.Div([
          html.Div([html.H4("🚌 مواقع الحافلات والسائقين", className="title"),
            html.Div(id="bus-info", className="status"),
            dl.Map([dl.TileLayer(), dl.LayerGroup(id="buses-markers")], id="passenger-map",
                   center=CENTER, zoom=14, style={"height": "72vh", "minHeight": "420px"})], className="mapwrap"),
          html.Div([
            html.Label("اسم الراكب:"),
            dcc.Input(id="passenger-name", type="text", placeholder="أدخل اسمك هنا", className="field",
                      persistence=True, persistence_type="local"),
            html.Button("📍 تحديد موقعي الحالي (GPS)", id="get-loc-btn", n_clicks=0, className="btn blue"),
            html.Div("لم يتم تحديد الموقع بعد", id="location-status", className="status"),
            html.Button("✅ إرسال الطلب", id="submit-btn", n_clicks=0, className="btn green"),
            html.Button("❌ إلغاء الطلب", id="cancel-btn", n_clicks=0, className="btn red"),
            html.Div(id="submit-output", className="status"),
            html.Div(id="request-response", className="status"),
          ], className="card passenger-card"),
        ], className="passenger-layout"),
        dcc.Interval(id="buses-interval", interval=REFRESH_SECONDS * 1000, n_intervals=0),
    ])


# ================================ دخول السائق ================================
def driver_login_layout():
    return html.Div([
        navbar(dcc.Link("🏠 الراكب", href="/", className="pill")),
        html.Div([
            html.Div("🔐", style={"fontSize": 50, "textAlign": "center"}),
            html.H2("تسجيل دخول السائقين", className="title"),
            dcc.Input(id="driver-code-input", type="password", placeholder="رمز السائق", className="field"),
            html.Button("🚀 دخول للخريطة", id="driver-login-btn", n_clicks=0, className="btn purple"),
            html.Div(id="login-output", className="status err"),
        ], className="card"),
    ])


def admin_login_layout():
    return html.Div([
        navbar(), html.H2("🛠 دخول الإدارة", className="title"),
        html.Div([dcc.Input(id="admin-code", type="password", placeholder="رمز الإدارة", className="field"),
                  html.Button("دخول الإدارة", id="admin-login", n_clicks=0, className="btn purple"),
                  html.Div(id="admin-login-output", className="status err")], className="card"),
    ])


def admin_board_layout():
    logout = html.Button("🚪 خروج الإدارة", id="admin-logout", n_clicks=0, className="pill")
    legend = "🟠 بانتظار القرار   🔵 مقبول   ⚪ مرفوض   🟢🟠🔴 حافلة متصلة (حسب المقاعد الشاغرة)   ⚫ حافلة غير متصلة"
    return html.Div([
        navbar(logout),
        html.H2("🛠 لوحة الإدارة — مواقع الركاب والسائقين والحركات", className="title"),
        html.Div([
            html.Div([
                html.Div(id="admin-summary", className="status"),
                html.Div(dl.Map([dl.TileLayer(), dl.LayerGroup(id="admin-markers")], id="admin-map",
                                center=CENTER, zoom=14, style={"height": "72vh", "minHeight": "420px"}),
                         className="mapwrap"),
                html.Div(legend, className="status small"),
            ]),
            html.Div([
                html.Div("⏳ طلبات بانتظار قرارك", className="sec"),
                html.Div(id="admin-pending"),
                html.Div("✅ الحركات المقبولة", className="sec ok"),
                html.Div(id="admin-accepted", className="logbox"),
                html.Div("❌ الحركات المرفوضة", className="sec no"),
                html.Div(id="admin-rejected", className="logbox"),
                html.Div("🚌 السائقون", className="sec"),
                html.Div(id="admin-fleet", className="logbox"),
                html.Div(id="admin-msg", className="status small"),
            ], className="sidepanel"),
        ], className="admin-layout"),
        dcc.Store(id="pending-sig"),
        dcc.Interval(id="admin-interval", interval=REFRESH_SECONDS * 1000, n_intervals=0),
    ])


# ================================ واجهة السائق ================================
def driver_map_layout(bus):
    logout = html.Button("🚪 خروج", id="logout-btn", n_clicks=0, className="pill")
    return html.Div([
        navbar(logout),
        html.H2(f"👨‍✈️ واجهة السائق - {bus['label']}", className="title"),
        html.Div([
            html.Div(id="bus-status", className="big"),
            html.Div(id="passenger-count", className="status"),
            html.Div(style={"height": 8}),
            html.Div([
                html.Button("⬆ تحميل راكب", id="board-btn", n_clicks=0, className="btn orange"),
                html.Button("⬇ تنزيل راكب", id="alight-btn", n_clicks=0, className="btn blue"),
            ], className="row"),
            html.Div("جارٍ تحديد موقعك…", id="gps-status", className="status"),
        ], className="card"),
        html.Div(dl.Map([dl.TileLayer(), dl.LayerGroup(id="passengers-markers")], id="driver-map",
                        center=CENTER, zoom=14, style={"height": "55vh", "minHeight": "320px"}),
                 className="mapwrap"),
        dcc.Store(id="driver-pos"),
        dcc.Interval(id="driver-tick", interval=REFRESH_SECONDS * 1000, n_intervals=0),
    ])


# ================================ الهيكل العام ================================
app.layout = html.Div([
    dcc.Location(id="url", refresh=False),
    dcc.Store(id="pid"),                                     # معرّف هذا المتصفح (يُحفظ في localStorage)
    dcc.Store(id="passenger-pos"),                           # موقع الراكب بعد ضغط GPS
    dcc.Store(id="driver", storage_type="local"),            # جلسة السائق
    dcc.Store(id="admin", storage_type="session"),           # جلسة الإدارة (رمز جلسة وليس الرمز السري)
    html.Div(id="page-content"),
])


@app.callback(Output("page-content", "children"), Input("url", "pathname"), Input("driver", "data"),
              Input("admin", "data"))
def display_page(path, drv, adm):
    if path == "/admin":
        return admin_board_layout() if valid_admin(adm) else admin_login_layout()
    if path and path.startswith("/driver"):
        bus = bus_of(drv)
        return driver_map_layout(bus) if bus else driver_login_layout()   # لا دخول للخريطة بدون رمز
    return passenger_layout()


# ------------------------- معرّف المتصفح (clientside) -------------------------
app.clientside_callback(
    """
    function(_){
        const mk = () => (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
                         : "p" + Date.now() + Math.random().toString(16).slice(2);
        try{
            let id = window.localStorage.getItem("sarafees_pid");
            if(!id){ id = mk(); window.localStorage.setItem("sarafees_pid", id); }
            return id;
        }catch(e){ return mk(); }
    }
    """,
    Output("pid", "data"), Input("url", "pathname"),
)


# ================================ تسجيل دخول السائق ================================
@app.callback(Output("driver", "data"), Output("url", "pathname"), Output("login-output", "children"),
              Input("driver-login-btn", "n_clicks"), State("driver-code-input", "value"),
              prevent_initial_call=True)
def login_driver(_n, code):
    code = (code or "").strip()
    if not code:
        return no_update, no_update, "الرجاء إدخال الرمز"
    match = next((c for c in DRIVER_CODES if hmac.compare_digest(c.encode(), code.encode())), None)
    if match is None:
        return no_update, no_update, "رمز غير صحيح"
    return {"code": match}, "/driver-map", ""


@app.callback(Output("driver", "data", allow_duplicate=True), Output("url", "pathname", allow_duplicate=True),
              Input("logout-btn", "n_clicks"), State("driver", "data"), prevent_initial_call=True)
def logout_driver(n, drv):
    if not n:
        return no_update, no_update
    with lock:
        bus = bus_of(drv)
        if bus:
            bus["seen"] = 0.0               # تختفي الحافلة من خريطة الركاب
    return None, "/driver-login"


# ================================ GPS الراكب (clientside) ================================
app.clientside_callback(
    """
    async function(n){
        const nu = window.dash_clientside.no_update;
        if(!n){ return [nu, nu]; }
        if(!navigator.geolocation){ return [nu, "المتصفح لا يدعم تحديد الموقع"]; }
        try{
            const p = await new Promise((res, rej) =>
                navigator.geolocation.getCurrentPosition(res, rej,
                    {enableHighAccuracy: true, timeout: 15000, maximumAge: 0}));
            return [{lat: p.coords.latitude, lon: p.coords.longitude},
                    "تم تحديد الموقع بنجاح ✓ (الدقة ≈ " + Math.round(p.coords.accuracy) + " م)"];
        }catch(e){
            return [nu, "فشل تحديد الموقع: فعّل خدمة الموقع واسمح للمتصفح بالوصول إليه ثم أعد المحاولة"];
        }
    }
    """,
    Output("passenger-pos", "data"), Output("location-status", "children"),
    Input("get-loc-btn", "n_clicks"), prevent_initial_call=True,
)


@app.callback(Output("passenger-map", "viewport"), Input("passenger-pos", "data"), prevent_initial_call=True)
def center_passenger_map(pos):
    if not pos or "lat" not in pos:
        return no_update
    return {"center": [pos["lat"], pos["lon"]], "zoom": 15, "transition": "flyTo"}


# ================================ طلب / إلغاء ================================
@app.callback(Output("submit-output", "children"),
              Input("submit-btn", "n_clicks"), State("pid", "data"), State("passenger-name", "value"),
              State("passenger-pos", "data"), prevent_initial_call=True)
def add_passenger(_n, pid, name, pos):
    name = (name or "").strip()
    if not name:
        return "الرجاء إدخال اسمك"
    if not pos or "lat" not in pos:
        return "حدد موقعك أولاً بزر (تحديد موقعي الحالي)"
    if not pid:
        return "تعذّر تحديد هويتك، أعد تحميل الصفحة"
    with lock:
        cur = passengers.get(pid)
        if cur and cur["status"] in ("pending", "accepted"):      # تحديث الموقع مع بقاء حالة الطلب
            cur.update(name=name, lat=pos["lat"], lon=pos["lon"], t=time.time())
            request_records.get(cur["rid"], {}).update(name=name, lat=pos["lat"], lon=pos["lon"])
            msg = "تم تحديث موقعك"
        else:                                                      # طلب جديد (يحتاج قرار الإدارة)
            rid = uuid.uuid4().hex[:8]
            passengers[pid] = {"name": name, "lat": pos["lat"], "lon": pos["lon"], "t": time.time(),
                               "status": "pending", "eta": "", "rid": rid}
            new_record(rid, name, pos["lat"], pos["lon"])
            msg = "تم إرسال الطلب للإدارة"
    return f"{msg} ✓ ({stamp()})"


@app.callback(Output("submit-output", "children", allow_duplicate=True),
              Input("cancel-btn", "n_clicks"), State("pid", "data"), prevent_initial_call=True)
def cancel_request(_n, pid):
    with lock:
        removed = passengers.pop(pid, None)
        if removed and removed["status"] in ("pending", "accepted"):
            update_record(removed["rid"], status="canceled")
    return f"تم إلغاء الطلب ✓ ({stamp()})" if removed else "لا يوجد طلب لإلغائه"


# ================================ خريطة الحافلات للراكب ================================
@app.callback(Output("buses-markers", "children"), Output("bus-info", "children"), Output("request-response", "children"),
              Input("buses-interval", "n_intervals"), Input("submit-output", "children"), Input("pid", "data"))
def update_buses(_n, _s, pid):
    now = time.time()
    with lock:
        prune()
        live = [dict(b) for b in buses.values() if b["lat"] is not None and now - b["seen"] <= BUS_TTL]
        me = passengers.get(pid)
        me = dict(me) if me else None
    markers = []
    for b in live:
        free = CAPACITY - b["riders"]
        color = "#2e7d32" if free >= 3 else ("#ef6c00" if free >= 1 else "#c62828")
        markers.append(circle(b["lat"], b["lon"], color, 13, f"🚌 {b['label']} - مقاعد شاغرة: {free} من {CAPACITY}"))
    if me:
        markers.append(circle(me["lat"], me["lon"], "#1565c0", 10, "موقعك (طلبك مسجّل)"))
    info = (f"حافلات تعمل الآن: {len(live)} | آخر تحديث {stamp()}" if live
            else f"لا توجد حافلات نشطة حالياً | آخر تحديث {stamp()}")
    response = ""
    if me:
        st = me.get("status")
        if st == "accepted":
            response = f"✅ تم قبول طلبك{(' — المدة المتوقعة: ' + str(me['eta'])) if me.get('eta') else ''}"
        elif st == "rejected":
            response = "❌ تم رفض طلبك حالياً (يمكنك إرسال طلب جديد)"
        else:
            response = "📨 طلبك بانتظار قرار الإدارة"
    return markers, info, response


# ================================ الإدارة: دخول وخروج ================================
@app.callback(Output("admin", "data"), Output("admin-login-output", "children"),
              Input("admin-login", "n_clicks"), State("admin-code", "value"), prevent_initial_call=True)
def admin_login(_n, code):
    code = (code or "").strip()
    if not code or not hmac.compare_digest(code.encode(), ADMIN_CODE.encode()):
        return no_update, "رمز الإدارة غير صحيح"
    if len(admin_tokens) > 50:
        admin_tokens.pop()
    token = secrets.token_urlsafe(24)
    admin_tokens.add(token)
    return {"token": token}, ""


@app.callback(Output("admin", "data", allow_duplicate=True),
              Input("admin-logout", "n_clicks"), State("admin", "data"), prevent_initial_call=True)
def admin_logout(n, adm):
    if not n:
        return no_update
    if adm:
        admin_tokens.discard(adm.get("token"))
    return None


# ================================ الإدارة: القبول والرفض (للإدارة فقط) ================================
@app.callback(Output("admin-msg", "children"),
              Input({"type": "admin-accept", "rid": ALL}, "n_clicks"),
              Input({"type": "admin-reject", "rid": ALL}, "n_clicks"),
              State({"type": "eta", "rid": ALL}, "value"), State({"type": "eta", "rid": ALL}, "id"),
              State("admin", "data"), prevent_initial_call=True)
def admin_decide(_yes, _no, etas, eta_ids, adm):
    if not valid_admin(adm):                     # أي جلسة غير إدارية لا تملك صلاحية القرار
        return no_update
    trig = ctx.triggered_id
    if not isinstance(trig, dict) or not ctx.triggered or not ctx.triggered[0].get("value"):
        return no_update                         # ظهور أزرار جديدة وليس ضغطة فعلية
    rid, accept = trig["rid"], trig["type"] == "admin-accept"
    eta = next((str(v or "").strip() for v, i in zip(etas or [], eta_ids or []) if i and i.get("rid") == rid), "")
    with lock:
        p = next((v for v in passengers.values() if v.get("rid") == rid), None)
        if not p or p["status"] != "pending":
            return f"الطلب لم يعد بانتظار القرار ({stamp()})"
        p["status"], p["eta"] = ("accepted", eta) if accept else ("rejected", "")
        update_record(rid, status=p["status"], eta=p["eta"])
        name = p["name"]
    return f"{'✅ تم قبول' if accept else '❌ تم رفض'} طلب {name} ({stamp()})"


# ================================ الإدارة: الخريطة والقوائم ================================
@app.callback(Output("admin-markers", "children"), Output("admin-summary", "children"),
              Output("admin-pending", "children"), Output("pending-sig", "data"),
              Output("admin-accepted", "children"), Output("admin-rejected", "children"),
              Output("admin-fleet", "children"),
              Input("admin-interval", "n_intervals"), Input("admin-msg", "children"),
              State("admin", "data"), State("pending-sig", "data"))
def admin_refresh(_n, _m, adm, sig):
    if not valid_admin(adm):
        return (no_update,) * 7
    now = time.time()
    with lock:
        prune()
        plist = [dict(p) for p in passengers.values()]
        blist = [dict(b) for b in buses.values()]
        recs = sorted((dict(r) for r in request_records.values()), key=lambda r: r["t"], reverse=True)

    colors = {"pending": "#f39c12", "accepted": "#1565c0", "rejected": "#95a5a6"}
    labels = {"pending": "بانتظار القرار", "accepted": "مقبول", "rejected": "مرفوض"}
    markers = [circle(p["lat"], p["lon"], colors.get(p["status"], "#f39c12"), 9,
                      f"👤 {p['name']} — {labels.get(p['status'], p['status'])}") for p in plist]
    fleet = []
    for b in blist:
        online = b["lat"] is not None and now - b["seen"] <= BUS_TTL
        if b["lat"] is not None:
            free = CAPACITY - b["riders"]
            color = ("#2e7d32" if free >= 3 else "#ef6c00" if free >= 1 else "#c62828") if online else "#34495e"
            markers.append(circle(b["lat"], b["lon"], color, 13,
                                  f"🚌 {b['label']} — الركاب {b['riders']}/{CAPACITY}" + ("" if online else " (غير متصلة)")))
        fleet.append(html.Div(f"{b['label']}: الركاب {b['riders']}/{CAPACITY} — {'🟢 متصلة' if online else '⚫ غير متصلة'}",
                              className="logline"))

    pending = sorted((p for p in plist if p["status"] == "pending"), key=lambda p: p["t"])
    new_sig = [p["rid"] for p in pending]
    if sig == new_sig:                           # لا نعيد رسم البطاقات كي لا يضيع ما كتبته الإدارة في حقل المدة
        pending_children, sig_out = no_update, no_update
    else:
        sig_out = new_sig
        pending_children = [html.Div([
            html.Div([html.B(p["name"]), html.Span(f" — {time.strftime('%H:%M:%S', time.localtime(p['t']))}")]),
            html.Div(f"{p['lat']:.5f}, {p['lon']:.5f}", className="small"),
            dcc.Input(id={"type": "eta", "rid": p["rid"]}, type="text", placeholder="الوقت المتوقع (اختياري)",
                      className="field"),
            html.Div([html.Button("✅ قبول", id={"type": "admin-accept", "rid": p["rid"]}, n_clicks=0, className="btn green"),
                      html.Button("❌ رفض", id={"type": "admin-reject", "rid": p["rid"]}, n_clicks=0, className="btn red")],
                     className="row"),
        ], className="mini") for p in pending] or [html.Div("لا توجد طلبات بانتظار القرار", className="logline")]

    def lines(status, icon):
        out = []
        for r in [r for r in recs if r["status"] == status][:60]:
            txt = f"{icon} {r['time']} — {r['name']}"
            if r.get("eta"):
                txt += f" — المدة: {r['eta']}"
            if r.get("bus"):
                txt += f" — ركب {r['bus']}"
            out.append(html.Div(txt, className="logline"))
        return out or [html.Div("لا توجد حركات", className="logline")]

    n_acc = sum(1 for p in plist if p["status"] == "accepted")
    n_on = sum(1 for b in blist if b["lat"] is not None and now - b["seen"] <= BUS_TTL)
    summary = (f"بانتظار القرار: {len(pending)} | مقبولون ينتظرون الحافلة: {n_acc} | "
               f"مرفوضون: {sum(1 for r in recs if r['status'] == 'rejected')} | حافلات متصلة: {n_on} | آخر تحديث {stamp()}")
    return markers, summary, pending_children, sig_out, lines("accepted", "✅"), lines("rejected", "❌"), fleet


# ================================ GPS السائق (clientside) ================================
app.clientside_callback(
    """
    async function(n){
        if(!navigator.geolocation){ return {error: "المتصفح لا يدعم تحديد الموقع", ts: Date.now()}; }
        try{
            const p = await new Promise((res, rej) =>
                navigator.geolocation.getCurrentPosition(res, rej,
                    {enableHighAccuracy: true, timeout: 15000, maximumAge: 0}));
            return {lat: p.coords.latitude, lon: p.coords.longitude, ts: Date.now()};
        }catch(e){
            return {error: "تعذّر تحديد موقعك: فعّل خدمة الموقع واسمح للمتصفح بالوصول إليه", ts: Date.now()};
        }
    }
    """,
    Output("driver-pos", "data"), Input("driver-tick", "n_intervals"),
)


# ================================ خريطة السائق ================================
@app.callback(Output("passengers-markers", "children"), Output("passenger-count", "children"),
              Output("bus-status", "children"), Output("gps-status", "children"), Output("driver-map", "viewport"),
              Input("driver-pos", "data"), Input("board-btn", "n_clicks"), Input("alight-btn", "n_clicks"),
              State("driver", "data"))
def driver_view(pos, _b, _a, drv):
    trig = ctx.triggered_id
    viewport = no_update
    with lock:
        bus = bus_of(drv)
        if bus is None:
            return [], "", "", "", no_update
        pos_ok = bool(pos) and "lat" in pos
        if trig == "driver-pos" and pos_ok:
            first_fix = bus["lat"] is None
            bus["lat"], bus["lon"], bus["seen"] = pos["lat"], pos["lon"], time.time()
            if first_fix:
                viewport = {"center": [pos["lat"], pos["lon"]], "zoom": 15, "transition": "flyTo"}
        elif trig == "board-btn" and bus["riders"] < CAPACITY:
            bus["riders"] += 1
            accepted = [(pid, p) for pid, p in passengers.items() if p.get("status") == "accepted"]
            if bus["lat"] is not None and accepted:      # أقرب طلب مقبول (من الإدارة) يُعتبر محمّلاً
                here = (bus["lat"], bus["lon"])
                pid, p = min(accepted, key=lambda kv: dist(here, (kv[1]["lat"], kv[1]["lon"])))
                if dist(here, (p["lat"], p["lon"])) <= LOAD_RADIUS:
                    update_record(p["rid"], bus=bus["label"])
                    del passengers[pid]
        elif trig == "alight-btn" and bus["riders"] > 0:
            bus["riders"] -= 1
        prune()
        plist = [dict(p) for p in passengers.values() if p.get("status") == "accepted"]   # السائق يرى المقبولين فقط
        riders, mine = bus["riders"], (bus["lat"], bus["lon"])

    markers = [circle(p["lat"], p["lon"], "#1565c0", 10,
                      f"الراكب: {p['name']}" + (f" — {p['eta']}" if p.get("eta") else "")) for p in plist]
    if mine[0] is not None:
        markers.append(circle(mine[0], mine[1], "#ef6c00", 13, "🚌 موقعك"))
    status = f"الركاب في الحافلة: {riders} من {CAPACITY} (شاغر {CAPACITY - riders})"
    count = f"👥 الركاب المقبولون المنتظرون: {len(plist)}"
    if pos_ok:
        gps = f"آخر تحديث لموقعك {stamp()}"
    elif pos and pos.get("error"):
        gps = pos["error"]
    else:
        gps = "جارٍ تحديد موقعك…"
    return markers, count, status, gps, viewport


# ================================ التشغيل ================================
if __name__ == "__main__":
    print(f"http://127.0.0.1:{os.environ.get('PORT', 8050)}   |   رموز السائقين: {', '.join(DRIVER_CODES)}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8050)), debug=False)
