import mysql.connector

# Replace these values with your actual database credentials
config = {
    'user': 'superadmin',
    'password': 'Moblisa12#$5',
    'host': '86.106.183.209',
    'database': 'lisa_ehr_practice',
    'raise_on_warnings': True
}

def get_patient_by_id(connection, patient_id):
    cursor = connection.cursor(dictionary=True)
    query = "SELECT * FROM patients WHERE id = %s"
    cursor.execute(query, (patient_id,))
    result = cursor.fetchone()
    cursor.close()
    return result

try:
    connection = mysql.connector.connect(**config)
    if connection.is_connected():
        print('Successfully connected to MySQL database')
        patient = get_patient_by_id(connection, 111)
        print('Query result:', patient)
except mysql.connector.Error as err:
    print(f'Error: {err}')
finally:
    if 'connection' in locals() and connection.is_connected():
        connection.close()
