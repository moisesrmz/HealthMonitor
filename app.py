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
from database_operations import insert_test_result
from database_operations import fetch_historico_data
from database_operations import insert_kpi
import numpy as np
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

app = Flask(__name__)
socketio = SocketIO(app)
last_reset_time = datetime.datetime.now()
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
                    # Calcular el tiempo de ciclo promedio antes de procesar el archivo

                    oee_data = calculate_oee(parent_folder)
                    adjusted_elapsed_time = oee_data["adjusted_elapsed_time"]

                    # Agregar el tiempo acumulado para nuevas líneas, si no existe
                    if parent_folder not in inactivity_accumulated_time:
                        inactivity_accumulated_time[parent_folder] = adjusted_elapsed_time
                        print(f"[INFO] Nueva línea detectada: {parent_folder}. Tiempo inicial: {adjusted_elapsed_time:.2f} segundos")
 
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
# Diccionario para almacenar tiempos de inactividad acumulados por línea
inactivity_accumulated_time = {}
inactivity_start_time = {}
flag_state = defaultdict(lambda: False)
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
        
        print("currentstate (1a impresion): ", current_state)
        ##############################################################################################################aqui entra la primera vez para activar
        if avg_cycle_time is None or last_time is None:
            avg_cycle_time = 0
            #print("flag:: ", flag)
            if not flag_state[parent_folder]:  # Si es la primera vez (flag = False)
                current_state = 1
                flag_state[parent_folder] = True  # Actualiza la bandera para futuras iteraciones
                #print(f"[INFO] Primera ejecución detectada para {parent_folder}, current_state = 1")
            else:
                current_state = 0  # Si ya pasó la primera vez, se desactiva
                #print(f"[INFO] Segunda ejecución para {parent_folder}, current_state = 0")

            
        else:
            if (now - last_time).total_seconds() > avg_cycle_time + 3:##offset para inactivar linea
                current_state = 0
                #print("currentstate (3a impresion (else): ", avg_cycle_time)

        # Lógica para acumulación de tiempo de inactividad
        #print("currentstate oficial(4a impresion (else): ", avg_cycle_time)
        if current_state == 0:
            if parent_folder not in inactivity_start_time:
                inactivity_start_time[parent_folder] = now
            else:
                # Sumar tiempo acumulado de inactividad desde la última vez que se actualizó
                elapsed_inactive_time = (now - inactivity_start_time[parent_folder]).total_seconds()
                inactivity_accumulated_time[parent_folder] = inactivity_accumulated_time.get(parent_folder, 0) + elapsed_inactive_time
                inactivity_start_time[parent_folder] = now  # Reiniciar el punto de referencia para la próxima iteración
        else:
            if parent_folder in inactivity_start_time:
                del inactivity_start_time[parent_folder]  # Eliminar la marca de inicio cuando vuelva a estar activo

        # Calcular métricas OEE
        oee_data = calculate_oee(parent_folder, ideal_cycle_time=10)

        data.append({                                       ###################esto es lo que se envia al front end
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
        })

    socketio.emit('update_data', data)

def calculate_oee(line, ideal_cycle_time=10):
    global last_reset_time
    now = datetime.datetime.now()
    elapsed_time = (now - last_reset_time).total_seconds()

    # Definir duraciones de turno en segundos
    shift_durations = {
        "T1": 8 * 3600,  # 8 horas en segundos
        "T2": 7.5 * 3600,  # 7.5 horas en segundos
        "T3": 8.5 * 3600   # 8.5 horas en segundos
    }

    current_time = now.strftime("%H:%M:%S")
    current_shift = determine_shift(current_time)
    current_shift_duration = shift_durations[current_shift]

    # Calcular tiempo proporcional de descanso
    break_time = 2100  # 35 minutos (30 min comedor + 5 min ejercicios)
    proportional_break_time = (elapsed_time / current_shift_duration) * break_time

    # Calcular tiempo ajustado restando descansos
    adjusted_elapsed_time = max(0, elapsed_time - proportional_break_time)

    line_label = folder_labels.get(line, line)

    # Obtener tiempo de inactividad acumulado
    inactive_time = inactivity_accumulated_time.get(line, 0)

    # Ajustar el tiempo de inactividad restando los descansos proporcionales
    adjusted_inactive_time = max(0, inactive_time - proportional_break_time)

    # Calcular tiempo operativo
    operational_time = max(0, adjusted_elapsed_time - adjusted_inactive_time)

    # Obtener datos de piezas
    good_pieces = pass_fail_counts[line].get("Passed", 0)
    total_pieces = good_pieces + pass_fail_counts[line].get("Failed", 0)

    # Calculo de KPI's
    availability = (adjusted_elapsed_time - adjusted_inactive_time) / adjusted_elapsed_time if adjusted_elapsed_time > 0 else 0
    performance = (good_pieces * ideal_cycle_time) / operational_time if good_pieces > 0 and operational_time > 0 else 0
    quality = good_pieces / total_pieces if total_pieces > 0 else 0
    oee = availability * performance * quality

    # Depuración de los KPI calculados
    print(f"[INFO] Línea: {line} (Label: {line_label})")
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

        # Calcular percentil superior (por defecto el 95%)
        upper_bound = np.percentile(times, upper_percentile)

        # Filtrar valores que están por debajo del percentil superior
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
            emit_data()  # Llama a la función que emite los datos
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

def reset_all_values():
    global pass_fail_counts, cycle_times, inactivity_accumulated_time, inactivity_start_time, last_reset_time, last_file_times, flag_state

    with counts_lock:
        # Reset de todas las métricas a valores iniciales
        pass_fail_counts.clear()
        cycle_times.clear()
        inactivity_accumulated_time.clear()
        inactivity_start_time.clear()
        last_file_times.clear()
        flag_state.clear()
        last_reset_time = datetime.datetime.now()

    print("[INFO] ¡Se han reseteado todas las métricas y valores acumulados!")
    zero_data = []
    for line_label in folder_labels.values():  # Iterar sobre todas las líneas configuradas
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
            "state": 0,  # Estado inactivo
            "availability": 0,
            "operational_time": 0,
            "performance": 0,
            "quality": 0,
            "oee": 0,
            "inactive_time": 0,
            "elapsed_time": 0,
        })

    # Emitir datos en ceros al frontend
    socketio.emit('update_data', zero_data)

    # Emitir el evento de reseteo al frontend para limpiar gráficos, colores, etc.
    socketio.emit('reset_data')

    # Imprimir confirmación del reseteo
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
        dashboard_url = "http://localhost:5000"  # Asegúrate de usar la URL correcta
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
            datetime.time(6, 30),   # Reinicio a las 6:30 AM
            datetime.time(14, 30),  # Reinicio a las 2w:30 PM
            datetime.time(22, 0),   # Reinicio a las 10:00 PM
        ]

        next_reset = None

        # Encontrar el próximo reinicio en el mismo día
        for reset_time in reset_times:
            today_reset = datetime.datetime.combine(now.date(), reset_time)
            if now < today_reset:
                next_reset = today_reset
                break

        # Si no hay reinicio pendiente hoy, programarlo para el primer horario del día siguiente
        if next_reset is None:
            next_reset = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), reset_times[0])

        sleep_seconds = (next_reset - now).total_seconds()
        print(f"[INFO] Próximo reinicio programado a las {next_reset.strftime('%Y-%m-%d %H:%M:%S')}")

        time.sleep(sleep_seconds)  # Esperar hasta el momento del reinicio
        print("[INFO] Capturando la pantalla antes del reinicio...")

        
        capture_screenshot()  # Capturar la pantalla antes del reinicio
        reset_all_values()


@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    path_to_monitor = r"\\mlxgumvwfile01\Departamentos\Fakra\Pruebas\LogFiles"

    monitor_thread = threading.Thread(target=monitor_directory, args=(path_to_monitor,))##hilo monitoreo
    monitor_thread.start()

    periodic_thread = threading.Thread(target=periodic_update, daemon=True)##hilo de actualizacion
    periodic_thread.start()

    reset_thread = threading.Thread(target=schedule_resets, daemon=True)##hilo de reseteo programado
    reset_thread.start()

    socketio.run(app, host="EASYTOUCH-PC", port=5000, use_reloader=False)
