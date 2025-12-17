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
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
import shutil
import csv
from watchdog.events import PatternMatchingEventHandler


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
    "EOL3": "F10",
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
        ######new
        if not file_path.lower().endswith(".csv"):
            print(f"[SKIP] No-CSV detectado: {file_path}")
            return

        if is_resetting:
            print(f"[WAIT] Ignorando archivo durante reset: {event.src_path}")
            return
        ######new ends
        parent_folder = get_line_folder(file_path)
        print(f"[INFO] Nuevo archivo detectado: {file_path}")


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
                    failure_keywords = ["Failed", "*ERROR","NO","SHORTCIRCUIT", "*MISTAKE", "NO MEASUREMENT", ">"]
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
                                sn_label = folder_labels.get(parent_folder, "F1")
                                sn_label = "F0" if sn_label == "F10" else sn_label
                                serial_number = adjusted.strftime("%y%m%d%H%M%S") + sn_label

                            except Exception as e:
                                print(f"[ERROR] Tiempo inválido: {e}")

                        if line.startswith("Measured value") or line.startswith("#"):
                            current_table = "LVResult"
                            continue
                        if line.startswith("HiPotTest") or line.startswith("Name"):
                            current_table = "HVResult"
                            continue
                        if current_table == "LVResult" and line:
                            #LVResult += line + "*\n" #se retira el salto de línea
                            LVResult += line + "*"
                        elif current_table == "HVResult" and line:
                            #HVResult += line + "*\n" #se retira el salto de línea
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
                            elif "continuity" in l:
                                sFailure = "Nucleo"
                            elif "shortcircuit" in l:
                                sFailure = "Corto"
                            elif "resistance" in l:
                                sFailure = "Alta Resistencia"
                    print(f"🚨🚨🚨🚨🚨[DEBUG] status: {status}")
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
                    raw_label = folder_labels[parent_folder]   # aquí será F10 para EOL3
                    db_label = "F0" if raw_label == "F10" else raw_label
                    data_to_insert = {
                        "SerialNumber": serial_number,
                        "PartNumber": current_part,
                        "TestDate": datetime.datetime.strptime(test_date, "%m/%d/%Y").strftime("%Y-%m-%d") if test_date else None,
                        "TestTime": test_time,
                        "Shift": determine_shift(test_time),
                        "FALine": db_label,   # 👈 AQUÍ YA SE GUARDA F0
                        "Tester": parent_folder,
                        "TestResult": status,
                        "Failure": sFailure or "N/A",
                        "LVResult": LVResult,
                        "HVResult": HVResult
                    }
                    insert_test_result(data_to_insert)  # ← Descomenta cuando esté listo

                    with counts_lock:
                        pass_fail_counts[parent_folder]["Passed"] += real_passed
                        print(
                            f"[DEBUG] Resultado: {status}, Línea: {parent_folder}, "
                            f"Active: {activation_status[parent_folder]['active']}, "
                            f"Discount FAILs left: {activation_status[parent_folder]['discount_fail']}, "
                            f"Contado como Fail: {failed}"
                        )
                        pass_fail_counts[parent_folder]["Failed"] += failed
                        pass_fail_counts[parent_folder]["Reference"] = reference or "N/A"
                        pass_fail_counts[parent_folder]["Test Name"] = test_name or "N/A"
                        pass_fail_counts[parent_folder]["Nombre de la prueba"] = nombre_prueba or "N/A"
                    emit_data()
                    return
                except PermissionError:
                    time.sleep(1)
                except Exception as e:
                    print(f"[ERROR] Error al procesar el archivo {file_path}: {e}")
                    return
            else:
                time.sleep(0.5)
inactivity_start_time = {}
flag_state = defaultdict(lambda: False)
# NUEVAS VARIABLES GLOBALES
inactivity_start_time_oee = {}
inactivity_start_time_poee = {}
inactivity_state = defaultdict(lambda: False)  # True = inactivo

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
    global flag
    data = []
    now = datetime.datetime.now()

    for parent_folder, counts in pass_fail_counts.items():
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
                print(f"[INFO] Línea {line} aún no activada. No se cuenta tiempo.")
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
    # Imprimir tiempos para comparar
    #if current_mode == "POEE":
    #    print(f"[⏱️ POEE] Línea: {line} | Activada en: {start_time.strftime('%Y-%m-%d %H:%M:%S')} | Tiempo transcurrido: {elapsed_time:.2f}s")
    #else:
    #    print(f"[⏱️ OEE] Línea: {line} | Último reinicio: {last_reset_time.strftime('%Y-%m-%d %H:%M:%S')} | Tiempo transcurrido: {elapsed_time:.2f}s")

    # Definir tiempos de ciclo ideales por número de parte convertido a segundos
    ideal_cycle_times = {
        "2088702207": 3600/75,  # 75/hr
        "2098700356": 3600/165,  # 165/hr
        "2098700316": 3600/150,  # 150/hr
        "2098700154": 3600/165,  # 165/hr
        "2098700083": 3600/165,  # 165/hr
        "2154170050": 3600/75,   # 75/hr     cambio a 75 desde 55
        "2154170052": 3600/72,   # 72/hr   
        "2154150582": 3600/165,  # 165/hr    cambio a 165 desde 150
        "2154170049": 3600/72    # 72/hr
    }
    if line not in pass_fail_counts:
        print(f"[ERROR] Línea {line} no encontrada en pass_fail_counts. Usando N/A")
        part_number = "N/A"
    else:
        part_number = pass_fail_counts[line].get("Reference", None)
        if not part_number or part_number == "N/A":
            part_number = pass_fail_counts[line].get("Test Name", None)
        if not part_number or part_number == "N/A":
            part_number = pass_fail_counts[line].get("Nombre de la prueba", "N/A")
    # Asignar tiempo de ciclo ideal basado en el número de parte (10 seg por defecto)
    # Asignar tiempo de ciclo ideal basado en línea / número de parte
    line_label = folder_labels.get(line, line)

    # Caso especial: F1 (EOL1) corre a 264 pzs/hr
    if line_label == "F1" or line == "EOL1":
        ideal_cycle_time = 3600 / 264

    elif part_number in ideal_cycle_times:
        ideal_cycle_time = ideal_cycle_times[part_number]

    else:
        ideal_cycle_time = 3600 / 340

    shift_durations = {
        "T1": 8 * 3600,  # 8 horas en segundos
        "T2": 7.5 * 3600,  # 7.5 horas en segundos
        "T3": 8.5 * 3600   # 8.5 horas en segundos
    }
    current_time = now.strftime("%H:%M:%S")
    current_shift = determine_shift(current_time)
    current_shift_duration = shift_durations[current_shift]
    break_time = 2700  # 45 minutos (30 min comedor + 10 min de break + 5 min ejercicios)
    proportional_break_time = (elapsed_time / current_shift_duration) * break_time
    adjusted_elapsed_time = elapsed_time
    #line_label = folder_labels.get(line, line) duplicado arriba
    if current_mode == "POEE":
        inactive_time = inactivity_accumulated_poee.get(line, 0)
    else:
        inactive_time = inactivity_accumulated_oee.get(line, 0)
    adjusted_inactive_time = max(0, inactive_time - proportional_break_time)
    operational_time = max(0, adjusted_elapsed_time - adjusted_inactive_time)
    good_pieces = pass_fail_counts[line].get("Passed", 0)
    total_pieces = good_pieces + pass_fail_counts[line].get("Failed", 0)
    availability = (adjusted_elapsed_time - adjusted_inactive_time) / adjusted_elapsed_time if adjusted_elapsed_time > 0 else 0
    performance = (good_pieces * ideal_cycle_time) / operational_time if good_pieces > 0 and operational_time > 0 else 0
    quality = good_pieces / total_pieces if total_pieces > 0 else 0
    oee = availability * performance * quality
    print(f"[INFO] Línea: {line} (Label: {line_label})")
    print(f"[INFO] Número de parte detectado: {part_number}")
    #print(f"[INFO] Tiempo de ciclo ideal aplicado: {ideal_cycle_time} segundos")
    #print(f"[INFO] Tiempo Total Transcurrido: {elapsed_time:.2f}s")
    #print(f"[INFO] Tiempo de Comedor Proporcional: {proportional_break_time:.2f}s")
    #print(f"[INFO] Tiempo Ajustado: {adjusted_elapsed_time:.2f}s")
    #print(f"[INFO] Tiempo Inactivo Acumulado: {inactive_time:.2f}s")
    #print(f"[INFO] Tiempo Inactivo Ajustado: {adjusted_inactive_time:.2f}s")
    #print(f"[INFO] Tiempo Operativo: {operational_time:.2f}s")
    #print(f"[INFO] Piezas Buenas: {good_pieces}")
    #print(f"[INFO] Piezas Totales: {total_pieces}")
    #print(f"[INFO] Disponibilidad (Availability): {availability:.2%}")
    #print(f"[INFO] Rendimiento (Performance): {performance:.2%}")
    #print(f"[INFO] Calidad (Quality): {quality:.2%}")
    #print(f"[INFO] OEE: {oee:.2%}")
    print(f"********************************************************************************************************************")

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
    t1_start = datetime.time(6, 30)
    t1_end = datetime.time(14, 30)
    t2_start = datetime.time(14, 30)
    t2_end = datetime.time(22, 0)
    t3_start = datetime.time(22, 0)
    t3_end = datetime.time(6, 30)
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
        if cycle_time >= 0.05:  # Cambiado a 0.05 segundos
            cycle_times[line].append(cycle_time)
            print(f"[INFO] Tiempo de ciclo calculado para la línea {line}: {cycle_time:.2f}s")
            if len(cycle_times[line]) > 50:  #aqui se ajusta la cantidad de tiempos a promediar
                cycle_times[line].pop(0)
        else:
            print(f"[WARN] Tiempo de ciclo descartado para la línea {line} ({cycle_time:.2f}s). Menor a 5 segundos.")
    else:
        print(f"[INFO] No hay datos previos para la línea {line}. Esperando más datos.")
    last_file_times[line] = timestamp

def get_average_cycle_time(line, upper_percentile=96):
    times = cycle_times[line]
    if times:
        if len(times) == 1:
            return times[0]
        upper_bound = np.percentile(times, upper_percentile)
        filtered_times = [t for t in times if t <= upper_bound]

        if len(filtered_times) > 0:
            return sum(filtered_times) / len(filtered_times)
        else:
            return None 

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
#####################################################################new functions
NETWORK_FOLDER = r"\\mlxgumvwfile01\Departamentos\Fakra\Pruebas\LogFiles"
CHECK_INTERVAL = 10  # Segundos entre verificaciones
LOG_FILE = "network_log.txt"  # Archivo donde se guardarán las caídas de red
WATCHDOG_INTERVAL = 1  # 🔹 Se reduce el tiempo entre eventos de Watchdog

def is_network_available():
    """Verifica si la carpeta de red está accesible sin bloquear el sistema."""
    try:
        with os.scandir(NETWORK_FOLDER):  # Accede más rápido que os.path.exists()
            return True
    except (OSError, FileNotFoundError):
        return False

def log_network_outage(start_time, end_time):
    """Registra la caída de red en un archivo con duración detallada sin decimales."""
    duration_seconds = int((end_time - start_time).total_seconds())
    duration_formatted = str(timedelta(seconds=duration_seconds))  

    log_entry = (
        f"Fecha de inicio: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Fecha de recuperación: {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Duración de la caída: {duration_seconds} segundos ({duration_formatted})\n"
        f"{'-'*60}\n"
    )
    
    with open(LOG_FILE, "a") as log_file:
        log_file.write(log_entry)

    print(f"[LOG] Caída de red registrada:\n{log_entry}")

def start_monitoring():
    """Monitorea la carpeta de red y reacciona más rápido ante caídas y reconexiones."""
    outage_start_time = None  
    observer = Observer(timeout=WATCHDOG_INTERVAL)  # Instancia de Watchdog
    event_handler = NewFileHandler()

    while True:
        if is_network_available():
            if outage_start_time:
                outage_end_time = datetime.datetime.now()
                log_network_outage(outage_start_time, outage_end_time)
                outage_start_time = None  # Reseteamos la variable

            print("[INFO] Red detectada. Iniciando monitoreo de archivos...")
            
            # 🔹 Evita iniciar múltiples instancias de Watchdog
            if not observer.is_alive():
                observer.schedule(event_handler, NETWORK_FOLDER, recursive=True)
                try:
                    observer.start()  # 🔹 Iniciar solo si no está en ejecución
                except RuntimeError:
                    print("[ERROR] Watchdog ya estaba iniciado. Omitiendo.")

            time.sleep(CHECK_INTERVAL)  # 🔹 Revisar la red cada 2 segundos para evitar bloqueos
        else:
            if outage_start_time is None:
                outage_start_time = datetime.datetime.now()
                print(f"[WARNING] Red caída desde {outage_start_time.strftime('%Y-%m-%d %H:%M:%S')}")

            if observer.is_alive():
                observer.stop()  # 🔹 Detiene el observador si la red se cae
                observer.join()  # 🔹 Esperar a que el hilo se cierre antes de continuar
                print("[INFO] Watchdog detenido debido a la caída de la red.")

            print("[WARNING] No se puede acceder a la red. Esperando reconexión...")
            time.sleep(CHECK_INTERVAL)  # 🔹 Revisar más frecuentemente si la red vuelve

##############################################################################################

def reset_all_values():
    global is_resetting
    is_resetting = True  # 🛑 Bloquear archivos mientras se limpia todo

    global pass_fail_counts, cycle_times, inactivity_start_time_oee, inactivity_start_time_poee
    global last_reset_time, last_file_times, flag_state, inactivity_state
    global inactivity_accumulated_oee, inactivity_accumulated_poee, poee_start_times

    load_fail_discounts()  # Cargar descuentos desde CSV

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
            pass_fail_counts[line] = {
                "Passed": 0,
                "Failed": 0,
                "Reference": "",
                "Test Name": "",
                "Nombre de la prueba": ""
            }
            cycle_times[line] = []
            last_file_times[line] = None
            print(f"[🔁 RESET STATUS] {line} - Descuentos: Pass=2, Fail={part_fail_discounts.get(current_part, 0)}")

    print("[INFO] ¡Se han reseteado todas las métricas y valores acumulados!")

    # 🔄 Emitir data limpia al frontend
    zero_data = []
    for line_label in folder_labels.values():
        zero_data.append({
            "label": line_label,
            "yield": 0,
            "total_tests": 0,
            "passed": 0,
            "failed": 0,
            "reference": "N/A",
            "test_name": "N/A",
            "nombre_prueba": "N/A",
            "avg_cycle_time": 0,
            "state": 0,
            "availability": 0,
            "operational_time": 0,
            "performance": 0,
            "quality": 0,
            "oee": 0,
            "inactive_time": 0,
            "elapsed_time": 0,
        })

    socketio.emit('update_data', zero_data)
    socketio.emit('reset_data')
    is_resetting = False  # ✅ Liberar el lock

    print("[INFO] Datos de la interfaz reiniciados y gráficos restablecidos.")

def capture_screenshot():
    try:
        chrome_options = Options()
        chrome_options.add_argument("--headless")  # Modo sin interfaz gráfica
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        #chrome_options.add_argument("--start-maximized")
        #chrome_options.add_argument("--force-device-scale-factor=0.8")
        chrome_options.add_argument("--window-size=1920,1080")  # Tamaño de la ventana

        driver = webdriver.Chrome(options=chrome_options)
        driver.set_window_size(1920, 1080)
        dashboard_url = "http://127.0.0.1:5000"  # Asegúrate de usar la URL correcta
        driver.get(dashboard_url)
        driver.execute_script("document.body.style.zoom='80%'")
        #driver.set_window_size(1920, 1080)  # Configurar tamaño de ventana Full HD
        # Esperar unos segundos para cargar la página completamente
        time.sleep(4)
        emit_data()
        time.sleep(1)
        # Crear carpeta de capturas si no existe
        os.makedirs("screenshots", exist_ok=True)

        # Guardar la captura de pantalla con marca de tiempo
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_filename = f"screenshots/dashboard_{timestamp}.png"
        driver.save_screenshot(screenshot_filename)
        driver.quit()

        print(f"[INFO] Captura de pantalla guardada en {screenshot_filename}")
    except Exception as e:
        print(f"[ERROR] Error al capturar la pantalla: {e}")

def schedule_resets():
    while True:
        now = datetime.datetime.now()
        reset_times = [
            datetime.time(6, 25),   # Reinicio a las 6:30 AM
            datetime.time(14, 25),  # Reinicio a las 2w:30 PM
            datetime.time(21, 55),   # Reinicio a las 10:00 PM
        ]

        next_reset = None

        # Encontrar el próximo reinicio en el mismo día
        for reset_time in reset_times:
            today_reset = datetime.datetime.combine(now.date(), reset_time)
            if now < today_reset:
                next_reset = today_reset
                break
        if next_reset is None:
            next_reset = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), reset_times[0])

        sleep_seconds = (next_reset - now).total_seconds()
        print(f"[INFO] Próximo reinicio programado a las {next_reset.strftime('%Y-%m-%d %H:%M:%S')}")

        time.sleep(sleep_seconds)  # Esperar hasta el momento del reinicio
        print("[INFO] Capturando la pantalla antes del reinicio...")

        
        capture_screenshot()  # Capturar la pantalla antes del reinicio
        reset_all_values()


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
    pass_fail_counts[line_name] = {
        "Passed": 0,
        "Failed": 0,
        "Reference": "",
        "Test Name": "",
        "Nombre de la prueba": ""
    }
    cycle_times[line_name] = []
    last_file_times[line_name] = None
    print(f"[🧼 RESET TOTAL] Línea {line_name} reiniciada por cambio de parte: {new_part_number}")
    emit_data()  # 🔁 Emitir actualización inmediata al frontend



@app.route('/')
def index():
    return render_template('index.html')
@app.route('/set_mode/<mode>', methods=['POST'])
def set_mode(mode):
    global current_mode
    if mode.upper() in ["OEE", "POEE"]:
        current_mode = mode.upper()
        print(f"[MODE] Modo de cálculo actualizado a: {current_mode}")
        return jsonify({"status": "ok", "mode": current_mode})
    return jsonify({"status": "error", "message": "Modo inválido"}), 400
########################################################################################new functions
from flask import send_file, make_response
import io
import csv

@app.route("/historico")
def historico():
    return render_template("historico.html")


@app.route("/fetch_historico_data", methods=["POST"])
def fetch_historico_data_route():
    try:
        data = request.get_json()
        start_date = data.get("start_date")
        end_date = data.get("end_date")
        part_number = data.get("part_number")

        results = fetch_historico_data(start_date, end_date, part_number)

        return jsonify(results or [])
    except Exception as e:
        print(f"[ERROR] fetch_historico_data_route: {e}")
        return jsonify([])

@app.route("/download_csv")
def download_csv():
    try:
        start_date = request.args.get("start_date")
        end_date   = request.args.get("end_date")
        part_number = request.args.get("part_number") or None

        # Usa la misma consulta (con fecha+hora y PN)
        filtered = fetch_historico_data(start_date, end_date, part_number)

        si = io.StringIO()
        writer = csv.DictWriter(si, fieldnames=[
            "SerialNumber", "PartNumber", "TestDate", "TestTime", "Shift", "FALine",
            "Tester", "TestResult", "Failure", "LVResult", "HVResult"
        ])
        writer.writeheader()
        writer.writerows(filtered)

        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=historico_resultados.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    except Exception as e:
        print(f"[ERROR] download_csv: {e}")
        return "Error generando CSV", 500

#####################################################################new ends

if __name__ == '__main__':
    load_fail_discounts()
    monitor_thread = threading.Thread(target=start_monitoring, daemon=True)
    monitor_thread.start()
    periodic_thread = threading.Thread(target=periodic_update, daemon=True)##hilo de actualizacion
    periodic_thread.start()
    reset_thread = threading.Thread(target=schedule_resets, daemon=True)##hilo de reseteo programado
    reset_thread.start()
    socketio.run(app, host="0.0.0.0", port=5000, use_reloader=False)
