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
import shutil
import csv

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
    "P2": "F3",
    "P3": "F2",
    "EOL5": "F4",
    "EOL2": "F5",
    "EOL4": "F6",
    "EOL6": "F7",
    "EOL7": "F8"
}
counts_lock = threading.Lock()
cycle_times = defaultdict(list)
last_file_times = defaultdict(lambda: None)

class NewFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        file_path = event.src_path
        parent_folder = os.path.basename(os.path.dirname(file_path))
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
                            LVResult += line + "*\n"
                        elif current_table == "HVResult" and line:
                            HVResult += line + "*\n"
                    if status == "Fail":
                        failure_line = next((l for l in LVResult.splitlines() if any(k in l for k in failure_keywords)), None)
                        if not failure_line:
                            failure_line = next((l for l in HVResult.splitlines() if any(k in l for k in failure_keywords)), None)
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
                    current_part = reference or test_name or nombre_prueba
                    prev_part = activation_status[parent_folder]["current_part_number"]
                    if current_part and prev_part != current_part:
                        activation_status[parent_folder].update({
                            "active": False,
                            "discount_pass": 2,
                            "discount_fail": part_fail_discounts.get(current_part, 0),
                            "current_part_number": current_part
                        })
                        activation_pass_counter[parent_folder] = 0
                        print(f"[🔁 RESET DESCUENTOS] Nueva parte en {parent_folder}: {current_part}")
                    real_passed = 0
                    real_failed = 0
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
                            else:
                                real_failed = 1
                        if activation_pass_counter[parent_folder] == 4:
                            pass_fail_counts[parent_folder]["Passed"] += 4  # ← Agrega los 4 válidos
                            activation_status[parent_folder]["active"] = True
                            poee_start_times[parent_folder] = timestamp - datetime.timedelta(seconds=60)
                            print(f"[🟢 ACTIVADA] Línea {parent_folder} ACTIVADA tras 6 Passed (2 skip + 4 válidos)")
                        if not activation_status[parent_folder]["active"]:
                            print(f"[⛔ NO ACTIVA] {parent_folder}, esperando más Passed/Fail")
                            return
                    else:
                        if status == "Pass":
                            real_passed = 1
                        elif status == "Fail":
                            real_failed = 1
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
                    insert_test_result(data_to_insert)
                    print("\n################################################################################################")
                    with counts_lock:
                        pass_fail_counts[parent_folder]["Passed"] += real_passed
                        pass_fail_counts[parent_folder]["Failed"] += real_failed
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
inactivity_start_time_oee = {}
inactivity_start_time_poee = {}
inactivity_state = defaultdict(lambda: False)  # True = inactivo

def emit_data():
    global flag
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

        # 🧠 Determinar si la línea está inactiva
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

        # 💡 Siempre evaluamos OEE
        if current_state == 0:
            # OEE: Acumular inactividad siempre
            if not inactivity_state[parent_folder]:
                inactivity_start_time_oee[parent_folder] = now
                print(f"[⏱️ OEE] INICIO inactividad en {parent_folder} a {now.strftime('%H:%M:%S')}")
            else:
                elapsed_oee = (now - inactivity_start_time_oee[parent_folder]).total_seconds()
                inactivity_accumulated_oee[parent_folder] += elapsed_oee
                inactivity_start_time_oee[parent_folder] = now
                print(f"[➕ OEE] +{elapsed_oee:.2f}s acumulados en {parent_folder}")

            # POEE: Solo si ya hubo producción (tiene poee_start_time)
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
        start_time = poee_start_times.get(line, now)
        elapsed_time = (now - start_time).total_seconds()
    else:
        elapsed_time = (now - last_reset_time).total_seconds()
    if current_mode == "POEE":
        print(f"[⏱️ POEE] Línea: {line} | Activada en: {start_time.strftime('%Y-%m-%d %H:%M:%S')} | Tiempo transcurrido: {elapsed_time:.2f}s")
    else:
        print(f"[⏱️ OEE] Línea: {line} | Último reinicio: {last_reset_time.strftime('%Y-%m-%d %H:%M:%S')} | Tiempo transcurrido: {elapsed_time:.2f}s")
    ideal_cycle_times = {
        "2098700356": 3600/165,  # 165/hr
        "2098700316": 3600/150,  # 150/hr
        "2098700154": 3600/165,  # 165/hr
        "2098700083": 3600/165,  # 165/hr
        "2154170050": 3600/55,  # 55/hr
        "2154170052": 3600/72,  # 72/hr
        "2154150582": 3600/150,   # 150/hr
        "2154170049": 3600/72  # 72/hr
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
    if line[0].isdigit():
        ideal_cycle_time = 3600 / 264  # Si la línea comienza con un número, usar 3600/264
        print(f"[VALIDACIÓN] Línea {line} comienza con un número. Asignando ideal_cycle_time: {ideal_cycle_time:.2f} segundos")
    elif part_number in ideal_cycle_times:
        ideal_cycle_time = ideal_cycle_times[part_number]  # Usar el valor del diccionario si el número de parte está definido
        print(f"[VALIDACIÓN] Número de parte detectado: {part_number} - Asignando ideal_cycle_time: {ideal_cycle_time:.2f} segundos")
    else:
        ideal_cycle_time = 3600 / 340  # Valor por defecto si no se cumplen las otras reglas
        print(f"[VALIDACIÓN] Número de parte no encontrado en la lista. Usando valor por defecto: {ideal_cycle_time:.2f} segundos")
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
    #adjusted_elapsed_time = max(0, elapsed_time - proportional_break_time) #se intercambia por que ya se esta descontando el tiempo proporcional de breaks en inactividad.
    adjusted_elapsed_time = elapsed_time
    line_label = folder_labels.get(line, line)
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
    print(f"[INFO] Tiempo de ciclo ideal aplicado: {ideal_cycle_time} segundos")
    print(f"[INFO] Tiempo Total Transcurrido: {elapsed_time:.2f}s")
    print(f"[INFO] Tiempo de Comedor Proporcional: {proportional_break_time:.2f}s")
    print(f"[INFO] Tiempo Ajustado: {adjusted_elapsed_time:.2f}s")
    print(f"[INFO] Tiempo Inactivo Acumulado: {inactive_time:.2f}s")
    print(f"[INFO] Tiempo Inactivo Ajustado: {adjusted_inactive_time:.2f}s")
    print(f"[INFO] Tiempo Operativo: {operational_time:.2f}s")
    print(f"[INFO] Piezas Buenas: {good_pieces}")
    print(f"[INFO] Piezas Totales: {total_pieces}")
    print(f"[INFO] Disponibilidad (Availability): {availability:.2%}")
    print(f"[INFO] Rendimiento (Performance): {performance:.2%}")
    print(f"[INFO] Calidad (Quality): {quality:.2%}")
    print(f"[INFO] OEE: {oee:.2%}")
    print(f"*******************************************")
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
        if cycle_time >= 5.0:  
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
            return times[0]  # Devuelve el primer tiempo registrado en lugar de N/A
        upper_bound = np.percentile(times, upper_percentile)
        filtered_times = [t for t in times if t <= upper_bound]
        if len(filtered_times) > 0:
            return sum(filtered_times) / len(filtered_times)  # Calcular promedio sin valores altos
        else:
            return None  # Si todos fueron filtrados como outliers

    return None  # Si no hay registros, devuelve None

def periodic_update(interval=30):
    """
    Función para emitir datos cada 'interval' segundos.
    """
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
            time.sleep(CHECK_INTERVAL)

def reset_all_values():
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
    print("[INFO] ¡Se han reseteado todas las métricas y valores acumulados!")
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
    print("[INFO] Datos de la interfaz reiniciados y gráficos restablecidos.")

def capture_screenshot():
    try:
        chrome_options = Options()
        chrome_options.add_argument("--headless")  # Modo sin interfaz gráfica
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--start-maximized")
        #chrome_options.add_argument("--force-device-scale-factor=0.8")
        chrome_options.add_argument("--window-size=1920,1080")  # Tamaño de la ventana
        driver = webdriver.Chrome(options=chrome_options)
        dashboard_url = "http://127.0.0.1:5000"  # Asegúrate de usar la URL correcta
        driver.get(dashboard_url)
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
        print(f"[ERROR] Error al capturar la pantalla: {e}")

def schedule_resets():
    while True:
        now = datetime.datetime.now()
        reset_times = [
            datetime.time(6, 25),   # Reinicio a las 6:25 AM
            datetime.time(14, 25),  # Reinicio a las 2:25 PM
            datetime.time(21, 55),   # Reinicio a las 9:55 PM
        ]
        next_reset = None
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

SHAREPOINT_USER = ""
SHAREPOINT_PASS = ""

def login_to_sharepoint(driver, wait):
    try:
        # Paso 1: Usuario
        email_input = wait.until(EC.presence_of_element_located((By.NAME, "loginfmt")))
        email_input.send_keys(SHAREPOINT_USER)
        driver.find_element(By.ID, "idSIButton9").click()
        print("[✅] Usuario ingresado")

        # Paso 2: Contraseña
        password_input = wait.until(EC.presence_of_element_located((By.NAME, "passwd")))
        password_input.send_keys(SHAREPOINT_PASS)
        driver.find_element(By.ID, "idSIButton9").click()
        print("[✅] Contraseña ingresada")
        try:
            stay_signed_in = wait.until(EC.presence_of_element_located((By.ID, "idSIButton9")))
            stay_signed_in.click()
            print("[🔐] Mantener sesión activado")
        except:
            print("[ℹ️] Botón de mantener sesión no apareció (puede estar cacheado)")
    except Exception as e:
        print(f"[❌] Error durante el login: {e}")

def capture_scorecard():
    try:
        chrome_options = Options()
        # ✅ Mostrar navegador para login manual
        chrome_options.add_argument("--start-maximized")
        chrome_options.add_argument("--headless")  # ← Actívalo luego del primer login

        # ✅ Usar perfil persistente para mantener sesión iniciada
        profile_path = os.path.abspath("chrome_profile")
        chrome_options.add_argument(f"--user-data-dir={profile_path}")

        driver = webdriver.Chrome(options=chrome_options)
        wait = WebDriverWait(driver, 15)

        sharepoint_url = (
            "https://kochind-my.sharepoint.com/:x:/r/personal/jose_cervantes_molex_com/"
            "Documents/Dashboard%20HFM/ScoreCard%20Template%20(1).xlsx"
            "?d=wcead5b5fd9684a6cb7510b71d445c3d7&csf=1&web=1&e=thwvE9&nav=MTVfezlCMUMxODU4LTBGRTYtNDk3My1CMzNDLUQ0MUQxMjQxQzE4RX0"
        )

        print("[🔗] Abriendo SharePoint...")
        driver.get(sharepoint_url)

        # Espera tiempo para login manual la primera vez
        print("[⏳] Tienes 60 segundos para loguearte si es necesario...")
        time.sleep(5)####aqui cambiar el tiempo para loggeo manual

        # ✅ Ocultar el ribbon y headers de la UI
        print("[🧼] Ocultando ribbon, headers y pie de página...")
        driver.execute_script("""
            let ribbon = document.querySelector('[role="toolbar"]');
            if (ribbon) ribbon.style.display = "none";

            let header = document.querySelector('[data-automationid="OfficeHeader"]');
            if (header) header.style.display = "none";

            let footer = document.querySelector('[data-automationid="Footer"]');
            if (footer) footer.style.display = "none";
        """)

        # ✅ Zoom y scroll para vista clara
        driver.execute_script("document.body.style.zoom='130%'")
        driver.execute_script("window.scrollTo(0, 0);")
        os.makedirs("static/images", exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = f"static/images/scorecard_{timestamp}.png"
        driver.save_screenshot(screenshot_path)
        shutil.copyfile(screenshot_path, "static/images/scorecard.png")
        for fname in os.listdir("static/images"):
            if fname.startswith("scorecard_") and fname.endswith(".png") and fname != os.path.basename(screenshot_path):
                try:
                    os.remove(os.path.join("static/images", fname))
                    print(f"[🗑️] Eliminada: {fname}")
                except Exception as e:
                    print(f"[⚠️] No se pudo eliminar {fname}: {e}")
        driver.quit()
        print(f"[📸] Captura de Scorecard guardada en: {screenshot_path}")
    except Exception as e:
        print(f"[❌ ERROR] Falló la captura automática: {e}")

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

# Enlace público al Excel
SHAREPOINT_EXCEL_URL = "https://kochind-my.sharepoint.com/:x:/g/personal/jose_cervantes_molex_com/EV9brc5o2WxKt1ELcdRFw9cB9-Fe12M_LuK4d7tF7DScjA?email=functional.test%40molex.com&e=xOjsMh"

if __name__ == '__main__':
    #path_to_monitor = r"\\mlxgumvwfile01\Departamentos\Fakra\Pruebas\LogFiles"
    load_fail_discounts()
    monitor_thread = threading.Thread(target=start_monitoring, daemon=True)
    monitor_thread.start()
    periodic_thread = threading.Thread(target=periodic_update, daemon=True)##hilo de actualizacion
    periodic_thread.start()
    reset_thread = threading.Thread(target=schedule_resets, daemon=True)##hilo de reseteo programado
    reset_thread.start()
    def schedule_scorecard_updates(interval=600):
        while True:
            capture_scorecard()
            time.sleep(interval)
    scorecard_thread = threading.Thread(target=schedule_scorecard_updates, daemon=True)
    scorecard_thread.start()
    socketio.run(app, host="0.0.0.0", port=5000, use_reloader=False)
