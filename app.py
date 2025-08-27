import os
import time
import re
import datetime
from datetime import timedelta
from flask import request, jsonify
from flask import Flask, render_template
from flask_socketio import SocketIO
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from collections import defaultdict
import threading
from database_operations import insert_test_result
from database_operations import fetch_historico_data
from database_operations import insert_kpi
import numpy as np
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import csv
from flask import make_response
import io

app = Flask(__name__)
socketio = SocketIO(app)
last_reset_time = datetime.datetime.now()
current_mode = "OEE"
poee_start_times = defaultdict(lambda: None)
inactivity_accumulated_oee = defaultdict(float)
inactivity_accumulated_poee = defaultdict(float)
activation_pass_counter = defaultdict(int)
activation_status = defaultdict(lambda: {
    "active": False,
    "discount_pass": 2,
    "discount_fail": 0,
    "current_part_number": None
})

part_fail_discounts = {}  # Cargado desde CSV

pass_fail_counts = defaultdict(lambda: {"Passed": 0, "Failed": 0, "Reference": "", "Test Name": "", "Nombre de la prueba": ""})
folder_labels = {
    "EOL1": "F1",
    "P3": "F2",
    "P2": "F3",
    "EOL5": "F4",
    "EOL2": "F5",
    "EOL3": "F9",
    "EOL4": "F6",
    "EOL6": "F7",
    "EOL7": "F8"
}
counts_lock = threading.Lock()
cycle_times = defaultdict(list)
last_file_times = defaultdict(lambda: None)
is_resetting = False 

class NewFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        if is_resetting:
            print(f"[WAIT] Ignorando archivo durante reset: {event.src_path}")
            return
        file_path = event.src_path
        parent_folder = get_line_folder(file_path)
        print(f"[INFO] Nuevo archivo detectado: {file_path}")

        with counts_lock:
            line_label = folder_labels.get(parent_folder, "F1")

        for attempt in range(5):
            if os.path.exists(file_path):
                try:
                    timestamp = datetime.datetime.now()
                    calculate_cycle_time(parent_folder, timestamp)

                    with open(file_path, 'r') as file:
                        lines = file.readlines()

                    passed, failed = 0, 0
                    status, reference, test_name, nombre_prueba = None, None, None, None
                    serial_number, test_date, test_time = None, None, None
                    LVResult, HVResult = "", ""
                    failure_line, sFailure = None, None
                    failure_keywords = ["Failed", "*ERROR", "*MISTAKE", "NO MEASUREMENT", ">"]
                    current_table = None

                    for line in lines:
                        line = line.strip()
                        if any(keyword in line for keyword in ["Status:", "Final Test Result:", "Resultado final de la prueba:"]):
                            if any(success in line for success in ["Passed", "*PASS", "Pasa"]):
                                status = "Pass"
                            elif any(fail in line for fail in ["Failed", "*FAIL", "Falla"]):
                                status = "Fail"
                        if "Reference:" in line:
                            reference = re.sub(r'^[*,\s]+', '', line.split("Reference:")[-1].strip())
                        elif "Nombre de la prueba:" in line:
                            test_name = re.sub(r'^[*,\s]+', '', line.split("Nombre de la prueba:")[-1].strip())
                        elif "Test Name:" in line:
                            nombre_prueba = re.sub(r'^[*,\s]+', '', line.split("Test Name:")[-1].strip())

                        if "Serial number:" in line and not line.startswith("EOL Serial number:"):
                            serial_number = re.sub(r'^[*,\s]+', '', line.split("Serial number:")[-1].strip())
                            if len(serial_number) >= 14:
                                year = serial_number[:2]
                                month = serial_number[2:4]
                                day = serial_number[4:6]
                                hour = serial_number[6:8]
                                minute = serial_number[8:10]
                                second = serial_number[10:12]
                                year = f"20{year}"
                                test_date = f"{month}/{day}/{year}"
                                test_time = f"{hour}:{minute}:{second}"
                        elif "Test Date:" in line:
                            test_date = re.sub(r'^[*,\s]+', '', line.split("Test Date:")[-1].strip())
                        elif "Test Time:" in line:
                            test_time_raw = re.sub(r'^[*,\s]+', '', line.split("Test Time:")[-1].strip())
                            try:
                                test_time = datetime.datetime.strptime(test_time_raw, "%I:%M:%S %p").strftime("%H:%M:%S")
                                dt_combined = datetime.datetime.strptime(f"{test_date} {test_time}", "%m/%d/%Y %H:%M:%S")
                                adjusted = dt_combined - datetime.timedelta(seconds=2)
                                serial_number = adjusted.strftime("%y%m%d%H%M%S") + folder_labels.get(parent_folder, "F1")
                            except Exception as e:
                                print(f"[ERROR] Tiempo inválido: {e}")

                        if line.startswith("Measured value") or line.startswith("#"):
                            current_table = "LVResult"
                            continue
                        if line.startswith("HiPotTest") or line.startswith("Name"):
                            current_table = "HVResult"
                            continue
                        if current_table == "LVResult" and line:
                            LVResult += line + "*"
                        elif current_table == "HVResult" and line:
                            HVResult += line + "*"

                    if status == "Fail":
                        failure_line = next((l for l in LVResult.split('*') if any(k in l for k in failure_keywords)), None)
                        if not failure_line:
                            failure_line = next((l for l in HVResult.split('*') if any(k in l for k in failure_keywords)), None)
                        sFailure = "Dielectrico"
                        if failure_line:
                            l = failure_line.lower()
                            if "nucleo" in l and "wire" in l:
                                sFailure = "Nucleo"
                            elif "nucleo" in l and "4wire" in l:
                                sFailure = "Alta Resistencia"
                            elif "malla" in l and "wire" in l:
                                sFailure = "Malla"
                            elif "malla" in l and "4wire" in l:
                                sFailure = "Alta Resistencia"
                            elif "tpa" in l:
                                sFailure = "TPA"
                            elif "lua" in l:
                                sFailure = "Dielectrico LUA"
                            elif "cpa" in l:
                                sFailure = "CPA"
                            elif "sello" in l or "seal" in l:
                                sFailure = "Sello"
                            elif "cover" in l:
                                sFailure = "Cover"
                            elif "no continuity" in l:
                                sFailure = "Nucleo"
                            elif "shortcircuit" in l:
                                sFailure = "Corto"
                            elif "high resistance" in l:
                                sFailure = "Alta Resistencia"
                    print(f"🚨🚨🚨🚨🚨[DEBUG] status: {status}")
                    print(f"🚨🚨🚨🚨🚨[DEBUG] Motivo de falla clasificado: {sFailure} | Línea analizada: {failure_line}")
                    current_part = reference or test_name or nombre_prueba
                    prev_part = activation_status[parent_folder]["current_part_number"]

                    if current_part and prev_part != current_part:
                        reset_line_state(parent_folder, current_part)

                    real_passed = 0
                    if not activation_status[parent_folder]["active"]:
                        if status == "Pass":
                            if activation_status[parent_folder]["discount_pass"] > 0:
                                activation_status[parent_folder]["discount_pass"] -= 1
                                print(f"[SKIP PASS] {parent_folder} - Remaining: {activation_status[parent_folder]['discount_pass']}")
                            else:
                                activation_pass_counter[parent_folder] += 1
                                real_passed = 1
                                print(f"[COUNTED PASS] {parent_folder} - Contador interno: {activation_pass_counter[parent_folder]}")

                        elif status == "Fail":
                            if activation_status[parent_folder]["discount_fail"] > 0:
                                activation_status[parent_folder]["discount_fail"] -= 1
                                print(f"[SKIP FAIL] {parent_folder} - Remaining: {activation_status[parent_folder]['discount_fail']}")
                                failed = 0
                            else:
                                failed = 1

                        if activation_pass_counter[parent_folder] == 4:
                            pass_fail_counts[parent_folder]["Passed"] += 4
                            real_passed = 0
                            activation_status[parent_folder]["active"] = True
                            if not poee_start_times.get(parent_folder):
                                poee_start_times[parent_folder] = timestamp - datetime.timedelta(seconds=60)
                            print(f"[🟢 ACTIVADA] Línea {parent_folder} ACTIVADA tras 6 Passed (2 skip + 4 válidos)")

                        if not activation_status[parent_folder]["active"]:
                            print(f"[⛔ NO ACTIVA] {parent_folder}, esperando más Passed/Fail")
                            return
                    else:
                        if status == "Pass":
                            real_passed = 1
                        elif status == "Fail":
                            failed = 1

                    oee_data = calculate_oee(parent_folder)
                    adjusted_elapsed_time = oee_data["adjusted_elapsed_time"]
                    if parent_folder not in inactivity_accumulated_oee:
                        inactivity_accumulated_oee[parent_folder] = adjusted_elapsed_time

                    data_to_insert = {
                        "SerialNumber": serial_number,
                        "PartNumber": current_part,
                        "TestDate": datetime.datetime.strptime(test_date, "%m/%d/%Y").strftime("%Y-%m-%d") if test_date else None,
                        "TestTime": test_time,
                        "Shift": determine_shift(test_time),
                        "FALine": folder_labels.get(parent_folder, "F1"),
                        "Tester": "EOL1" if parent_folder[0].isdigit() else parent_folder,
                        "TestResult": status,
                        "Failure": sFailure or "N/A",
                        "LVResult": LVResult,
                        "HVResult": HVResult
                    }
                    # insert_test_result(data_to_insert)  # cuando quieras habilitar DB

                    with counts_lock:
                        pass_fail_counts[parent_folder]["Passed"] += real_passed
                        pass_fail_counts[parent_folder]["Failed"] += failed
                        pass_fail_counts[parent_folder]["Reference"] = reference or "N/A"
                        pass_fail_counts[parent_folder]["Test Name"] = test_name or "N/A"
                        pass_fail_counts[parent_folder]["Nombre de la prueba"] = nombre_prueba or "N/A"

                        print(
                            f"[DEBUG] Resultado: {status}, Línea: {parent_folder}, "
                            f"Active: {activation_status[parent_folder]['active']}, "
                            f"Discount FAILs left: {activation_status[parent_folder]['discount_fail']}, "
                            f"Contado como Fail: {failed}"
                        )

                    emit_data()
                    return
                except PermissionError:
                    time.sleep(1)
                except Exception as e:
                    print(f"[ERROR] Error al procesar el archivo {file_path}: {e}")
                    return
            else:
                time.sleep(0.5)


# Variables globales adicionales
inactivity_start_time = {}
flag_state = defaultdict(lambda: False)
inactivity_start_time_oee = {}
inactivity_start_time_poee = {}
inactivity_state = defaultdict(lambda: False)  # True = inactivo


# -------------------------------
# Funciones auxiliares
# -------------------------------
def get_line_folder(file_path):
    parts = os.path.normpath(file_path).split(os.sep)
    parts_lower = [p.lower() for p in parts]
    try:
        idx = parts_lower.index("logfiles")
        if idx + 1 < len(parts):
            line_name = parts[idx + 1].strip()
            print(f"[DEBUG] Línea detectada: {line_name} (en path: {file_path})")
            return line_name
        else:
            print(f"[WARNING] No hay línea después de 'LogFiles': {file_path}")
            return "Unknown"
    except ValueError:
        print(f"[WARNING] 'LogFiles' no está en el path: {file_path}")
        return "Unknown"


def emit_data():
    data = []
    now = datetime.datetime.now()

    for parent_folder, counts in pass_fail_counts.items():
        if parent_folder[0].isdigit():
            label = "F1"
        else:
            label = folder_labels.get(parent_folder, parent_folder)

        total_tests = counts["Passed"] + counts["Failed"]
        yield_value = (counts["Passed"] / total_tests) * 100 if total_tests > 0 else 0
        avg_cycle_time = get_average_cycle_time(parent_folder)
        last_time = last_file_times[parent_folder]
        current_state = 1

        if avg_cycle_time is None or last_time is None:
            avg_cycle_time = 0
            if not flag_state[parent_folder]:
                current_state = 1
                flag_state[parent_folder] = True
            else:
                current_state = 0
        else:
            if (now - last_time).total_seconds() > (avg_cycle_time + avg_cycle_time * 0.3):
                current_state = 0

        if current_state == 0:
            if not inactivity_state[parent_folder]:
                inactivity_start_time_oee[parent_folder] = now
                print(f"[⏱️ OEE] INICIO inactividad en {parent_folder} a {now.strftime('%H:%M:%S')}")
            else:
                elapsed_oee = (now - inactivity_start_time_oee[parent_folder]).total_seconds()
                inactivity_accumulated_oee[parent_folder] += elapsed_oee
                inactivity_start_time_oee[parent_folder] = now
                print(f"[➕ OEE] +{elapsed_oee:.2f}s acumulados en {parent_folder}")

            if poee_start_times.get(parent_folder):
                if not inactivity_state[parent_folder]:
                    inactivity_start_time_poee[parent_folder] = now
                    print(f"[⏱️ POEE] INICIO inactividad en {parent_folder} a {now.strftime('%H:%M:%S')}")
                else:
                    elapsed_poee = (now - inactivity_start_time_poee[parent_folder]).total_seconds()
                    inactivity_accumulated_poee[parent_folder] += elapsed_poee
                    inactivity_start_time_poee[parent_folder] = now
                    print(f"[➕ POEE] +{elapsed_poee:.2f}s acumulados en {parent_folder}")

            inactivity_state[parent_folder] = True
        else:
            inactivity_state[parent_folder] = False
            inactivity_start_time_oee.pop(parent_folder, None)
            inactivity_start_time_poee.pop(parent_folder, None)

        oee_data = calculate_oee(parent_folder)
        data.append({
            "label": label,
            "yield": yield_value,
            "total_tests": total_tests,
            "passed": counts["Passed"],
            "failed": counts["Failed"],
            "reference": counts["Reference"],
            "test_name": counts["Test Name"],
            "nombre_prueba": counts["Nombre de la prueba"],
            "avg_cycle_time": avg_cycle_time,
            "state": current_state,
            "availability": oee_data["availability"],
            "operational_time": oee_data["operational_time"],
            "performance": oee_data["performance"],
            "quality": oee_data["quality"],
            "oee": oee_data["oee"],
            "inactive_time": oee_data["adjusted_inactive_time"],
            "elapsed_time": oee_data["adjusted_elapsed_time"],
            "poee_start_time": poee_start_times.get(parent_folder).strftime("%H:%M:%S") if poee_start_times.get(parent_folder) else "N/A",
        })

    socketio.emit('update_data', data)
def calculate_oee(line):
    global last_reset_time
    now = datetime.datetime.now()
    if current_mode == "POEE":
        start_time = poee_start_times.get(line)
        if not start_time:
            if activation_status[line]["active"]:
                print(f"[WARN] Línea {line} activa sin poee_start_time. Asignando ahora.")
                start_time = now
                poee_start_times[line] = now
            else:
                return {
                    "line": line,
                    "line_label": folder_labels.get(line, line),
                    "shift": determine_shift(now.strftime("%H:%M:%S")),
                    "elapsed_time": 0,
                    "proportional_break_time": 0,
                    "adjusted_elapsed_time": 0,
                    "inactive_time": 0,
                    "adjusted_inactive_time": 0,
                    "operational_time": 0,
                    "good_pieces": 0,
                    "total_pieces": 0,
                    "availability": 0,
                    "performance": 0,
                    "quality": 0,
                    "oee": 0
                }
        elapsed_time = (now - start_time).total_seconds()
    else:
        elapsed_time = (now - last_reset_time).total_seconds()

    ideal_cycle_times = {
        "2098700356": 3600/165,
        "2098700316": 3600/150,
        "2098700154": 3600/165,
        "2098700083": 3600/165,
        "2154170050": 3600/75,
        "2154170052": 3600/72,
        "2154150582": 3600/165,
        "2154170049": 3600/72
    }

    if line not in pass_fail_counts:
        part_number = "N/A"
    else:
        part_number = pass_fail_counts[line].get("Reference", None)
        if not part_number or part_number == "N/A":
            part_number = pass_fail_counts[line].get("Test Name", None)
        if not part_number or part_number == "N/A":
            part_number = pass_fail_counts[line].get("Nombre de la prueba", "N/A")

    if line[0].isdigit():
        ideal_cycle_time = 3600 / 264
    elif part_number in ideal_cycle_times:
        ideal_cycle_time = ideal_cycle_times[part_number]
    else:
        ideal_cycle_time = 3600 / 340

    shift_durations = {"T1": 8*3600, "T2": 7.5*3600, "T3": 8.5*3600}
    current_time = now.strftime("%H:%M:%S")
    current_shift = determine_shift(current_time)
    current_shift_duration = shift_durations[current_shift]

    break_time = 2700
    proportional_break_time = (elapsed_time / current_shift_duration) * break_time
    adjusted_elapsed_time = elapsed_time
    line_label = folder_labels.get(line, line)

    inactive_time = inactivity_accumulated_poee.get(line, 0) if current_mode == "POEE" else inactivity_accumulated_oee.get(line, 0)
    adjusted_inactive_time = max(0, inactive_time - proportional_break_time)
    operational_time = max(0, adjusted_elapsed_time - adjusted_inactive_time)

    good_pieces = pass_fail_counts[line].get("Passed", 0)
    total_pieces = good_pieces + pass_fail_counts[line].get("Failed", 0)
    availability = (adjusted_elapsed_time - adjusted_inactive_time) / adjusted_elapsed_time if adjusted_elapsed_time > 0 else 0
    performance = (good_pieces * ideal_cycle_time) / operational_time if good_pieces > 0 and operational_time > 0 else 0
    quality = good_pieces / total_pieces if total_pieces > 0 else 0
    oee = availability * performance * quality

    return {
        "line": line,
        "line_label": line_label,
        "shift": current_shift,
        "elapsed_time": elapsed_time,
        "proportional_break_time": proportional_break_time,
        "adjusted_elapsed_time": adjusted_elapsed_time,
        "inactive_time": inactive_time,
        "adjusted_inactive_time": adjusted_inactive_time,
        "operational_time": operational_time,
        "good_pieces": good_pieces,
        "total_pieces": total_pieces,
        "availability": availability,
        "performance": performance,
        "quality": quality,
        "oee": oee
    }


def determine_shift(test_time):
    time_obj = datetime.datetime.strptime(test_time, "%H:%M:%S").time()
    t1_start, t1_end = datetime.time(6, 30), datetime.time(14, 30)
    t2_start, t2_end = datetime.time(14, 30), datetime.time(22, 0)
    if t1_start <= time_obj < t1_end:
        return "T1"
    elif t2_start <= time_obj < t2_end:
        return "T2"
    else:
        return "T3"


def calculate_cycle_time(line, timestamp):
    last_time = last_file_times[line]
    if last_time is not None:
        cycle_time = (timestamp - last_time).total_seconds()
        if cycle_time >= 0.05:
            cycle_times[line].append(cycle_time)
            if len(cycle_times[line]) > 50:
                cycle_times[line].pop(0)
        else:
            print(f"[WARN] Tiempo de ciclo descartado ({cycle_time:.2f}s)")
    last_file_times[line] = timestamp


def get_average_cycle_time(line, upper_percentile=96):
    times = cycle_times[line]
    if times:
        if len(times) == 1:
            return times[0]
        upper_bound = np.percentile(times, upper_percentile)
        filtered_times = [t for t in times if t <= upper_bound]
        return sum(filtered_times) / len(filtered_times) if filtered_times else None
    return None


def periodic_update(interval=30):
    while True:
        with counts_lock:
            emit_data()
        time.sleep(interval)


def monitor_directory(path):
    event_handler = NewFileHandler()
    observer = Observer()
    observer.schedule(event_handler, path, recursive=True)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


# ----------------------------------------
# Network monitoring
# ----------------------------------------
NETWORK_FOLDER = r"\\mlxgumvwfile01\Departamentos\Fakra\Pruebas\LogFiles"
CHECK_INTERVAL = 10
LOG_FILE = "network_log.txt"
WATCHDOG_INTERVAL = 1

def is_network_available():
    try:
        with os.scandir(NETWORK_FOLDER):
            return True
    except (OSError, FileNotFoundError):
        return False

def log_network_outage(start_time, end_time):
    duration_seconds = int((end_time - start_time).total_seconds())
    duration_formatted = str(timedelta(seconds=duration_seconds))
    log_entry = (
        f"Fecha de inicio: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Fecha de recuperación: {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Duración: {duration_seconds} segundos ({duration_formatted})\n"
        f"{'-'*60}\n"
    )
    with open(LOG_FILE, "a") as log_file:
        log_file.write(log_entry)
    print(f"[LOG] Caída de red registrada:\n{log_entry}")

def start_monitoring():
    outage_start_time = None
    observer = Observer(timeout=WATCHDOG_INTERVAL)
    event_handler = NewFileHandler()

    while True:
        if is_network_available():
            if outage_start_time:
                outage_end_time = datetime.datetime.now()
                log_network_outage(outage_start_time, outage_end_time)
                outage_start_time = None
            if not observer.is_alive():
                observer.schedule(event_handler, NETWORK_FOLDER, recursive=True)
                try:
                    observer.start()
                except RuntimeError:
                    pass
            time.sleep(CHECK_INTERVAL)
        else:
            if outage_start_time is None:
                outage_start_time = datetime.datetime.now()
                print(f"[WARNING] Red caída desde {outage_start_time.strftime('%H:%M:%S')}")
            if observer.is_alive():
                observer.stop()
                observer.join()
            time.sleep(CHECK_INTERVAL)


# ----------------------------------------
# Reset de métricas
# ----------------------------------------
def load_fail_discounts():
    global part_fail_discounts
    part_fail_discounts.clear()
    try:
        with open(r"\\mlxgumvwfile01\Departamentos\Fakra\Pruebas\Proyectos\HealthMonitor\QTYnegatives.csv", mode='r') as file:
            reader = csv.reader(file)
            for row in reader:
                if len(row) >= 2:
                    part = row[0].strip()
                    try:
                        qty = int(row[1].strip())
                        part_fail_discounts[part] = qty
                    except ValueError:
                        continue
        print(f"[✔️] Cargado QTYnegatives.csv: {part_fail_discounts}")
    except Exception as e:
        print(f"[ERROR] Falló la carga de QTYnegatives.csv: {e}")


def reset_all_values():
    global is_resetting
    is_resetting = True
    global pass_fail_counts, cycle_times, inactivity_start_time_oee, inactivity_start_time_poee
    global last_reset_time, last_file_times, flag_state, inactivity_state
    global inactivity_accumulated_oee, inactivity_accumulated_poee, poee_start_times

    load_fail_discounts()
    with counts_lock:
        pass_fail_counts.clear()
        cycle_times.clear()
        last_file_times.clear()
        flag_state.clear()
        poee_start_times.clear()
        inactivity_accumulated_oee.clear()
        inactivity_accumulated_poee.clear()
        inactivity_start_time_oee.clear()
        inactivity_start_time_poee.clear()
        inactivity_state.clear()
        last_reset_time = datetime.datetime.now()
        for line in folder_labels:
            current_part = "DEFAULT"
            activation_status[line] = {
                "active": False,
                "discount_pass": 2,
                "discount_fail": part_fail_discounts.get(current_part, 0),
                "current_part_number": current_part
            }
            activation_pass_counter[line] = 0
            poee_start_times[line] = None
            pass_fail_counts[line] = {"Passed": 0,"Failed": 0,"Reference": "","Test Name": "","Nombre de la prueba": ""}
            cycle_times[line] = []
            last_file_times[line] = None

    socketio.emit('update_data', [])
    socketio.emit('reset_data')
    is_resetting = False


def reset_line_state(line_name, new_part_number):
    activation_status[line_name] = {
        "active": False,
        "discount_pass": 2,
        "discount_fail": part_fail_discounts.get(new_part_number, 0),
        "current_part_number": new_part_number
    }
    activation_pass_counter[line_name] = 0
    poee_start_times.pop(line_name, None)
    inactivity_start_time_oee.pop(line_name, None)
    inactivity_start_time_poee.pop(line_name, None)
    inactivity_accumulated_oee[line_name] = 0
    inactivity_accumulated_poee[line_name] = 0
    inactivity_state[line_name] = False
    pass_fail_counts[line_name] = {"Passed": 0,"Failed": 0,"Reference": "","Test Name": "","Nombre de la prueba": ""}
    cycle_times[line_name] = []
    last_file_times[line_name] = None
    emit_data()

def capture_screenshot():
    try:
        chrome_options = Options()
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--window-size=1920,1080")
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_window_size(1920, 1080)
        driver.get("http://127.0.0.1:5000")
        driver.execute_script("document.body.style.zoom='80%'")
        time.sleep(4)
        emit_data()
        time.sleep(1)
        os.makedirs("screenshots", exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_filename = f"screenshots/dashboard_{timestamp}.png"
        driver.save_screenshot(screenshot_filename)
        driver.quit()
        print(f"[INFO] Captura de pantalla guardada en {screenshot_filename}")
    except Exception as e:
        print(f"[ERROR] Error al capturar pantalla: {e}")


def schedule_resets():
    while True:
        now = datetime.datetime.now()
        #reset_times = [datetime.time(6, 25), datetime.time(14, 25), datetime.time(21, 55)]
        reset_times = [datetime.time(6, 25), datetime.time(17, 1), datetime.time(21, 55)]
        next_reset = None
        for reset_time in reset_times:
            today_reset = datetime.datetime.combine(now.date(), reset_time)
            if now < today_reset:
                next_reset = today_reset
                break
        if next_reset is None:
            next_reset = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), reset_times[0])
        sleep_seconds = (next_reset - now).total_seconds()
        print(f"[INFO] Próximo reinicio programado a las {next_reset.strftime('%H:%M:%S')}")
        time.sleep(sleep_seconds)
        capture_screenshot()
        reset_all_values()


# ----------------------------------------
# Rutas Flask
# ----------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/set_mode/<mode>', methods=['POST'])
def set_mode(mode):
    global current_mode
    if mode.upper() in ["OEE", "POEE"]:
        current_mode = mode.upper()
        return jsonify({"status": "ok", "mode": current_mode})
    return jsonify({"status": "error"}), 400

@app.route("/historico")
def historico():
    return render_template("historico.html")

@app.route("/fetch_historico_data", methods=["POST"])
def fetch_historico_data_route():
    try:
        data = request.get_json()
        start_date, end_date, part_number = data.get("start_date"), data.get("end_date"), data.get("part_number")
        results = fetch_historico_data(start_date, end_date, part_number)
        return jsonify(results or [])
    except Exception as e:
        print(f"[ERROR] fetch_historico_data_route: {e}")
        return jsonify([])

@app.route("/download_csv")
def download_csv():
    try:
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        part_number = request.args.get("part_number")
        all_results = fetch_historico_data(start_date, end_date)
        filtered = [r for r in all_results if r.get("PartNumber") == part_number] if part_number else all_results
        si = io.StringIO()
        writer = csv.DictWriter(si, fieldnames=["SerialNumber","PartNumber","TestDate","TestTime","Shift","FALine","Tester","TestResult","Failure","LVResult","HVResult"])
        writer.writeheader()
        writer.writerows(filtered)
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=historico_resultados.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    except Exception as e:
        print(f"[ERROR] download_csv: {e}")
        return "Error generando CSV", 500

if __name__ == '__main__':
    load_fail_discounts()
    monitor_thread = threading.Thread(target=start_monitoring, daemon=True); monitor_thread.start()
    periodic_thread = threading.Thread(target=periodic_update, daemon=True); periodic_thread.start()
    reset_thread = threading.Thread(target=schedule_resets, daemon=True); reset_thread.start()
    socketio.run(app, host="0.0.0.0", port=5000, use_reloader=False)
