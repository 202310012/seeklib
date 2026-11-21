import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DB_PATH = os.getenv("SEEKLIB_DB", "data/seeklib.sqlite3")
SALON_ID = int(os.getenv("SEEKLIB_SALON_ID", "1"))
TZ = ZoneInfo("America/Mexico_City")


class AttendanceService:
    def __init__(self, db_path: str = DB_PATH, salon_id: int = SALON_ID):
        self.db_path = db_path
        self.salon_id = salon_id

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def register(self, identificador, metodo: str, id_salon: int = None):
        salon = id_salon if id_salon is not None else self.salon_id
        now = datetime.now(TZ)
        current_time = now.strftime("%H:%M:%S")
        today_str = now.date().isoformat()
        weekday = now.weekday()

        conn = self._connect()
        try:
            if metodo == "RFID":
                est = conn.execute(
                    "SELECT matricula, nombre, apellido FROM ESTUDIANTES WHERE lower(uid_rfid) = lower(?) AND activo = 1",
                    (str(identificador).strip(),),
                ).fetchone()
            elif metodo == "HUELLA":
                est = conn.execute(
                    "SELECT matricula, nombre, apellido FROM ESTUDIANTES WHERE id_huella = ? AND activo = 1",
                    (int(identificador),),
                ).fetchone()
            else:
                return False, "METODO_INVALIDO"

            if not est:
                return False, "ESTUDIANTE_NO_REGISTRADO"

            matricula = est["matricula"]

            horario = conn.execute(
                """SELECT id_horario, id_materia, hora_inicio, hora_fin, tolerancia_minutos
                   FROM HORARIOS_CLASE
                   WHERE id_salon = ? AND dia_semana = ? AND activo = 1
                     AND hora_inicio <= ? AND hora_fin > ?
                   LIMIT 1""",
                (salon, weekday, current_time, current_time),
            ).fetchone()

            if not horario:
                return False, "SIN_CLASE_ACTIVA"

            inscrito = conn.execute(
                """SELECT 1 FROM INSCRIPCIONES
                   WHERE matricula = ? AND id_materia = ? AND activo = 1""",
                (matricula, horario["id_materia"]),
            ).fetchone()

            if not inscrito:
                return False, "NO_INSCRITO_EN_MATERIA"

            ya_registrado = conn.execute(
                """SELECT estatus FROM REGISTRO_ASISTENCIA
                   WHERE matricula = ? AND id_horario = ? AND substr(fecha_hora, 1, 10) = ?""",
                (matricula, horario["id_horario"], today_str),
            ).fetchone()

            if ya_registrado:
                return True, ya_registrado["estatus"]

            h_ini_dt = datetime.strptime(
                f"{today_str} {horario['hora_inicio']}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=TZ)
            limite_tolerancia = h_ini_dt + timedelta(minutes=int(horario["tolerancia_minutos"] or 15))
            estatus = "PRESENTE" if now <= limite_tolerancia else "RETARDO"

            conn.execute(
                """INSERT INTO REGISTRO_ASISTENCIA
                   (matricula, id_horario, id_salon, fecha_hora, metodo_registro, estatus)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (matricula, horario["id_horario"], salon, now.isoformat(timespec="seconds"), metodo, estatus),
            )
            conn.commit()
            return True, estatus
        except Exception as e:
            conn.rollback()
            return False, f"ERROR_DB: {e}"
        finally:
            conn.close()
