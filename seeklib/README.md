# SeekLib — Sistema Local de Control de Asistencia (RFID + Huella Digital)

Sistema autónomo y 100% offline para Raspberry Pi que gestiona asistencia escolar mediante tarjetas RFID (**RC522** por SPI) y huella dactilar (**AS608** por UART), con panel web local en **Flask + SQLite** y herramienta de enrolamiento por consola.

## Estructura del Proyecto

- `app.py`: Servidor web Flask, API REST, autenticación, reportes CSV y enrolamiento remoto desde el navegador.
- `hardware_controller.py`: Demonio en segundo plano que lee continuamente el RC522 y el AS608 y controla los LEDs y el buzzer.
- `attendance_service.py`: Lógica de validación de horarios activos, tolerancia de retardo, grupos e inscripciones en SQLite.
- `enroll.py`: Utilidad interactiva por línea de comandos (CLI) para administrar estudiantes, credenciales, grupos, salones, materias y horarios.
- `templates/dashboard.html` & `templates/login.html`: Interfaz web de administración y monitoreo en vivo.
- `systemd/`: Archivos de servicio para ejecución automática al arrancar la Raspberry Pi.

## Pines de Hardware (Raspberry Pi BCM)

- **RFID RC522 (SPI0.0):** SDA/CE0 -> GPIO 8, SCK -> GPIO 11, MOSI -> GPIO 10, MISO -> GPIO 9, RST -> GPIO 25, 3.3V y GND.
- **Sensor Huella AS608 (UART):** TX/RX -> `/dev/ttyAMA0` (57600 bps), 3.3V y GND.
- **Indicadores:** LED Verde -> GPIO 17, LED Rojo -> GPIO 27, Buzzer -> GPIO 22.

## Instalación Rápida

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Acceso por defecto al panel web (`http://<IP_RASPBERRY>:8080`):
- **Usuario:** `admin@seeklib.local`
- **Contraseña:** `Admin123!`
