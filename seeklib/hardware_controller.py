import time
import serial
import spidev
from gpiozero import LED, Buzzer, DigitalOutputDevice
from attendance_service import AttendanceService
import adafruit_fingerprint

# Configuración de LEDs e Indicadores Acústicos (GPIO BCM)
led_verde = LED(17)
led_rojo = LED(27)
buzzer = Buzzer(22)

service = AttendanceService()


def exito_feedback():
    led_verde.on()
    buzzer.on()
    time.sleep(0.45)
    buzzer.off()
    time.sleep(0.75)
    led_verde.off()


def error_feedback():
    led_rojo.on()
    for i in range(2):
        buzzer.on()
        time.sleep(0.30)
        buzzer.off()
        if i == 0:
            time.sleep(0.15)
    time.sleep(0.55)
    led_rojo.off()


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
        self._write_reg(self.RF_CFG_REG, 0x70)  # Ganancia máxima de antena (48 dB)
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
        back_data = [self._read_reg(self.FIFO_DATA_REG) for _ in range(n)]
        return True, back_data

    def read_uid(self):
        if (self._read_reg(self.TX_CONTROL_REG) & 0x03) != 0x03:
            print("[RFID] Antena desactivada detectada, reinicializando RC522...", flush=True)
            self._init_chip()

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


def init_rfid():
    try:
        r = RC522Reader(bus=0, device=0, rst_pin=25)
        print(f"[HARDWARE] Lector RFID RC522 conectado correctamente (ver=0x{r.version:02X}).", flush=True)
        return r
    except Exception as e:
        print(f"[HARDWARE] Advertencia: No se pudo iniciar RC522 ({e})", flush=True)
        return None


rfid = init_rfid()

uart = None
try:
    uart = serial.Serial("/dev/ttyAMA0", baudrate=57600, timeout=1)
    finger = adafruit_fingerprint.Adafruit_Fingerprint(uart)
    print("[HARDWARE] Sensor de huella AS608 conectado correctamente.", flush=True)
except Exception as e:
    finger = None
    print(f"[HARDWARE] Advertencia: No se pudo iniciar AS608 ({e})", flush=True)

print("[HARDWARE] Controlador de SeekLib iniciado. Esperando lecturas...", flush=True)


def loop():
    global rfid
    ultimo_evento = ("", 0.0)
    ultimo_reintento_rfid = time.monotonic()
    COOLDOWN_SEG = 2.5

    while True:
        ahora = time.monotonic()

        if rfid is None and (ahora - ultimo_reintento_rfid) >= 5.0:
            ultimo_reintento_rfid = ahora
            rfid = init_rfid()

        # 1. Lectura de Tarjeta RFID (RC522)
        if rfid is not None:
            try:
                uid = rfid.read_uid()
                if uid and not (ultimo_evento[0] == f"RFID:{uid}" and (ahora - ultimo_evento[1]) < COOLDOWN_SEG):
                    ultimo_evento = (f"RFID:{uid}", ahora)
                    print(f"[RFID] Tarjeta detectada UID: {uid}", flush=True)
                    ok, res = service.register(uid, "RFID")
                    print(f"[RFID] Resultado: {res}", flush=True)
                    if ok and res in ("PRESENTE", "RETARDO"):
                        exito_feedback()
                    else:
                        error_feedback()
            except Exception as e:
                print(f"[RFID] Error leyendo SPI ({e}), reiniciando lector...", flush=True)
                rfid.close()
                rfid = None

        # 2. Lectura de Huella (AS608)
        if finger is not None:
            try:
                if finger.get_image() == adafruit_fingerprint.OK:
                    if finger.image_2_tz(1) == adafruit_fingerprint.OK:
                        if finger.finger_search() == adafruit_fingerprint.OK:
                            id_huella = finger.finger_id
                            if not (ultimo_evento[0] == f"HUELLA:{id_huella}" and (ahora - ultimo_evento[1]) < COOLDOWN_SEG):
                                ultimo_evento = (f"HUELLA:{id_huella}", ahora)
                                print(f"[HUELLA] Identificada ID: {id_huella}", flush=True)
                                ok, res = service.register(id_huella, "HUELLA")
                                print(f"[HUELLA] Resultado: {res}", flush=True)
                                if ok and res in ("PRESENTE", "RETARDO"):
                                    exito_feedback()
                                else:
                                    error_feedback()
                        else:
                            if not (ultimo_evento[0] == "HUELLA:ERR" and (ahora - ultimo_evento[1]) < COOLDOWN_SEG):
                                ultimo_evento = ("HUELLA:ERR", ahora)
                                print("[HUELLA] Huella no reconocida", flush=True)
                                error_feedback()
            except Exception as e:
                print(f"[HUELLA] Error serial: {e}", flush=True)

        time.sleep(0.12)


if __name__ == "__main__":
    try:
        loop()
    except KeyboardInterrupt:
        print("\n[HARDWARE] Servidor de hardware detenido.", flush=True)
    finally:
        if rfid is not None:
            rfid.close()
        if uart is not None:
            uart.close()
        led_verde.close()
        led_rojo.close()
        buzzer.close()
