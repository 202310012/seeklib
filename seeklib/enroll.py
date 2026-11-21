import os
import sqlite3
import subprocess
import time
import serial
import spidev
from gpiozero import DigitalOutputDevice
import adafruit_fingerprint

DB_PATH = os.getenv("SEEKLIB_DB", "data/seeklib.sqlite3")


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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


def enrolar_rfid():
    matricula = input("Matrícula del alumno: ").strip().upper()
    print("Acerca la tarjeta RFID al lector RC522...")
    reader = RC522Reader(bus=0, device=0, rst_pin=25)
    try:
        uid = None
        while not uid:
            uid = reader.read_uid()
            time.sleep(0.1)
        print(f"Tarjeta leída: {uid}")
        conn = get_conn()
        conn.execute("UPDATE ESTUDIANTES SET uid_rfid = ? WHERE matricula = ?", (uid, matricula))
        conn.commit()
        conn.close()
        print("RFID asociado correctamente.")
    finally:
        reader.close()


def enrolar_huella():
    matricula = input("Matrícula del alumno: ").strip().upper()
    slot = int(input("Slot de memoria en AS608 (1-127): ").strip())
    uart = serial.Serial("/dev/ttyAMA0", baudrate=57600, timeout=1)
    finger = adafruit_fingerprint.Adafruit_Fingerprint(uart)
    try:
        for paso in (1, 2):
            print(f"Coloca el dedo en el sensor (lectura {paso}/2)...")
            while finger.get_image() != adafruit_fingerprint.OK:
                time.sleep(0.1)
            if finger.image_2_tz(paso) != adafruit_fingerprint.OK:
                print("Error procesando imagen.")
                return
            if paso == 1:
                print("Retira el dedo...")
                time.sleep(1.5)
        if finger.create_model() != adafruit_fingerprint.OK:
            print("Las huellas no coincidieron.")
            return
        if finger.store_model(slot) != adafruit_fingerprint.OK:
            print("Error guardando modelo en AS608.")
            return
        conn = get_conn()
        conn.execute("UPDATE ESTUDIANTES SET id_huella = ? WHERE matricula = ?", (slot, matricula))
        conn.commit()
        conn.close()
        print(f"Huella guardada en slot {slot} y vinculada a {matricula}.")
    finally:
        uart.close()


def main():
    subprocess.run(["sudo", "systemctl", "stop", "seeklib-hardware.service"], check=False)
    try:
        while True:
            print("\n=== SEEKLIB CLI ENROLL ===")
            print("1. Registrar / actualizar alumno")
            print("2. Enrolar tarjeta RFID")
            print("3. Enrolar huella digital (AS608)")
            print("4. Listar alumnos")
            print("0. Salir")
            op = input("Opción: ").strip()
            if op == "1":
                mat = input("Matrícula: ").strip().upper()
                nom = input("Nombre: ").strip()
                ape = input("Apellido: ").strip()
                conn = get_conn()
                conn.execute(
                    """INSERT INTO ESTUDIANTES (matricula, nombre, apellido, activo)
                       VALUES (?, ?, ?, 1)
                       ON CONFLICT(matricula) DO UPDATE SET nombre=excluded.nombre, apellido=excluded.apellido""",
                    (mat, nom, ape),
                )
                conn.commit()
                conn.close()
                print("Alumno guardado.")
            elif op == "2":
                enrolar_rfid()
            elif op == "3":
                enrolar_huella()
            elif op == "4":
                conn = get_conn()
                for r in conn.execute("SELECT matricula, nombre, apellido, uid_rfid, id_huella FROM ESTUDIANTES"):
                    print(dict(r))
                conn.close()
            elif op == "0":
                break
    finally:
        subprocess.run(["sudo", "systemctl", "start", "seeklib-hardware.service"], check=False)


if __name__ == "__main__":
    main()
