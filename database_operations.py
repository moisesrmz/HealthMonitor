import pymysql
from datetime import datetime, date, timedelta

# Configuración de conexión a la base de datos
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "root",
    "database": "healthmonitor"
}

def insert_test_result(data):
    """
    Inserta un resultado de prueba en la tabla TestResults.

    :param data: Diccionario con los valores para insertar
    """
    query = """
        INSERT INTO TestResults (
            SerialNumber, PartNumber, TestDate, TestTime, Shift, FALine, Tester,

            TestResult, Failure, LVResult, HVResult
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    
    try:
        connection = pymysql.connect(**DB_CONFIG)
        with connection.cursor() as cursor:
            cursor.execute(query, (
                data["SerialNumber"],
                data["PartNumber"],
                data["TestDate"],
                data["TestTime"],
                data["Shift"],
                data["FALine"],
                data["Tester"],
                data["TestResult"],
                data["Failure"],
                data["LVResult"],
                data["HVResult"]
            ))
        connection.commit()
        print("[INFO] Resultado insertado correctamente en la base de datos.")
    except pymysql.MySQLError as e:
        print(f"[ERROR] Error al insertar en la base de datos: {e}")
    finally:
        connection.close()

def insert_kpi(data):
    """
    Inserta un registro de KPI en la tabla kpis.

    :param data: Diccionario con los valores para insertar
    """
    query = """
        INSERT INTO kpis (`shift`, `FALine`, `ok`, `nok`, `yield`, `operativeTime`, `availability`, `performance`, `OEE`)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    
    try:
        connection = pymysql.connect(**DB_CONFIG)
        with connection.cursor() as cursor:
            print(f"[DEBUG] Query: {query}")
            print(f"[DEBUG] Data: {data}")
            cursor.execute(query, (
                data["shift"],
                data["FALine"],
                data["ok"],
                data["nok"],
                data["yield"],
                data["operativeTime"],
                data["availability"],
                data["performance"],
                data["OEE"]
            ))
        connection.commit()
        print("[INFO] KPI insertado correctamente en la base de datos.")
    except pymysql.MySQLError as e:
        print(f"[ERROR] Error al insertar KPI en la base de datos: {e}")
    finally:
        connection.close()



# Configuración de conexión a la base de datos
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "root",
    "database": "healthmonitor"
}

def fetch_historico_data(start_date, end_date, part_number=None):
    """
    Consulta los datos históricos entre dos fechas en la tabla TestResults.
    Si se proporciona un part_number, filtra también por coincidencia parcial.

    :param start_date: Fecha de inicio en formato "YYYY-MM-DD"
    :param end_date: Fecha de fin en formato "YYYY-MM-DD"
    :param part_number: (opcional) Parte a filtrar
    :return: Lista de resultados
    """
    base_query = """
        SELECT SerialNumber, PartNumber, TestDate, TestTime, Shift, FALine, Tester, TestResult, Failure, LVResult, HVResult
        FROM TestResults
        WHERE TestDate BETWEEN %s AND %s
    """
    params = [start_date, end_date]

    if part_number:
        base_query += " AND PartNumber LIKE %s"
        params.append(f"%{part_number}%")

    try:
        connection = pymysql.connect(**DB_CONFIG)
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(base_query, params)
            results = cursor.fetchall()

            # Formatear tipos especiales a texto compatible JSON
            for row in results:
                for key, value in row.items():
                    if isinstance(value, timedelta):
                        row[key] = str(value)
                    elif isinstance(value, date):
                        row[key] = value.strftime("%Y-%m-%d")
                    elif isinstance(value, datetime):
                        row[key] = value.strftime("%Y-%m-%d %H:%M:%S")
            return results

    except pymysql.MySQLError as e:
        print(f"[ERROR] Error al consultar datos históricos: {e}")
        return None
    finally:
        connection.close()
