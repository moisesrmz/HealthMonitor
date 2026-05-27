# database_operations.py
import os
import pymysql
from datetime import datetime, date, time as dtime, timedelta

# ============================================================
# Config DB
# ============================================================
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "root",
    "database": "healthmonitor"
}

# Activa MOCK con:
#   Windows (sesión):   set USE_MOCK_DB=True
#   Windows (persist.): setx USE_MOCK_DB True  (abrir nueva consola)
#   Linux/Mac:          export USE_MOCK_DB=True
USE_MOCK_DB = os.getenv("USE_MOCK_DB", "False").lower() == "true"

__all__ = ["insert_test_result", "insert_kpi", "fetch_historico_data"]


# ------------------------- Helpers -------------------------
def _parse_date_flexible(s: str) -> date:
    """Acepta YYYY-MM-DD, DD/MM/YYYY o MM/DD/YYYY."""
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Fecha no reconocida: {s!r}")


def _parse_time_flexible(s: str) -> dtime:
    """Acepta HH:MM:SS (24h)."""
    s = (s or "").strip()
    for fmt in ("%H:%M:%S",):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    raise ValueError(f"Hora no reconocida: {s!r}")


def _parse_ui_datetime(s: str, default_hhmm="00:00") -> datetime:
    """
    Entrada del front: 'YYYY-MM-DD HH:MM' o 'YYYY-MM-DD'.
    Si viene sin hora, aplica default_hhmm.
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("Datetime vacío desde UI")
    if " " not in s:
        s = f"{s} {default_hhmm}"
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def _mock_seed_august_like_yours():
    """Datos MOCK con tu formato DD/MM/YYYY y horas 24h."""
    rows = [
        # 01/08/2025 dentro de 06:00–22:00
        {"SerialNumber": "250801060001F2","PartNumber":"2098706024","TestDate":"09/09/2025","TestTime":"06:41:55","Shift":"T1","FALine":"F2","Tester":"P3","TestResult":"PASS","Failure":"N/A","LVResult":"Sim LV","HVResult":"Sim HV"},
        {"SerialNumber": "250801064208F2","PartNumber":"2098706024","TestDate":"09/09/2025","TestTime":"06:42:10","Shift":"T1","FALine":"F2","Tester":"P3","TestResult":"PASS","Failure":"N/A","LVResult":"Sim LV","HVResult":"Sim HV"},
        {"SerialNumber": "250801121500F2","PartNumber":"2098706024","TestDate":"09/09/2025","TestTime":"12:15:00","Shift":"T1","FALine":"F2","Tester":"P3","TestResult":"FAIL","Failure":"Sello","LVResult":"Sim LV","HVResult":"Sim HV"},
        {"SerialNumber": "250801215100F5","PartNumber":"2154160034","TestDate":"09/09/2025","TestTime":"21:51:00","Shift":"T2","FALine":"F5","Tester":"EOL2","TestResult":"PASS","Failure":"N/A","LVResult":"Sim LV","HVResult":"Sim HV"},

        # 02/08/2025 dentro de 06:00–22:00
        {"SerialNumber": "250802060501F2","PartNumber":"2098706024","TestDate":"08/09/2025","TestTime":"06:05:01","Shift":"T1","FALine":"F2","Tester":"P3","TestResult":"PASS","Failure":"N/A","LVResult":"Sim LV","HVResult":"Sim HV"},
        {"SerialNumber": "250802150000F4","PartNumber":"2154160034","TestDate":"08/09/2025","TestTime":"15:00:00","Shift":"T2","FALine":"F4","Tester":"EOL5","TestResult":"FAIL","Failure":"Corto","LVResult":"Sim LV","HVResult":"Sim HV"},

        # 02/08/2025 fuera de rango por hora final (23:50:50)
        {"SerialNumber": "250802235050F5","PartNumber":"2098706024","TestDate":"08/09/2025","TestTime":"23:50:50","Shift":"T2","FALine":"F5","Tester":"EOL2","TestResult":"PASS","Failure":"N/A","LVResult":"Sim LV","HVResult":"Sim HV"},

        # 01/09 para validar que no entra cuando filtras agosto
        {"SerialNumber": "250901064153F2","PartNumber":"2098706024","TestDate":"08/09/2025","TestTime":"06:41:55","Shift":"T1","FALine":"F2","Tester":"P3","TestResult":"PASS","Failure":"N/A","LVResult":"Sim LV","HVResult":"Sim HV"},
    ]
    return rows


# ==============================
# INSERTS
# ==============================
def insert_test_result(data: dict):
    """
    data = {
        "SerialNumber": str,
        "PartNumber": str,
        "TestDate": "YYYY-MM-DD" o date,
        "TestTime": "HH:MM:SS" o time,
        "Shift": str,
        "FALine": str,
        "Tester": str,
        "TestResult": "PASS"/"FAIL",
        "Failure": str,
        "LVResult": str,
        "HVResult": str
    }
    """
    if USE_MOCK_DB:
        print("[MOCK] insert_test_result llamado (sin guardar en DB).", data)
        return

    # Normaliza tipos para MySQL
    td = data.get("TestDate")
    if isinstance(td, str):
        td = _parse_date_flexible(td)
    tt = data.get("TestTime")
    if isinstance(tt, str):
        tt = _parse_time_flexible(tt)

    query = """
        INSERT INTO TestResults (
            SerialNumber, PartNumber, TestDate, TestTime, Shift, FALine, Tester,
            TestResult, Failure, LVResult, HVResult
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    connection = None
    try:
        connection = pymysql.connect(**DB_CONFIG)
        with connection.cursor() as cursor:
            cursor.execute(query, (
                data.get("SerialNumber"),
                data.get("PartNumber"),
                td,               # DATE
                tt,               # TIME
                data.get("Shift"),
                data.get("FALine"),
                data.get("Tester"),
                data.get("TestResult"),
                data.get("Failure"),
                data.get("LVResult"),
                data.get("HVResult"),
            ))
        connection.commit()
        print("[INFO] Resultado insertado correctamente.")
    except pymysql.MySQLError as e:
        print(f"[ERROR] insert_test_result: {e}")
    finally:
        if connection:
            connection.close()


def insert_kpi(data: dict):
    """
    data = {
        "shift": str,
        "FALine": str,
        "ok": int,
        "nok": int,
        "yield": float,
        "operativeTime": float,
        "availability": float,
        "performance": float,
        "OEE": float
    }
    """
    if USE_MOCK_DB:
        print("[MOCK] insert_kpi llamado (sin guardar en DB).", data)
        return

    query = """
        INSERT INTO kpis (`shift`, `FALine`, `ok`, `nok`, `yield`,
                          `operativeTime`, `availability`, `performance`, `OEE`)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    connection = None
    try:
        connection = pymysql.connect(**DB_CONFIG)
        with connection.cursor() as cursor:
            cursor.execute(query, (
                data.get("shift"),
                data.get("FALine"),
                data.get("ok"),
                data.get("nok"),
                data.get("yield"),
                data.get("operativeTime"),
                data.get("availability"),
                data.get("performance"),
                data.get("OEE"),
            ))
        connection.commit()
        print("[INFO] KPI insertado correctamente.")
    except pymysql.MySQLError as e:
        print(f"[ERROR] insert_kpi: {e}")
    finally:
        if connection:
            connection.close()


# ==============================
# SELECT con fecha+hora
# ==============================
#def fetch_historico_data(start_date: str, end_date: str, part_number: str | None = None):
def fetch_historico_data(start_date: str, end_date: str, part_number: str | None = None, include_results: bool = True, exclude_ao: bool = True):
    """
    Filtro por rango de fecha+hora inclusivo.
    - start_date/end_date vienen del front como 'YYYY-MM-DD HH:MM' (flatpickr).
    - Si USE_MOCK_DB=True => filtra en memoria sobre el mock.
    - Si DB real => usa TIMESTAMP(TestDate, TestTime) BETWEEN %s AND %s.
    """
    try:
        start_dt = _parse_ui_datetime(start_date, default_hhmm="06:00")
        end_dt = _parse_ui_datetime(end_date, default_hhmm="22:00")
    except Exception as e:
        print(f"[ERROR] parse UI datetimes: {e}")
        return []

    print(f"[DEBUG] IN start={start_dt} end={end_dt} pn={part_number} USE_MOCK_DB={USE_MOCK_DB}")

    if USE_MOCK_DB:
        rows = _mock_seed_august_like_yours()
        out = []
        for r in rows:
            try:
                d = _parse_date_flexible(r["TestDate"])
                t = _parse_time_flexible(r["TestTime"])
                dt = datetime.combine(d, t)
            except Exception as e:
                print(f"[WARN] mock row skip (parse): {e} -> {r}")
                continue

            if not (start_dt <= dt <= end_dt):
                continue
            if part_number and part_number.strip() and part_number not in (r.get("PartNumber") or ""):
                continue

            out.append({
                "SerialNumber": r["SerialNumber"],
                "PartNumber": r["PartNumber"],
                "TestDate": d.strftime("%Y-%m-%d"),
                "TestTime": t.strftime("%H:%M:%S"),
                "Shift": r.get("Shift", ""),
                "FALine": r.get("FALine", ""),
                "Tester": r.get("Tester", ""),
                "TestResult": r.get("TestResult", ""),
                "Failure": r.get("Failure", "N/A"),
                "LVResult": r.get("LVResult", ""),
                "HVResult": r.get("HVResult", ""),
            })
        print(f"[DEBUG] MOCK rows filtered: {len(out)}")
        return out

    # DB real
    connection = None
    try:
        connection = pymysql.connect(**DB_CONFIG)
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            if include_results:
                select_fields = """
                    SerialNumber,
                    PartNumber,
                    DATE_FORMAT(TestDate, '%%Y-%%m-%%d') AS TestDate,
                    TIME_FORMAT(TestTime, '%%H:%%i:%%s') AS TestTime,
                    Shift,
                    FALine,
                    Tester,
                    TestResult,
                    Failure,
                    LVResult,
                    HVResult
                """
            else:
                select_fields = """
                    SerialNumber,
                    PartNumber,
                    DATE_FORMAT(TestDate, '%%Y-%%m-%%d') AS TestDate,
                    TIME_FORMAT(TestTime, '%%H:%%i:%%s') AS TestTime,
                    Shift,
                    FALine,
                    Tester,
                    TestResult,
                    Failure
                """
            ####
            base_query = f"""
                SELECT {select_fields}
                FROM TestResults
                WHERE TIMESTAMP(TestDate, TestTime) BETWEEN %s AND %s
            """
            params = [start_dt, end_dt]

            if exclude_ao:
                base_query += " AND Tester NOT LIKE 'AO%%'"

            if part_number and part_number.strip():
                base_query += " AND PartNumber LIKE %s"
                params.append(f"%{part_number.strip()}%")

            cursor.execute(base_query, params)
            rows = cursor.fetchall()
            print(f"[DEBUG] DB rows fetched: {len(rows)}")
            return rows

    except pymysql.MySQLError as e:
        print(f"[ERROR] DB error: {e}  (tip: activa USE_MOCK_DB=True para probar sin DB)")
        return []
    finally:
        if connection:
            connection.close()
