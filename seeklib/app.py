import csv
import io
import os
import sqlite3
import subprocess
import time
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

import bcrypt
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

DB_PATH = os.getenv("SEEKLIB_DB", "data/seeklib.sqlite3")
TZ = ZoneInfo("America/Mexico_City")

app = Flask(__name__)
app.secret_key = os.getenv("SEEKLIB_SECRET_KEY", "seeklib-local-secret-key-2026")


def get_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS USUARIOS (
                id_usuario INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                correo TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                rol TEXT NOT NULL DEFAULT 'ADMIN',
                activo INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS GRUPOS (
                id_grupo INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre_grupo TEXT NOT NULL UNIQUE,
                carrera TEXT,
                activo INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS SALONES (
                id_salon INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre_salon TEXT NOT NULL UNIQUE,
                ubicacion TEXT,
                activo INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS MATERIAS (
                id_materia INTEGER PRIMARY KEY AUTOINCREMENT,
                clave TEXT NOT NULL UNIQUE,
                nombre_materia TEXT NOT NULL,
                id_grupo INTEGER REFERENCES GRUPOS(id_grupo),
                activo INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS ESTUDIANTES (
                matricula TEXT PRIMARY KEY,
                nombre TEXT NOT NULL,
                apellido TEXT NOT NULL,
                id_grupo INTEGER REFERENCES GRUPOS(id_grupo),
                uid_rfid TEXT UNIQUE,
                id_huella INTEGER UNIQUE,
                activo INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS INSCRIPCIONES (
                id_inscripcion INTEGER PRIMARY KEY AUTOINCREMENT,
                matricula TEXT NOT NULL REFERENCES ESTUDIANTES(matricula) ON DELETE CASCADE,
                id_materia INTEGER NOT NULL REFERENCES MATERIAS(id_materia) ON DELETE CASCADE,
                activo INTEGER NOT NULL DEFAULT 1,
                UNIQUE(matricula, id_materia)
            );

            CREATE TABLE IF NOT EXISTS HORARIOS_CLASE (
                id_horario INTEGER PRIMARY KEY AUTOINCREMENT,
                id_materia INTEGER NOT NULL REFERENCES MATERIAS(id_materia) ON DELETE CASCADE,
                id_salon INTEGER NOT NULL REFERENCES SALONES(id_salon) ON DELETE CASCADE,
                dia_semana INTEGER NOT NULL CHECK(dia_semana BETWEEN 0 AND 6),
                hora_inicio TEXT NOT NULL,
                hora_fin TEXT NOT NULL,
                tolerancia_minutos INTEGER NOT NULL DEFAULT 15,
                activo INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS REGISTRO_ASISTENCIA (
                id_registro INTEGER PRIMARY KEY AUTOINCREMENT,
                matricula TEXT NOT NULL REFERENCES ESTUDIANTES(matricula),
                id_horario INTEGER NOT NULL REFERENCES HORARIOS_CLASE(id_horario),
                id_salon INTEGER NOT NULL REFERENCES SALONES(id_salon),
                fecha_hora TEXT NOT NULL,
                metodo_registro TEXT NOT NULL CHECK(metodo_registro IN ('RFID', 'HUELLA', 'MANUAL')),
                estatus TEXT NOT NULL CHECK(estatus IN ('PRESENTE', 'RETARDO', 'FALTA', 'JUSTIFICADO'))
            );
            """
        )

        # Semilla inicial: usuario administrador y salón por defecto
        admin = conn.execute("SELECT 1 FROM USUARIOS WHERE correo = ?", ("admin@seeklib.local",)).fetchone()
        if not admin:
            pw_hash = bcrypt.hashpw(b"Admin123!", bcrypt.gensalt()).decode("utf-8")
            conn.execute(
                "INSERT INTO USUARIOS (nombre, correo, password_hash, rol) VALUES (?, ?, ?, ?)",
                ("Administrador SeekLib", "admin@seeklib.local", pw_hash, "ADMIN"),
            )

        salon = conn.execute("SELECT 1 FROM SALONES WHERE id_salon = 1").fetchone()
        if not salon:
            conn.execute(
                "INSERT INTO SALONES (id_salon, nombre_salon, ubicacion) VALUES (1, 'Laboratorio 1', 'Edificio Principal')"
            )

        conn.commit()
    finally:
        conn.close()


init_db()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error="No autenticado"), 401
            return redirect(url_for("login_page"))
        return fn(*args, **kwargs)

    return wrapper


# --- LECTOR RC522 PARA ENROLAMIENTO REMOTO ---
class RC522Reader:
    COMMAND_REG = 0x01
    COM_IRQ_REG = 0x04
    ERROR_REG = 0x06
    FIFO_DATA_REG = 0x09
    FIFO_LEVEL_REG = 0x0A
    BIT_FRAMING_REG = 0x0D
    MODE_REG = 0x11
    TX_CONTROL_REG = 0x14
    TX_ASK_REG = 0x15
    RF_CFG_REG = 0x26
    T_MODE_REG = 0x2A
    T_PRESCALER_REG = 0x2B
    T_RELOAD_REG_H = 0x2C
    T_RELOAD_REG_L = 0x2D
    VERSION_REG = 0x37

    PCD_IDLE = 0x00
    PCD_TRANSCEIVE = 0x0C
    PCD_RESETPHASE = 0x0F
    PICC_REQIDL = 0x26
    PICC_ANTICOLL = 0x93

    def __init__(self, bus=0, device=0, rst_pin=25):
        import spidev
        from gpiozero import DigitalOutputDevice

        self.rst = None
        if rst_pin is not None:
            try:
                self.rst = DigitalOutputDevice(rst_pin, initial_value=True)
                self.rst.off()
                time.sleep(0.02)
                self.rst.on()
                time.sleep(0.05)
            except Exception as e:
                print(f"[RFID] Aviso: GPIO {rst_pin} ocupado ({e}), usando reset por software SPI.", flush=True)
                self.rst = None

        self.spi = spidev.SpiDev()
        try:
            self.spi.open(bus, device)
            self.spi.max_speed_hz = 1000000
            self.spi.mode = 0

            self._init_chip()
            self.version = self._read_reg(self.VERSION_REG)
            if self.version in (0x00, 0xFF):
                raise RuntimeError(
                    f"Chip RC522 no detectado en SPI{bus}.{device} (VersionReg=0x{self.version:02X}). Revisa cableado."
                )
        except Exception:
            self.close()
            raise

    def _write_reg(self, reg, val):
        self.spi.xfer2([(reg << 1) & 0x7E, val])

    def _read_reg(self, reg):
        return self.spi.xfer2([((reg << 1) & 0x7E) | 0x80, 0x00])[1]

    def _set_bit_mask(self, reg, mask):
        self._write_reg(reg, self._read_reg(reg) | mask)

    def _clear_bit_mask(self, reg, mask):
        self._write_reg(reg, self._read_reg(reg) & (~mask))

    def _init_chip(self):
        self._write_reg(self.COMMAND_REG, self.PCD_RESETPHASE)
        time.sleep(0.05)
        self._write_reg(self.T_MODE_REG, 0x8D)
        self._write_reg(self.T_PRESCALER_REG, 0x3E)
        self._write_reg(self.T_RELOAD_REG_L, 30)
        self._write_reg(self.T_RELOAD_REG_H, 0)
        self._write_reg(self.TX_ASK_REG, 0x40)
        self._write_reg(self.MODE_REG, 0x3D)
        self._write_reg(self.RF_CFG_REG, 0x70)
        if (self._read_reg(self.TX_CONTROL_REG) & 0x03) != 0x03:
            self._set_bit_mask(self.TX_CONTROL_REG, 0x03)

    def _to_card(self, send_data):
        self._write_reg(self.COMMAND_REG, self.PCD_IDLE)
        self._write_reg(self.COM_IRQ_REG, 0x7F)
        self._write_reg(self.FIFO_LEVEL_REG, 0x80)
        for byte in send_data:
            self._write_reg(self.FIFO_DATA_REG, byte)
        self._write_reg(self.COMMAND_REG, self.PCD_TRANSCEIVE)
        self._set_bit_mask(self.BIT_FRAMING_REG, 0x80)
        i = 200
        while True:
            irq = self._read_reg(self.COM_IRQ_REG)
            i -= 1
            if (irq & 0x30) or (irq & 0x01) or i == 0:
                break
        self._clear_bit_mask(self.BIT_FRAMING_REG, 0x80)
        if i == 0 or (irq & 0x01):
            return False, []
        if self._read_reg(self.ERROR_REG) & 0x1B:
            return False, []
        n = self._read_reg(self.FIFO_LEVEL_REG)
        return True, [self._read_reg(self.FIFO_DATA_REG) for _ in range(n)]

    def read_uid(self):
        self._write_reg(self.BIT_FRAMING_REG, 0x07)
        ok, back = self._to_card([self.PICC_REQIDL])
        if not ok or len(back) != 2:
            return None
        self._write_reg(self.BIT_FRAMING_REG, 0x00)
        ok, uid = self._to_card([self.PICC_ANTICOLL, 0x20])
        if not ok or len(uid) != 5:
            return None
        if (uid[0] ^ uid[1] ^ uid[2] ^ uid[3]) != uid[4]:
            return None
        return "".join(f"{b:02X}" for b in uid[:4])

    def close(self):
        try:
            self.spi.close()
        except Exception:
            pass
        if self.rst is not None:
            try:
                self.rst.close()
            except Exception:
                pass


def _control_hardware_service(action: str):
    subprocess.run(["sudo", "systemctl", action, "seeklib-hardware.service"], check=False)


# --- RUTAS DE AUTENTICACIÓN Y VISTAS ---
@app.get("/login")
def login_page():
    return render_template("login.html")


@app.post("/login")
def login_post():
    data = request.get_json(silent=True) or {}
    correo = (data.get("correo") or "").strip().lower()
    password = (data.get("password") or "").encode("utf-8")

    conn = get_db()
    try:
        user = conn.execute(
            "SELECT id_usuario, nombre, correo, password_hash, rol FROM USUARIOS WHERE lower(correo) = ? AND activo = 1",
            (correo,),
        ).fetchone()
    finally:
        conn.close()

    if not user or not bcrypt.checkpw(password, user["password_hash"].encode("utf-8")):
        return jsonify(ok=False, error="Credenciales inválidas"), 401

    session["user_id"] = user["id_usuario"]
    session["user_name"] = user["nombre"]
    session["user_role"] = user["rol"]
    return jsonify(ok=True)


@app.post("/logout")
def logout_post():
    session.clear()
    return jsonify(ok=True)


@app.get("/")
@login_required
def dashboard():
    return render_template("dashboard.html", user_name=session.get("user_name", "Admin"))


# --- API DE CONSULTA Y MÉTRICAS ---
@app.get("/api/dashboard")
@login_required
def api_dashboard():
    hoy = datetime.now(TZ).date().isoformat()
    conn = get_db()
    try:
        total_estudiantes = conn.execute("SELECT COUNT(*) FROM ESTUDIANTES WHERE activo = 1").fetchone()[0]
        presentes_hoy = conn.execute(
            "SELECT COUNT(*) FROM REGISTRO_ASISTENCIA WHERE substr(fecha_hora, 1, 10) = ? AND estatus = 'PRESENTE'",
            (hoy,),
        ).fetchone()[0]
        retardos_hoy = conn.execute(
            "SELECT COUNT(*) FROM REGISTRO_ASISTENCIA WHERE substr(fecha_hora, 1, 10) = ? AND estatus = 'RETARDO'",
            (hoy,),
        ).fetchone()[0]

        registros = conn.execute(
            """SELECT r.id_registro, r.matricula, e.nombre || ' ' || e.apellido AS estudiante,
                      m.nombre_materia AS materia, s.nombre_salon AS salon,
                      r.fecha_hora, r.metodo_registro, r.estatus
               FROM REGISTRO_ASISTENCIA r
               JOIN ESTUDIANTES e ON e.matricula = r.matricula
               JOIN HORARIOS_CLASE h ON h.id_horario = r.id_horario
               JOIN MATERIAS m ON m.id_materia = h.id_materia
               JOIN SALONES s ON s.id_salon = r.id_salon
               ORDER BY r.fecha_hora DESC
               LIMIT 50"""
        ).fetchall()

        estudiantes = conn.execute(
            """SELECT e.matricula, e.nombre, e.apellido, e.uid_rfid, e.id_huella,
                      g.nombre_grupo
               FROM ESTUDIANTES e
               LEFT JOIN GRUPOS g ON g.id_grupo = e.id_grupo
               WHERE e.activo = 1
               ORDER BY e.matricula"""
        ).fetchall()

        grupos = conn.execute("SELECT id_grupo, nombre_grupo, carrera FROM GRUPOS WHERE activo = 1").fetchall()
        materias = conn.execute("SELECT id_materia, clave, nombre_materia, id_grupo FROM MATERIAS WHERE activo = 1").fetchall()
        salones = conn.execute("SELECT id_salon, nombre_salon, ubicacion FROM SALONES WHERE activo = 1").fetchall()
        horarios = conn.execute(
            """SELECT h.id_horario, m.nombre_materia, s.nombre_salon, h.dia_semana,
                      h.hora_inicio, h.hora_fin, h.tolerancia_minutos
               FROM HORARIOS_CLASE h
               JOIN MATERIAS m ON m.id_materia = h.id_materia
               JOIN SALONES s ON s.id_salon = h.id_salon
               WHERE h.activo = 1
               ORDER BY h.dia_semana, h.hora_inicio"""
        ).fetchall()

        return jsonify(
            ok=True,
            resumen={
                "total_estudiantes": total_estudiantes,
                "presentes_hoy": presentes_hoy,
                "retardos_hoy": retardos_hoy,
            },
            registros=[dict(r) for r in registros],
            estudiantes=[dict(e) for e in estudiantes],
            grupos=[dict(g) for g in grupos],
            materias=[dict(m) for m in materias],
            salones=[dict(s) for s in salones],
            horarios=[dict(h) for h in horarios],
        )
    finally:
        conn.close()


# --- CRUD ACADÉMICO ---
@app.post("/api/estudiantes")
@login_required
def crear_estudiante():
    d = request.get_json(silent=True) or {}
    matricula = (d.get("matricula") or "").strip().upper()
    nombre = (d.get("nombre") or "").strip()
    apellido = (d.get("apellido") or "").strip()
    id_grupo = d.get("id_grupo") or None

    if not matricula or not nombre or not apellido:
        return jsonify(ok=False, error="Matrícula, nombre y apellido son obligatorios"), 400

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO ESTUDIANTES (matricula, nombre, apellido, id_grupo, activo)
               VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(matricula) DO UPDATE SET
                 nombre = excluded.nombre,
                 apellido = excluded.apellido,
                 id_grupo = excluded.id_grupo,
                 activo = 1""",
            (matricula, nombre, apellido, id_grupo),
        )
        if id_grupo:
            mats = conn.execute("SELECT id_materia FROM MATERIAS WHERE id_grupo = ? AND activo = 1", (id_grupo,)).fetchall()
            for m in mats:
                conn.execute(
                    "INSERT OR IGNORE INTO INSCRIPCIONES (matricula, id_materia, activo) VALUES (?, ?, 1)",
                    (matricula, m["id_materia"]),
                )
        conn.commit()
        return jsonify(ok=True)
    except Exception as e:
        conn.rollback()
        return jsonify(ok=False, error=str(e)), 400
    finally:
        conn.close()


@app.post("/api/grupos")
@login_required
def crear_grupo():
    d = request.get_json(silent=True) or {}
    nombre = (d.get("nombre_grupo") or "").strip()
    carrera = (d.get("carrera") or "").strip()
    if not nombre:
        return jsonify(ok=False, error="Nombre de grupo obligatorio"), 400
    conn = get_db()
    try:
        conn.execute("INSERT INTO GRUPOS (nombre_grupo, carrera) VALUES (?, ?)", (nombre, carrera))
        conn.commit()
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400
    finally:
        conn.close()


@app.post("/api/materias")
@login_required
def crear_materia():
    d = request.get_json(silent=True) or {}
    clave = (d.get("clave") or "").strip().upper()
    nombre = (d.get("nombre_materia") or "").strip()
    id_grupo = d.get("id_grupo") or None
    if not clave or not nombre:
        return jsonify(ok=False, error="Clave y nombre de materia obligatorios"), 400
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO MATERIAS (clave, nombre_materia, id_grupo) VALUES (?, ?, ?)",
            (clave, nombre, id_grupo),
        )
        id_mat = cur.lastrowid
        if id_grupo:
            ests = conn.execute("SELECT matricula FROM ESTUDIANTES WHERE id_grupo = ? AND activo = 1", (id_grupo,)).fetchall()
            for e in ests:
                conn.execute(
                    "INSERT OR IGNORE INTO INSCRIPCIONES (matricula, id_materia, activo) VALUES (?, ?, 1)",
                    (e["matricula"], id_mat),
                )
        conn.commit()
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400
    finally:
        conn.close()


@app.post("/api/horarios")
@login_required
def crear_horario():
    d = request.get_json(silent=True) or {}
    try:
        id_materia = int(d["id_materia"])
        id_salon = int(d.get("id_salon", 1))
        dia_semana = int(d["dia_semana"])
        hora_inicio = d["hora_inicio"]
        hora_fin = d["hora_fin"]
        tolerancia = int(d.get("tolerancia_minutos", 15))
        if len(hora_inicio) == 5:
            hora_inicio += ":00"
        if len(hora_fin) == 5:
            hora_fin += ":00"
    except Exception:
        return jsonify(ok=False, error="Datos de horario incompletos"), 400

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO HORARIOS_CLASE
               (id_materia, id_salon, dia_semana, hora_inicio, hora_fin, tolerancia_minutos)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (id_materia, id_salon, dia_semana, hora_inicio, hora_fin, tolerancia),
        )
        conn.commit()
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400
    finally:
        conn.close()


# --- ENROLAMIENTO DE CREDENCIALES DESDE WEB ---
@app.post("/api/enrolar/rfid")
@login_required
def enrolar_rfid_web():
    d = request.get_json(silent=True) or {}
    matricula = (d.get("matricula") or "").strip().upper()
    if not matricula:
        return jsonify(ok=False, error="Selecciona una matrícula"), 400

    _control_hardware_service("stop")
    time.sleep(0.4)
    reader = None
    uid_leido = None
    try:
        reader = RC522Reader(bus=0, device=0, rst_pin=25)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            uid = reader.read_uid()
            if uid:
                uid_leido = uid
                break
            time.sleep(0.1)
    except Exception as e:
        return jsonify(ok=False, error=f"Error de hardware RC522: {e}"), 500
    finally:
        if reader is not None:
            reader.close()
        _control_hardware_service("start")

    if not uid_leido:
        return jsonify(ok=False, error="Tiempo agotado (15s) sin detectar tarjeta RFID"), 408

    conn = get_db()
    try:
        conn.execute("UPDATE ESTUDIANTES SET uid_rfid = ? WHERE matricula = ?", (uid_leido, matricula))
        conn.commit()
        return jsonify(ok=True, uid_rfid=uid_leido)
    except sqlite3.IntegrityError:
        return jsonify(ok=False, error=f"La tarjeta {uid_leido} ya está asignada a otro alumno"), 409
    finally:
        conn.close()


@app.post("/api/enrolar/huella")
@login_required
def enrolar_huella_web():
    import serial
    import adafruit_fingerprint

    d = request.get_json(silent=True) or {}
    matricula = (d.get("matricula") or "").strip().upper()
    if not matricula:
        return jsonify(ok=False, error="Selecciona una matrícula"), 400

    conn = get_db()
    try:
        usados = {
            r["id_huella"]
            for r in conn.execute("SELECT id_huella FROM ESTUDIANTES WHERE id_huella IS NOT NULL").fetchall()
        }
        slot = next((i for i in range(1, 128) if i not in usados), None)
    finally:
        conn.close()

    if slot is None:
        return jsonify(ok=False, error="Memoria del sensor AS608 llena (127 huellas)"), 507

    _control_hardware_service("stop")
    time.sleep(0.4)
    uart = None
    try:
        uart = serial.Serial("/dev/ttyAMA0", baudrate=57600, timeout=1)
        finger = adafruit_fingerprint.Adafruit_Fingerprint(uart)

        for paso in (1, 2):
            deadline = time.monotonic() + 12.0
            while finger.get_image() != adafruit_fingerprint.OK:
                if time.monotonic() > deadline:
                    return jsonify(ok=False, error=f"Tiempo agotado en captura {paso}/2"), 408
                time.sleep(0.1)
            if finger.image_2_tz(paso) != adafruit_fingerprint.OK:
                return jsonify(ok=False, error=f"No se pudo procesar plantilla {paso}/2"), 500
            if paso == 1:
                time.sleep(1.0)

        if finger.create_model() != adafruit_fingerprint.OK:
            return jsonify(ok=False, error="Las dos lecturas de huella no coincidieron"), 400
        if finger.store_model(slot) != adafruit_fingerprint.OK:
            return jsonify(ok=False, error="Error guardando huella en sensor AS608"), 500
    except Exception as e:
        return jsonify(ok=False, error=f"Error serial AS608: {e}"), 500
    finally:
        if uart is not None:
            uart.close()
        _control_hardware_service("start")

    conn = get_db()
    try:
        conn.execute("UPDATE ESTUDIANTES SET id_huella = ? WHERE matricula = ?", (slot, matricula))
        conn.commit()
        return jsonify(ok=True, id_huella=slot)
    finally:
        conn.close()


# --- EXPORTACIÓN CSV ---
@app.get("/api/exportar.csv")
@login_required
def exportar_csv():
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT r.id_registro, r.matricula, e.nombre, e.apellido,
                      g.nombre_grupo, m.clave AS clave_materia, m.nombre_materia,
                      s.nombre_salon, r.fecha_hora, r.metodo_registro, r.estatus
               FROM REGISTRO_ASISTENCIA r
               JOIN ESTUDIANTES e ON e.matricula = r.matricula
               LEFT JOIN GRUPOS g ON g.id_grupo = e.id_grupo
               JOIN HORARIOS_CLASE h ON h.id_horario = r.id_horario
               JOIN MATERIAS m ON m.id_materia = h.id_materia
               JOIN SALONES s ON s.id_salon = r.id_salon
               ORDER BY r.fecha_hora DESC"""
        ).fetchall()
    finally:
        conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["ID", "Matricula", "Nombre", "Apellido", "Grupo", "Clave", "Materia", "Salon", "FechaHora", "Metodo", "Estatus"]
    )
    for r in rows:
        writer.writerow(list(r))

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=asistencias_seeklib.csv"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
