import os
import time
import re
import datetime
from flask import request, jsonify
from flask import Flask, render_template
from flask_socketio import SocketIO
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from collections import defaultdict
import threading
from threading import Timer
from database_operations import insert_test_result
from database_operations import fetch_historico_data
from database_operations import insert_kpi

app = Flask(__name__)
socketio = SocketIO(app)
last_reset_time = datetime.datetime.now()  # Inicialización al inicio de la app
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
pulse_history_data = {
    "pulseHistoryX": defaultdict(list),
    "pulseHistoryY": defaultdict(list),
    "inactiveTimeByLine": defaultdict(int),
    "lastUpdateByLine": defaultdict(lambda: 0)
}

def determine_shift(test_time):
    # Convertir el tiempo a objeto datetime
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
            if len(cycle_times[line]) > 25:  #aqui se ajusta la cantidad de tiempos a promediar
                cycle_times[line].pop(0)
        else:
            print(f"[WARN] Tiempo de ciclo descartado para la línea {line} ({cycle_time:.2f}s). Menor a 5 segundos.")
    else:
        print(f"[INFO] No hay datos previos para la línea {line}. Esperando más datos.")
    last_file_times[line] = timestamp

def get_average_cycle_time(line):
    times = cycle_times[line]
    if times:
        avg_time = sum(times) / len(times)
        return avg_time
    print(f"[INFO] No hay datos suficientes para calcular el promedio de la línea {line}.")
    return None

def calculate_oee(line, ideal_cycle_time=10):

    global last_reset_time
    elapsed_time = (datetime.datetime.now() - last_reset_time).total_seconds()
    shift_durations = {
        "T1": 8 * 3600,    # 8 horas en segundos
        "T2": 7.5 * 3600,  # 7.5 horas en segundos
        "T3": 8.5 * 3600   # 8.5 horas en segundos
    }
    current_time = datetime.datetime.now().strftime("%H:%M:%S")
    current_shift = determine_shift(current_time) 
    current_shift_duration = shift_durations[current_shift]
    break_time = 2100                                           #30 mins de comedor y 5 de ejercicios
    proportional_break_time = (elapsed_time / current_shift_duration) * break_time
    adjusted_elapsed_time = max(0, elapsed_time - proportional_break_time)
    line_label = folder_labels.get(line, line)
    inactive_time = pulse_history_data["inactiveTimeByLine"].get(line_label, 0)
    operational_time = max(0, adjusted_elapsed_time - inactive_time)
    good_pieces = pass_fail_counts[line].get("Passed", 0)
    total_pieces = pass_fail_counts[line].get("Passed", 0) + pass_fail_counts[line].get("Failed", 0)
    if adjusted_elapsed_time > 0:
        availability = (adjusted_elapsed_time - inactive_time) / adjusted_elapsed_time
    else:
        availability = 0
    if good_pieces == 0 or operational_time == 0:
        performance = 0
    else:
        performance = (good_pieces * ideal_cycle_time) / operational_time
    if total_pieces > 0:
        quality = good_pieces / total_pieces
    else:
        quality = 0
    oee = availability * performance * quality

    print(f"[INFO] Línea: {line} (Label: {line_label})")
    #print(f"[INFO] Turno Actual: {current_shift}")
    print(f"[INFO] Tiempo Total Transcurrido: {elapsed_time:.2f}s")
    #print(f"[INFO] Tiempo de Comedor Proporcional: {proportional_break_time:.2f}s")
    #print(f"[INFO] Tiempo Ajustado: {adjusted_elapsed_time:.2f}s")
    print(f"[INFO] Producción Real: {good_pieces}")
    print(f"[INFO] Tiempo Inactivo: {inactive_time:.2f}s")
    print(f"[INFO] Tiempo Operativo: {operational_time:.2f}s")
    print(f"[INFO] Performance: {performance:.2%}")
    #print(f"[INFO] Piezas Totales: {total_pieces}")
    print(f"[INFO] Availability: {availability:.2%}")
    print(f"[INFO] Yield: {quality:.2%}")
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
        "operational_time": operational_time,
        "good_pieces": good_pieces,
        "total_pieces": total_pieces,
        "availability": availability,
        "performance": performance,
        "quality": quality,
        "oee": oee
    }


class NewFileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return  
        file_path = event.src_path
        parent_folder = os.path.basename(os.path.dirname(file_path))
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
                    LVResult = ""
                    HVResult = ""
                    failure_line = None
                    sFailure = None
                    failure_keywords = ["Failed", "*ERROR", "*MISTAKE", "NO MEASUREMENT", ">"]
                    current_table = None
                    for line in lines:
                        line = line.strip()  
                        if any(keyword in line for keyword in ["Status:", "Final Test Result:", "Resultado final de la prueba:"]):
                            if any(success in line for success in ["Passed", "*PASS", "Pasa"]):
                                passed += 1
                                status = "Pass"
                                print("Status: " + status)
                            elif any(fail in line for fail in ["Failed", "*FAIL", "Falla"]):
                                failed += 1
                                status = "Fail"
                                print("Status: " + status)

                        if "Reference:" in line:
                            reference = re.sub(r'^[*,\s]+', '', line.split("Reference:")[-1].strip())
                            print("PN: " + reference)
                        elif "Nombre de la prueba:" in line:
                            test_name = re.sub(r'^[*,\s]+', '', line.split("Nombre de la prueba:")[-1].strip())
                            print("PN: " + test_name)
                        elif "Test Name:" in line:
                            nombre_prueba = re.sub(r'^[*,\s]+', '', line.split("Test Name:")[-1].strip())
                            print("PN: " + nombre_prueba)

                        if "Serial number:" in line and not line.startswith("EOL Serial number:"):
                            serial_number = re.sub(r'^[*,\s]+', '', line.split("Serial number:")[-1].strip())
                            print("Serial Number: " + serial_number)

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

                                print(f"Test Date: {test_date}")
                                print(f"Test Time: {test_time}")
                            else:
                                print("[WARNING] El Serial Number no tiene el formato esperado para extraer Test Date y Test Time.")
                        elif "Test Date:" in line:
                            test_date_raw = re.sub(r'^[*,\s]+', '', line.split("Test Date:")[-1].strip())
                            test_date = re.sub(r'^[*,\s]+', '', line.split("Test Date:")[-1].strip())
                            print("Test Date: " + test_date)
                        elif "Test Time:" in line:
                            test_time_raw = re.sub(r'^[*,\s]+', '', line.split("Test Time:")[-1].strip())
                            try:
                                test_time = datetime.datetime.strptime(test_time_raw, "%I:%M:%S %p").strftime("%H:%M:%S")
                                print("Test Time: " + test_time)
                                try:
                                    date_time_obj = datetime.datetime.strptime(f"{test_date} {test_time}", "%m/%d/%Y %H:%M:%S")
                                    adjusted_time = date_time_obj - datetime.timedelta(seconds=2)
                                    year = adjusted_time.strftime("%y")
                                    month = adjusted_time.strftime("%m")
                                    day = adjusted_time.strftime("%d")
                                    hour = adjusted_time.strftime("%H")
                                    minute = adjusted_time.strftime("%M")
                                    second = adjusted_time.strftime("%S")
                                    suffix = folder_labels.get(parent_folder, "F1")  # Valor por defecto "F1"
                                    serial_number = f"{year}{month}{day}{hour}{minute}{second}{suffix}"
                                    print("Serial Number:", serial_number)
                                except ValueError as e:
                                    print(f"[ERROR] Error al generar el Serial Number: {e}")
                            except ValueError:
                                print(f"[WARNING] El Test Time no tiene el formato esperado: {test_time_raw}")
                        if line.startswith("Measured value") or line.startswith("#"):
                            current_table = "LVResult"
                            continue  
                        if line.startswith("HiPotTest") or line.startswith("Name"):
                            current_table = "HVResult"
                            continue  
                        if current_table == "LVResult":
                            if line:  
                                LVResult += line + "*\n"  
                        elif current_table == "HVResult":
                            if line:  
                                HVResult += line + "*\n" 
                    if status == "Fail":
                        failure_line = next(
                            (line for line in LVResult.splitlines() if any(keyword in line for keyword in failure_keywords)),
                            None
                        )
                        if failure_line:
                            print(f"[LVResult] Línea encontrada con keyword: {failure_line}")
                        else:
                            failure_line = next(
                                (line for line in HVResult.splitlines() if any(keyword in line for keyword in failure_keywords)),
                                None
                            )
                            if failure_line:
                                print(f"[HVResult] Línea encontrada con keyword: {failure_line}")
                            else:
                                print("[ERROR] No se encontró ninguna línea con keywords en LVResult ni HVResult.")
                        if failure_line:
                            if all(kw in failure_line.lower() for kw in ["nucleo","wire"]):
                                sFailure = "Nucleo"
                            elif all(kw in failure_line.lower() for kw in ["nucleo","4wire"]):
                                sFailure = "Alta Resistencia"
                            elif all(kw in failure_line.lower() for kw in ["malla","wire"]):
                                sFailure = "Malla"
                            elif all(kw in failure_line.lower() for kw in ["malla","4wire"]):
                                sFailure = "Alta Resistencia"
                            elif any(kw in failure_line.lower() for kw in ["tpa"]):
                                sFailure = "TPA"
                            elif any(kw in failure_line.lower() for kw in ["cpa"]):
                                sFailure = "CPA"
                            elif any(kw in failure_line.lower() for kw in ["sello","seal"]):
                                sFailure = "Sello"
                            elif any(kw in failure_line.lower() for kw in ["cover"]):
                                sFailure = "Cover"
                            elif any(kw in failure_line.lower() for kw in ["no continuity"]):
                                sFailure = "Nucleo"
                            elif any(kw in failure_line.lower() for kw in ["shortcircuit"]):
                                sFailure = "Corto"
                            elif any(kw in failure_line.lower() for kw in ["high resistance"]):
                                sFailure = "Alta Resistencia"
                            else:
                                sFailure = "Dielectrico"
                        else:
                            sFailure = "Corto"  

                    print(f"Failure: {sFailure}")

                    if LVResult:
                        print("\n[LVResult]")
                        print(LVResult)

                    if HVResult:
                        print("\n[HVResult]")
                        print(HVResult)

                    data_to_insert = {
                        "SerialNumber": serial_number,
                        "PartNumber": reference or test_name or nombre_prueba,
                        "TestDate": datetime.datetime.strptime(test_date, "%m/%d/%Y").strftime("%Y-%m-%d") if test_date else None,  # Fecha formateada
                        "TestTime": test_time,
                        "Shift": determine_shift(test_time),  # Agregar el turno calculado
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
                        pass_fail_counts[parent_folder]["Passed"] += passed
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
def emit_data():
    data = []
    for parent_folder, counts in pass_fail_counts.items():
        if parent_folder[0].isdigit():
            label = "F1"
        else:
            label = folder_labels.get(parent_folder, parent_folder)

        total_tests = counts["Passed"] + counts["Failed"]
        yield_value = (counts["Passed"] / total_tests) * 100 if total_tests > 0 else 0
        avg_cycle_time = get_average_cycle_time(parent_folder)
        last_time = last_file_times[parent_folder]
        current_state = 1  # Inicialmente, el estado es "1" (normal)

        if avg_cycle_time is None or last_time is None:
            avg_cycle_time = 0
            current_state = 0
        else:
            now = datetime.datetime.now()
            if (now - last_time).total_seconds() > avg_cycle_time :#aqui se agrega o quita el tiempo de caida a inactivo
                current_state = 0

        # Calcular performance y OEE
        oee_data = calculate_oee(parent_folder, ideal_cycle_time=10)  # Ajustar ideal_cycle_time según tu configuración

        #print(f"[INFO] Línea: {parent_folder}, OEE: {oee_data['oee']:.2%}, Performance: {oee_data['performance']:.2%}")

        data.append({
            "label": label,
            "yield": yield_value,
            "passed": counts["Passed"],
            "failed": counts["Failed"],
            "reference": counts["Reference"],
            "test_name": counts["Test Name"],
            "nombre_prueba": counts["Nombre de la prueba"],
            "avg_cycle_time": avg_cycle_time,
            "availability": oee_data["availability"],
            "performance": oee_data["performance"],
            "oee": oee_data["oee"],
            "state": current_state
        })

    data = sorted(data, key=lambda x: x["label"])
    socketio.emit('update_data', data)
    data = sorted(data, key=lambda x: x["label"])
    socketio.emit('update_data', data)
def reset_scheduler():
    reset_times = [
        datetime.time(hour=6, minute=30, second=0),
        datetime.time(hour=14, minute=30, second=0),
        datetime.time(hour=22, minute=0, second=0)
    ]
    
    while True:
        now = datetime.datetime.now()
        current_time = now.time()
        for reset_time in reset_times:
            if current_time.hour == reset_time.hour and current_time.minute == reset_time.minute:
                print(f"[INFO] Reinicio programado activado a las {reset_time}")
                execute_reset()
                time.sleep(60)  
        time.sleep(1)  

def execute_reset():
    print("[INFO] Ejecutando reseteo programado")
    global last_reset_time
    try:
        save_kpis_before_reset()
    except Exception as e:
        print(f"[ERROR] Error al guardar los KPIs antes del reinicio: {e}")

    last_reset_time = datetime.datetime.now()

    try:
        reset_counts_and_graph()
    except Exception as e:
        print(f"[ERROR] Error al reiniciar los datos y gráficos: {e}")

def save_kpis_before_reset():
    """
    Calcula y guarda los KPIs en la base de datos antes de reiniciar los datos.
    Inserta una fila por cada línea activa en pass_fail_counts.
    """
    global last_reset_time
    print(f"[DEBUG] Valor actual de last_reset_time: {last_reset_time}")  # Depuración del tiempo de reinicio
    for line, counts in pass_fail_counts.items():
        # Depuración inicial de datos por línea
        print(f"[DEBUG] Procesando línea: {line}")
        print(f"[DEBUG] Datos iniciales para la línea {line}: {counts}")

        total_tests = counts["Passed"] + counts["Failed"]
        print(f"[DEBUG] Total de pruebas para la línea {line}: {total_tests}")

        # Saltar líneas sin pruebas realizadas
        if total_tests == 0:
            print(f"[INFO] Línea {line}: No hay pruebas realizadas, omitiendo.")
            continue

        # Cálculo del tiempo transcurrido desde el último reinicio
        elapsed_time = (datetime.datetime.now() - last_reset_time).total_seconds()
        print(f"[DEBUG] Tiempo transcurrido para la línea {line}: "
        f"now={datetime.datetime.now()}, last_reset_time={last_reset_time}, "
        f"elapsed_time={elapsed_time:.2f} segundos")

        shift_durations = {
            "T1": 8 * 3600,    # 8 horas en segundos
            "T2": 7.5 * 3600,  # 7.5 horas en segundos
            "T3": 8.5 * 3600   # 8.5 horas en segundos
        }
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        current_shift = determine_shift(current_time)
        print(f"[DEBUG] Turno actual para la línea {line}: {current_shift}")

        current_shift_duration = shift_durations[current_shift]
        print(f"[DEBUG] Duración del turno {current_shift}: {current_shift_duration} segundos")

        break_time = 1800  # Tiempo de comedor en segundos
        proportional_break_time = (elapsed_time / current_shift_duration) * break_time
        print(f"[DEBUG] Tiempo proporcional de comedor para la línea {line}: {proportional_break_time:.2f} segundos")

        adjusted_elapsed_time = max(0, elapsed_time - proportional_break_time)
        print(f"[DEBUG] Tiempo ajustado para la línea {line}: {adjusted_elapsed_time:.2f} segundos")

        line_label = folder_labels.get(line, line)
        print(f"[DEBUG] Etiqueta de línea para {line}: {line_label}")

        inactive_time = max(0, pulse_history_data["inactiveTimeByLine"].get(line_label, 0))
        print(f"[DEBUG] Tiempo inactivo para la línea {line}: {inactive_time:.2f} segundos")

        operational_time = max(0, adjusted_elapsed_time - inactive_time)
        print(f"[DEBUG] Tiempo operativo para la línea {line}: {operational_time:.2f} segundos")

        # KPIs
        good_pieces = counts["Passed"]
        print(f"[DEBUG] Piezas buenas (Passed) para la línea {line}: {good_pieces}")

        total_pieces = total_tests
        print(f"[DEBUG] Piezas totales para la línea {line}: {total_pieces}")

        # Disponibilidad
        if adjusted_elapsed_time > 0:
            availability = max(0, min((adjusted_elapsed_time - inactive_time) / adjusted_elapsed_time, 1)) * 100
        else:
            availability = 0
        print(f"[DEBUG] Disponibilidad (Availability) para la línea {line}: {availability:.2f}%")

        # Rendimiento
        if good_pieces == 0 or operational_time == 0:
            performance = 0
        else:
            performance = (good_pieces * 10) / operational_time * 100  # Ciclo ideal de 10 segundos
        print(f"[DEBUG] Rendimiento (Performance) para la línea {line}: {performance:.2f}%")

        # Calidad
        if total_pieces > 0:
            quality = (good_pieces / total_pieces) * 100
        else:
            quality = 0
        print(f"[DEBUG] Calidad (Yield) para la línea {line}: {quality:.2f}%")

        # OEE
        oee = (availability / 100) * (performance / 100) * (quality / 100) * 100
        print(f"[DEBUG] OEE para la línea {line}: {oee:.2f}%")

        # Preparar datos para la base de datos
        kpi_data = {
            "shift": current_shift,                            # Turno calculado
            "FALine": line_label,                              # Nombre de la línea
            "ok": good_pieces,                                 # Piezas pasadas
            "nok": counts["Failed"],                           # Piezas fallidas
            "yield": quality,                                  # Rendimiento en porcentaje
            "operativeTime": operational_time / 60,            # Convertir de segundos a minutos
            "availability": availability,                      # Disponibilidad en porcentaje
            "performance": performance,                        # Performance en porcentaje
            "OEE": oee                                         # OEE en porcentaje
        }

        # Validar los valores calculados
        print(f"[DEBUG] Datos preparados para la base de datos para la línea {line}: {kpi_data}")

        # Insertar en la base de datos
        try:
            insert_kpi(kpi_data)
            print(f"[INFO] KPI insertado para la línea {line}: {kpi_data}")
        except Exception as e:
            print(f"[ERROR] Error al insertar KPI para la línea {line}: {e}")

def reset_counts_and_graph():
    with counts_lock:
        pass_fail_counts.clear()
        cycle_times.clear()  
        last_file_times.clear()
        # Limpia el historial del monitor de pulsos
        global pulse_history_data
        pulse_history_data = {
            "pulseHistoryX": defaultdict(list),
            "pulseHistoryY": defaultdict(list),
            "inactiveTimeByLine": defaultdict(int),
            "lastUpdateByLine": defaultdict(lambda: datetime.datetime.now().timestamp())
        }
    # Actualiza las gráficas del frontend
    emit_data()  
    socketio.emit('pulse_history_data', pulse_history_data)  
    socketio.emit('reset_activity_monitor')  

    print("[INFO] Datos y gráficos reiniciados correctamente")

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

@socketio.on('get_pulse_history')
def send_pulse_history():
    """
    Enviar el historial del monitor de pulsos al cliente.
    """
    socketio.emit('pulse_history_data', pulse_history_data)

@socketio.on('update_pulse_history')
def update_pulse_history(data):
    """
    Actualizar los datos del historial de pulsos desde el cliente.
    """
    global pulse_history_data
    pulse_history_data["pulseHistoryX"].update(data.get("pulseHistoryX", {}))
    pulse_history_data["pulseHistoryY"].update(data.get("pulseHistoryY", {}))
    pulse_history_data["inactiveTimeByLine"].update(data.get("inactiveTimeByLine", {}))
    pulse_history_data["lastUpdateByLine"].update(data.get("lastUpdateByLine", {}))


@socketio.on('reset_activity_monitor')
def reset_activity_monitor():
    """
    Restablecer el historial del monitor de pulsos.
    """
    global pulse_history_data
    pulse_history_data = {
        "pulseHistoryX": defaultdict(list),
        "pulseHistoryY": defaultdict(list),
        "inactiveTimeByLine": defaultdict(int),
        "lastUpdateByLine": defaultdict(lambda: datetime.datetime.now().timestamp())
    }
    socketio.emit('pulse_history_data', pulse_history_data)
    print("[INFO] Monitor de actividad reiniciado.")
    #######################################new
@socketio.on('connect')
def handle_connect():
    """
    Envía el estado inicial al cliente cuando se conecta.
    """
    print("[INFO] Cliente conectado. Enviando estado inicial de pulse_history_data.")
    socketio.emit('pulse_history_data', pulse_history_data)
    emit_data()  # Enviar el estado actual de producción
#####################################new
def periodic_emitter(interval=30):
    """
    Emite datos periódicamente para refrescar el gráfico, incluso si no hay nuevos archivos.
    :param interval: Tiempo en segundos entre emisiones.
    """
    while True:
        with counts_lock:
            now = datetime.datetime.now().timestamp()
            for line, last_update in pulse_history_data["lastUpdateByLine"].items():
                elapsed_time = now - last_update
                if elapsed_time > 0:  # Solo acumular si hay inactividad
                    pulse_history_data["inactiveTimeByLine"][line] += elapsed_time
                    pulse_history_data["lastUpdateByLine"][line] = now  # Actualizar el último tiempo
            emit_data()  # Actualizar el frontend
        time.sleep(interval)

@app.route('/')
def index():
    return render_template('index.html')
    

@app.route('/historico')
def historico():
    return render_template('historico.html')

@app.route('/api/historico', methods=['POST'])
def get_historico_data():
    data = request.json
    start_date = data.get('start_date')
    end_date = data.get('end_date')

    if not start_date or not end_date:
        return jsonify({"error": "Las fechas de inicio y fin son requeridas."}), 400

    results = fetch_historico_data(start_date, end_date)

    if results is None:
        return jsonify({"error": "Error al consultar datos históricos."}), 500

    return jsonify(results)

if __name__ == '__main__':
    path_to_monitor = r"\\mlxgumvwfile01\Departamentos\Fakra\Pruebas\LogFiles"
    monitor_thread = threading.Thread(target=monitor_directory, args=(path_to_monitor,))
    monitor_thread.start()
    reset_scheduler_thread = threading.Thread(target=reset_scheduler, daemon=True)
    reset_scheduler_thread.start()
    periodic_emitter_thread = threading.Thread(target=periodic_emitter, args=(30,), daemon=True)  # Intervalo de 30 segundos
    periodic_emitter_thread.start()
    socketio.run(app, host="0.0.0.0", port=5000, use_reloader=False)

