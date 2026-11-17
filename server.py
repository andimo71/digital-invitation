#!/usr/bin/env python3
"""Server undangan pernikahan + SQLite + panel admin (hanya library standar Python 3).
Jalankan:  ADMIN_PASSWORD='kata-sandi-anda' python3 server.py
Undangan: http://127.0.0.1:8000   Admin: http://127.0.0.1:8000/admin
Env opsional: HOST, PORT, DB_PATH. Tanpa ADMIN_PASSWORD, kata sandi acak dicetak di terminal."""
import csv, hmac, io, json, os, secrets, sqlite3, time
from contextlib import closing
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "wishes.db"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
STATUS = ("Hadir", "Berhalangan hadir")
DEFAULTS = {
    "pria": "Raka", "wanita": "Anindya",
    "pria_lengkap": "Raka Pratama", "wanita_lengkap": "Anindya Putri",
    "ortu_pria": "Putra dari Bapak Hendra Pratama\n& Ibu Sari Wulandari",
    "ortu_wanita": "Putri dari Bapak Bambang Setiawan\n& Ibu Ratna Dewi",
    "tanggal": "2027-03-20T08:00", "rsvp_batas": "2027-03-10",
    "akad_jam": "08.00 – 10.00 WIB", "resepsi_jam": "11.00 – 14.00 WIB",
    "venue": "Gedung Serbaguna Melati",
    "alamat": "Jl. Kenanga Raya No. 12, Menteng,\nJakarta Pusat 10310",
    "maps": "https://maps.google.com/?q=Jakarta",
    "bank": "Bank BCA · a.n. Raka Pratama", "rekening": "1234 5678 90",
}
sessions, last_post, fails = set(), {}, {}

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init():
    with closing(db()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS wishes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nama TEXT NOT NULL, status TEXT NOT NULL,
            jumlah INTEGER NOT NULL, ucapan TEXT NOT NULL DEFAULT '',
            tampil INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        try:
            c.execute("ALTER TABLE wishes ADD COLUMN tampil INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        c.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.commit()

def get_settings(c):
    s = dict(DEFAULTS)
    s.update({r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings") if r["key"] in DEFAULTS})
    return s

def safe_cell(v):
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@") else v

class H(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json; charset=utf-8", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def authed(self):
        try:
            return SimpleCookie(self.headers.get("Cookie", ""))["adm"].value in sessions
        except Exception:
            return False

    def read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 16384:
            raise ValueError
        d = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(d, dict):
            raise ValueError
        return d

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self.send(200, open(os.path.join(BASE, "index.html"), "rb").read(), "text/html; charset=utf-8")
        if p == "/admin":
            return self.send(200, open(os.path.join(BASE, "admin.html"), "rb").read(), "text/html; charset=utf-8",
                             [("X-Frame-Options", "DENY"), ("Cache-Control", "no-store")])
        if p == "/api/wishes":
            with closing(db()) as c:
                rows = c.execute("SELECT nama,status,jumlah,ucapan,created_at FROM wishes WHERE tampil=1 ORDER BY id DESC LIMIT 50").fetchall()
            return self.send(200, [dict(r) for r in rows])
        if p == "/api/settings":
            with closing(db()) as c:
                return self.send(200, get_settings(c))
        if p.startswith("/admin/"):
            if not self.authed():
                return self.send(401, {"error": "Belum masuk"})
            if p == "/admin/api/data":
                with closing(db()) as c:
                    rows = c.execute("SELECT id,nama,status,jumlah,ucapan,tampil,created_at FROM wishes ORDER BY id DESC").fetchall()
                    return self.send(200, {"wishes": [dict(r) for r in rows], "settings": get_settings(c), "defaults": DEFAULTS},
                                     headers=[("Cache-Control", "no-store")])
            if p == "/admin/export.csv":
                with closing(db()) as c:
                    rows = c.execute("SELECT id,created_at,nama,status,jumlah,ucapan,tampil FROM wishes ORDER BY id").fetchall()
                out = io.StringIO()
                w = csv.writer(out)
                w.writerow(["id", "waktu_utc", "nama", "status", "jumlah", "ucapan", "tampil"])
                for r in rows:
                    w.writerow([r["id"], r["created_at"], safe_cell(r["nama"]), r["status"], r["jumlah"], safe_cell(r["ucapan"]), r["tampil"]])
                return self.send(200, out.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
                                 [("Content-Disposition", 'attachment; filename="daftar-tamu.csv"')])
        self.send(404, {"error": "Tidak ditemukan"})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/wishes":
            return self.post_wish()
        if p == "/admin/api/login":
            return self.login()
        if not p.startswith("/admin/api/"):
            return self.send(404, {"error": "Tidak ditemukan"})
        if not self.authed():
            return self.send(401, {"error": "Belum masuk"})
        if self.headers.get("X-Admin") != "1":
            return self.send(403, {"error": "Ditolak"})
        try:
            d = self.read_json()
        except Exception:
            return self.send(400, {"error": "Data tidak valid"})
        if p == "/admin/api/logout":
            sessions.discard(SimpleCookie(self.headers.get("Cookie", ""))["adm"].value)
            return self.send(200, {"ok": True}, headers=[("Set-Cookie", "adm=; Path=/; Max-Age=0")])
        if p == "/admin/api/wish":
            try:
                wid, act = int(d.get("id")), d.get("action")
            except Exception:
                return self.send(400, {"error": "Data tidak valid"})
            with closing(db()) as c:
                if act == "toggle":
                    c.execute("UPDATE wishes SET tampil=1-tampil WHERE id=?", (wid,))
                elif act == "delete":
                    c.execute("DELETE FROM wishes WHERE id=?", (wid,))
                else:
                    return self.send(400, {"error": "Aksi tidak dikenal"})
                c.commit()
            return self.send(200, {"ok": True})
        if p == "/admin/api/settings":
            with closing(db()) as c:
                for k, v in d.items():
                    if k not in DEFAULTS:
                        continue
                    v = str(v).strip()[:300]
                    if k == "maps" and v and not v.startswith("https://"):
                        return self.send(400, {"error": "Link peta harus diawali https://"})
                    if v:
                        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
                    else:
                        c.execute("DELETE FROM settings WHERE key=?", (k,))
                c.commit()
            return self.send(200, {"ok": True})
        self.send(404, {"error": "Tidak ditemukan"})

    def login(self):
        ip, now = self.client_address[0], time.time()
        fl = [t for t in fails.get(ip, []) if now - t < 60]
        if len(fl) >= 5:
            return self.send(429, {"error": "Terlalu banyak percobaan. Coba lagi sebentar lagi."})
        try:
            pw = str(self.read_json().get("password", ""))
        except Exception:
            return self.send(400, {"error": "Data tidak valid"})
        if hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
            tok = secrets.token_urlsafe(32)
            sessions.add(tok)
            return self.send(200, {"ok": True}, headers=[("Set-Cookie", f"adm={tok}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800")])
        fails[ip] = fl + [now]
        self.send(401, {"error": "Kata sandi salah"})

    def post_wish(self):
        ip, now = self.client_address[0], time.time()
        if now - last_post.get(ip, 0) < 10:
            return self.send(429, {"error": "Mohon tunggu sebentar sebelum mengirim lagi."})
        try:
            d = self.read_json()
            nama = str(d.get("nama", "")).strip()[:60]
            ucapan = str(d.get("ucapan", "")).strip()[:300]
            status, jumlah = d.get("status"), int(d.get("jumlah", 1))
            if not nama or status not in STATUS or not 1 <= jumlah <= 10:
                raise ValueError
        except Exception:
            return self.send(400, {"error": "Data tidak valid. Pastikan nama terisi."})
        last_post[ip] = now
        with closing(db()) as c:
            c.execute("INSERT INTO wishes(nama,status,jumlah,ucapan) VALUES(?,?,?,?)", (nama, status, jumlah, ucapan))
            c.commit()
        self.send(201, {"ok": True})

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    init()
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", 8000))
    print(f"Undangan: http://{host}:{port}   Admin: http://{host}:{port}/admin")
    if not os.environ.get("ADMIN_PASSWORD"):
        print(f"Kata sandi admin (acak, hanya untuk sesi ini): {ADMIN_PASSWORD}")
    ThreadingHTTPServer((host, port), H).serve_forever()
