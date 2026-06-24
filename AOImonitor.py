import os
import json
import time
from datetime import datetime
import re
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from database_operations import insert_test_result

# =========================================================
# CONFIG
# =========================================================

WATCH_FOLDER = r"\\10.39.2.11\Group\programs\LABVIEW\Dashboarding\RackData\AO"

STATE_FOLDER = r"C:\AO_Monitor"
os.makedirs(STATE_FOLDER, exist_ok=True)

STATE_FILE = os.path.join(STATE_FOLDER, "ao_monitor_state.json")

# =========================================================
# GLOBALS
# =========================================================

file_positions = {}
headline2_map = {}

# Descuento AOI: 2 PASS + 8 FAIL por Tester + PN + Shift
validation_status = {}

# =========================================================
# LOAD / SAVE STATE
# =========================================================

def load_state():
    global file_positions

    if not os.path.exists(STATE_FILE):
        print("[STATE] No previous state file")
        return

    try:
        with open(STATE_FILE, "r") as f:
            file_positions = json.load(f)

        print(f"[STATE LOADED] {len(file_positions)} files")

    except Exception as e:
        print("[STATE LOAD ERROR]")
        print(e)


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(file_positions, f, indent=2)

    except Exception as e:
        print("[STATE SAVE ERROR]")
        print(e)

# =========================================================
# SHIFT
# =========================================================

def determine_shift(dt_obj):
    total_minutes = dt_obj.hour * 60 + dt_obj.minute

    t1_start = 6 * 60 + 30
    t1_end   = 14 * 60 + 30

    t2_start = 14 * 60 + 30
    t2_end   = 22 * 60

    if t1_start <= total_minutes < t1_end:
        return "T1"
    elif t2_start <= total_minutes < t2_end:
        return "T2"
    else:
        return "T3"

# =========================================================
# FAILURES
# =========================================================
def normalize_failure_name(header_name):

    text = header_name.strip()

    # Quitar unidad
    text = text.replace("(mm)", "").strip()

    # Buscar PN del conector
    match = re.search(
        r'\b(?:AMZ\w*|59Z\d+)-[A-Z0-9]+-[A-Z0-9]+(?:-[A-Z0-9]+)*',
        text
    )

    if not match:
        return text

    # Conservar desde PN hacia adelante
    normalized = text[match.start():].strip()

    # Reemplazar "_" por espacios
    normalized = normalized.replace("_", " ")

    # Quitar DUAL
    normalized = normalized.replace("DUAL ", "")

    # Limpiar espacios dobles
    normalized = re.sub(r'\s+', ' ', normalized).strip()

    # Separar PN del detalle
    parts = normalized.split(" ", 1)

    pn = parts[0]
    detail = parts[1] if len(parts) > 1 else ""

    if detail:
        return f"{pn} | {detail}"

    return pn
def extract_failures(headers, values):
    failures = []

    for i in range(0, min(len(headers), len(values)), 2):
        header_name = headers[i].strip() if i < len(headers) else f"M{i}"
        measured_value = values[i].strip() if i < len(values) else ""
        result_flag = values[i + 1].strip().upper() if i + 1 < len(values) else ""

        if result_flag in ["HI", "LO", "FAIL", "NOK"]:
            normalized = normalize_failure_name(header_name)
            failures.append(normalized)

    # Solo regresar la primera falla
    if failures:
        return failures[0]

    return ""
# =========================================================
# LV RESULT
# =========================================================
def build_lv_result(headers, values):
    pairs = []

    for i in range(0, min(len(headers), len(values)), 2):
        header_name = headers[i].strip() if i < len(headers) else f"M{i}"
        measured_value = values[i].strip() if i < len(values) else ""
        result_flag = values[i + 1].strip() if i + 1 < len(values) else ""

        if not header_name:
            continue

        normalized_header = normalize_failure_name(header_name)

        pairs.append(f"{normalized_header}={measured_value}({result_flag})")

    return "; ".join(pairs)
# =========================================================
# PROCESS DATALINE
# =========================================================

def process_dataline(file_path, headers, line):
    try:
        cols = line.strip().split("\t")

        if len(cols) < 11:
            print("[WARNING] DATALINE incompleta")
            return

        # -------------------------------------------------
        # CAMPOS BASE
        # -------------------------------------------------

        measurement_id = cols[1]
        test_start     = cols[2]
        serial_number  = cols[3]
        test_duration  = cols[4]
        test_result    = cols[5].upper()
        station_id     = cols[6]
        exchangekit_id = cols[7]
        part_number    = cols[8]
        process_step   = cols[9]
        socket_number  = cols[10]

        # -------------------------------------------------
        # FECHA / HORA
        # -------------------------------------------------

        dt = datetime.strptime(test_start, "%d.%m.%Y %H:%M:%S")

        test_date = dt.strftime("%Y-%m-%d")
        test_time = dt.strftime("%H:%M:%S")
        shift = determine_shift(dt)

        # -------------------------------------------------
        # MEDICIONES
        # -------------------------------------------------

        measurement_headers = headers[11:]
        measurement_values  = cols[11:]

        failure_text = extract_failures(measurement_headers, measurement_values)
        lv_result = build_lv_result(measurement_headers, measurement_values)

        # -------------------------------------------------
        # FAILURE LOGIC
        # -------------------------------------------------

        if test_result == "PASS":
            failure_final = "N/A"
        else:
            if failure_text.strip():
                failure_final = failure_text
            else:
                failure_final = "AO_FAIL_GENERIC"

        # -------------------------------------------------
        # AO -> FA LINE MAPPING
        # -------------------------------------------------

        tester = station_id.strip().upper()

        ao_to_fa = {
            "AO01": "F4",
            "AO02": "F8"
        }

        fa_line = ao_to_fa.get(tester, "AOI")

        # -------------------------------------------------
        # AOI VALIDATION DISCOUNT
        # 2 PASS + 8 FAIL por Tester + PN + Shift
        # -------------------------------------------------

        validation_key = f"{tester}_{part_number}_{shift}"

        if validation_key not in validation_status:
            validation_status[validation_key] = {
                "pass_left": 2,
                "fail_left": 2
            }

        status_control = validation_status[validation_key]

        if test_result == "PASS" and status_control["pass_left"] > 0:
            status_control["pass_left"] -= 1
            print(
                f"[AOI SKIP PASS VALIDATION] "
                f"Tester:{tester} PN:{part_number} Shift:{shift} "
                f"Remaining PASS:{status_control['pass_left']}"
            )
            return

        if test_result == "FAIL" and status_control["fail_left"] > 0:
            status_control["fail_left"] -= 1
            print(
                f"[AOI SKIP FAIL VALIDATION] "
                f"Tester:{tester} PN:{part_number} Shift:{shift} "
                f"Remaining FAIL:{status_control['fail_left']}"
            )
            return

        # -------------------------------------------------
        # DB ROW
        # -------------------------------------------------

        db_row = {
            "SerialNumber": serial_number,
            "PartNumber": part_number,
            "TestDate": test_date,
            "TestTime": test_time,
            "Shift": shift,
            "FALine": fa_line,
            "Tester": tester,
            "TestResult": test_result,
            "Failure": failure_final,
            "LVResult": lv_result,
            "HVResult": test_duration
        }

        print("\n" + "=" * 80)
        print("[AO INSERT]")
        print("=" * 80)

        for k, v in db_row.items():
            print(f"{k:15}: {v}")

        insert_test_result(db_row)

    except Exception as e:
        print("[ERROR PROCESSING DATALINE]")
        print(e)

# =========================================================
# PROCESS FILE
# =========================================================

def process_file(file_path):
    global file_positions
    global headline2_map

    try:
        current_size = os.path.getsize(file_path)

        # -------------------------------------------------
        # NUEVO ARCHIVO
        # -------------------------------------------------

        if file_path not in file_positions:
            print(f"\n[NEW FILE DETECTED]")
            print(file_path)

            file_positions[file_path] = current_size
            save_state()

            print(f"[SEEK EOF] {current_size}")
            return

        last_position = file_positions.get(file_path, 0)

        # -------------------------------------------------
        # ARCHIVO TRUNCADO
        # -------------------------------------------------

        if current_size < last_position:
            print(f"\n[FILE RESET DETECTED]")
            print(file_path)
            last_position = 0

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            f.seek(last_position)

            lines_processed = 0

            while True:
                line = f.readline()

                if not line:
                    break

                line = line.strip()

                if not line:
                    continue

                # -------------------------------------------------
                # HEADLINE2
                # -------------------------------------------------

                if line.startswith("HEADLINE2"):
                    headers = line.split("\t")
                    headline2_map[file_path] = headers

                    print("\n" + "=" * 80)
                    print("[HEADERS UPDATED]")
                    print(os.path.basename(file_path))
                    print("=" * 80)

                    continue

                # -------------------------------------------------
                # DATALINE
                # -------------------------------------------------

                if line.startswith("DATALINE"):
                    headers = headline2_map.get(file_path)

                    if not headers:
                        print("[WARNING] No HEADLINE2 detected yet")
                        continue

                    process_dataline(file_path, headers, line)
                    lines_processed += 1

            # -------------------------------------------------
            # GUARDAR POSICIÓN
            # -------------------------------------------------

            new_position = f.tell()
            file_positions[file_path] = new_position

            save_state()

            if lines_processed:
                print(f"\n[STATE SAVED]")
                print(f"Lines processed : {lines_processed}")
                print(f"Position        : {new_position}")

    except Exception as e:
        print("\n[FILE ERROR]")
        print(file_path)
        print(e)

# =========================================================
# WATCHDOG
# =========================================================

class AOHandler(FileSystemEventHandler):

    def on_modified(self, event):
        if event.is_directory:
            return

        if not event.src_path.lower().endswith(".dat"):
            return

        process_file(event.src_path)

    def on_created(self, event):
        if event.is_directory:
            return

        if not event.src_path.lower().endswith(".dat"):
            return

        time.sleep(0.2)

        file_positions[event.src_path] = 0
        save_state()

        process_file(event.src_path)

# =========================================================
# INITIAL SCAN
# =========================================================

def initial_scan():
    print("\n[INITIAL SCAN]")

    for root, dirs, files in os.walk(WATCH_FOLDER):
        for file in files:
            if not file.lower().endswith(".dat"):
                continue

            full_path = os.path.join(root, file)

            if full_path in file_positions:
                continue

            try:
                size = os.path.getsize(full_path)
                file_positions[full_path] = size

                print(f"[INIT EOF] {file}")
                print(f"Size: {size}")

            except Exception as e:
                print(f"[INIT ERROR] {file}")
                print(e)

    save_state()

# =========================================================
# MAIN
# =========================================================

def start_ao_monitoring():
    print("=" * 80)
    print("AO MONITOR STARTED")
    print("=" * 80)
    print(WATCH_FOLDER)

    load_state()
    initial_scan()

    observer = Observer()

    observer.schedule(
        AOHandler(),
        WATCH_FOLDER,
        recursive=True
    )

    observer.start()

    print("\n[WATCHING FOR NEW DATA...]")

    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        observer.stop()

    observer.join()