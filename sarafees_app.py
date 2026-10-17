"""
Sarafees - سرافيس: تطبيق ركاب وسائقين (Dash + Leaflet)  ← نسخة معدّلة من f.py

التشغيل محلياً:
    pip install -r requirements.txt
    python sarafees_app.py            ثم افتح http://127.0.0.1:8050

النشر على Render (أمر التشغيل):
    gunicorn sarafees_app:server --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT
    (workers = 1 لأن الحالة الحية محفوظة في ذاكرة العملية)

متغيرات البيئة (اختيارية):
    DRIVER_CODES     رموز السائقين مفصولة بفاصلة، مثال: 4821,7730,1956   (لكل رمز حافلة)
    REFRESH_SECONDS  فترة تحديث الدبابيس بالثواني (الافتراضي 20)

ملاحظة: تحديد الموقع في المتصفح يعمل على https أو localhost فقط (Render يوفر https).
"""
import hmac
import math
import os
import threading
import time
import uuid

import dash
import dash_leaflet as dl
from dash import Input, Output, State, ctx, dcc, html, no_update

# ================================ الإعدادات ================================
CENTER = [31.9038, 35.2070]      # مركز الخريطة الابتدائي (كما في الكود الأصلي)
CAPACITY = 7                     # عدد ركاب الحافلة
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", 20))
PASSENGER_TTL = 15 * 60          # ينتهي طلب الراكب تلقائياً بعد 15 دقيقة
BUS_TTL = 120                    # حافلة لم تصل منها إشارة خلال دقيقتين تُخفى
LOAD_RADIUS = 0.006              # عند «تحميل راكب» يُحذف أقرب طلب ضمن ≈ 650 م
DRIVER_CODES = [c.strip() for c in os.environ.get("DRIVER_CODES", "1234,5678,9999").split(",") if c.strip()]
# ===========================================================================

lock = threading.Lock()
passengers = {}                  # معرّف المتصفح -> {name, lat, lon, t}
buses = {code: {"label": f"حافلة {i}", "lat": None, "lon": None, "riders": 0, "seen": 0.0}
         for i, code in enumerate(DRIVER_CODES, 1)}

app = dash.Dash(
    __name__, suppress_callback_exceptions=True, title="Sarafees",
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
  .field{width:100%;box-sizing:border-box;padding:12px;font-size:17px;border:1px solid #ccc;
         border-radius:10px;margin:6px 0 10px}
  .btn{display:block;width:100%;padding:14px;font-size:18px;border:none;border-radius:10px;
       color:#fff;margin-top:10px;cursor:pointer;font-family:inherit}
  .btn:active{opacity:.8}
  .blue{background:#3498db}.green{background:#2ecc71}.red{background:#e74c3c}
  .purple{background:#5322e6}.orange{background:#ef6c00}
  .status{text-align:center;font-weight:bold;margin-top:10px;min-height:20px}
  .err{color:#c0392b}
  .mapwrap{width:calc(100% - 24px);max-width:1000px;margin:12px auto 24px;border-radius:12px;overflow:hidden}
  .leaflet-container{direction:ltr}
  .row{display:flex;gap:10px}.row .btn{margin-top:0}
  .big{font-size:30px;font-weight:bold;text-align:center;margin:6px 0}
  @media(min-width:900px){.card{max-width:520px}}
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
    """يُستدعى والقفل محجوز: حذف الطلبات القديمة."""
    now = time.time()
    for pid in [k for k, p in passengers.items() if now - p["t"] > PASSENGER_TTL]:
        del passengers[pid]


def circle(lat, lon, color, radius, tip):
    return dl.CircleMarker(center=[lat, lon], radius=radius, color=color, fillColor=color, fillOpacity=0.8,
                           children=[dl.Tooltip(tip), dl.Popup(tip)])


def bus_of(drv):
    if not drv:
        return None
    return buses.get(drv.get("code"))


def navbar(right=None):
    return html.Div([
        html.Div("🚌 Sarafees", className="brand"),
        right or dcc.Link("👨‍✈️ دخول السائق", href="/driver-login", className="pill"),
    ], className="navbar")


# ================================ واجهة الراكب ================================
def passenger_layout():
    return html.Div([
        navbar(),
        html.H2("واجهة الراكب - طلب سرافيس", className="title"),
        html.Div([
            html.Label("اسم الراكب:"),
            dcc.Input(id="passenger-name", type="text", placeholder="أدخل اسمك هنا", className="field",
                      persistence=True, persistence_type="local"),
            html.Button("📍 تحديد موقعي الحالي (GPS)", id="get-loc-btn", n_clicks=0, className="btn blue"),
            html.Div("لم يتم تحديد الموقع بعد", id="location-status", className="status"),
            html.Button("✅ إرسال الطلب للسائقين", id="submit-btn", n_clicks=0, className="btn green"),
            html.Button("❌ إلغاء الطلب", id="cancel-btn", n_clicks=0, className="btn red"),
            html.Div(id="submit-output", className="status"),
        ], className="card"),
        html.H4("🚌 الحافلات المتاحة حالياً", className="title"),
        html.Div(id="bus-info", className="status"),
        html.Div(dl.Map([dl.TileLayer(), dl.LayerGroup(id="buses-markers")], id="passenger-map",
                        center=CENTER, zoom=14, style={"height": "55vh", "minHeight": "320px"}),
                 className="mapwrap"),
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
    html.Div(id="page-content"),
])


@app.callback(Output("page-content", "children"), Input("url", "pathname"), Input("driver", "data"))
def display_page(path, drv):
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
        existed = pid in passengers
        passengers[pid] = {"name": name, "lat": pos["lat"], "lon": pos["lon"], "t": time.time()}
    return f"{'تم تحديث موقعك' if existed else 'تم إرسال الطلب'} ✓ ({stamp()})"


@app.callback(Output("submit-output", "children", allow_duplicate=True),
              Input("cancel-btn", "n_clicks"), State("pid", "data"), prevent_initial_call=True)
def cancel_request(_n, pid):
    with lock:
        found = passengers.pop(pid, None) is not None
    return f"تم إلغاء الطلب ✓ ({stamp()})" if found else "لا يوجد طلب لإلغائه"


# ================================ خريطة الحافلات للراكب ================================
@app.callback(Output("buses-markers", "children"), Output("bus-info", "children"),
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
    return markers, info


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
            if bus["lat"] is not None and passengers:      # أقرب طلب قريب يُعتبر محمّلاً
                here = (bus["lat"], bus["lon"])
                pid, p = min(passengers.items(), key=lambda kv: dist(here, (kv[1]["lat"], kv[1]["lon"])))
                if dist(here, (p["lat"], p["lon"])) <= LOAD_RADIUS:
                    del passengers[pid]
        elif trig == "alight-btn" and bus["riders"] > 0:
            bus["riders"] -= 1
        prune()
        plist = [dict(p) for p in passengers.values()]
        riders, mine = bus["riders"], (bus["lat"], bus["lon"])

    markers = [circle(p["lat"], p["lon"], "#1565c0", 10, f"الراكب: {p['name']}") for p in plist]
    if mine[0] is not None:
        markers.append(circle(mine[0], mine[1], "#ef6c00", 13, "🚌 موقعك"))
    status = f"الركاب في الحافلة: {riders} من {CAPACITY} (شاغر {CAPACITY - riders})"
    count = f"👥 الركاب المنتظرون: {len(plist)}"
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
