import logging
import json
import uuid
from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional, List
from redis import asyncio as aioredis
import os
import mysql.connector
import httpx
from app.src.modules.services import simple_openai_chat
from app.src.data_types import (
    SchedulingSession, 
    SchedulingQuestion, 
    SchedulingStepType,
    Appointment
)

logger = logging.getLogger("scheduling")

class SchedulingService:
    def __init__(self):
        self.redis = None
        self.mysql_connection = None
        self.project_name = os.environ.get("PROJECT_NAME", "LisaAI")
        self.session_ttl = 3600  # 1 hour in seconds
        
        # Caching for practitioners with available slots
        self.practitioners_with_slots = {}  # {staff_id: "firstName lastName"}
        self.practitioners_cache_timestamp = None
        
    async def _get_redis(self):
        if self.redis is None:
            redis_url = os.environ.get("REDIS_URL")
            if not redis_url:
                raise Exception("REDIS_URL not configured")
            self.redis = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
        return self.redis
    
    def _get_mysql_connection(self):
        """Get MySQL connection"""
        if self.mysql_connection is None or not self.mysql_connection.is_connected():
            self.mysql_connection = mysql.connector.connect(
                host=os.environ.get("MYSQL_HOST"),
                user=os.environ.get("MYSQL_USERNAME"),
                password=os.environ.get("MYSQL_PASSWORD"),
                database=os.environ.get("MYSQL_DATABASE"),
            )
        return self.mysql_connection
    
    async def _get_session_key(self, user_id: int, session_id: str) -> str:
        return f"{self.project_name}:schedule:{user_id}:{session_id}"
    
    async def _get_user_sessions_key(self, user_id: int) -> str:
        return f"{self.project_name}:schedule:{user_id}:sessions"
    
    def _generate_session_id(self) -> str:
        return str(uuid.uuid4())
    
    def _get_practitioners_with_slots(self) -> Dict[int, str]:
        """Get all practitioners with available slots and cache the result"""
        try:
            # Check if cache is still valid (cache for 5 minutes)
            current_time = datetime.utcnow()
            if (self.practitioners_cache_timestamp and 
                (current_time - self.practitioners_cache_timestamp).total_seconds() < 300 and
                self.practitioners_with_slots):
                logger.info(f"Using cached practitioners data: {len(self.practitioners_with_slots)} practitioners")
                return self.practitioners_with_slots
            
            # Clear cache and rebuild
            self.practitioners_with_slots = {}
            
            connection = self._get_mysql_connection()
            cursor = connection.cursor(dictionary=True)
            
            # Get all staff members
            cursor.execute("SELECT id, firstName, lastName FROM staffs")
            staff_records = cursor.fetchall()
            
            # Check each practitioner for available slots
            for staff in staff_records:
                staff_id = staff['id']
                cursor.execute(
                    "SELECT COUNT(*) as slot_count FROM staffavailabilities WHERE staffId = %s",
                    (staff_id,)
                )
                result = cursor.fetchone()
                
                # Only include practitioners with available slots
                if result and result['slot_count'] > 0:
                    full_name = f"{staff['firstName']} {staff['lastName']}"
                    self.practitioners_with_slots[staff_id] = full_name
                    logger.info(f"Practitioner {staff_id} ({full_name}) has {result['slot_count']} available slots")
            
            cursor.close()
            
            # Update cache timestamp
            self.practitioners_cache_timestamp = current_time
            
            logger.info(f"Cached {len(self.practitioners_with_slots)} practitioners with available slots")
            return self.practitioners_with_slots
            
        except Exception as e:
            logger.error(f"Error getting practitioners with slots: {str(e)}")
            return {}

    def _practitioner_has_available_slots(self, staff_id: int) -> bool:
        """Check if a practitioner has any available time slots (legacy method, use cache instead)"""
        return staff_id in self.practitioners_with_slots

    def _create_numbered_options(self, options_list: List[str]) -> List[str]:
        """Convert a list of options to numbered options"""
        numbered_options = []
        for index, option in enumerate(options_list, 1):
            numbered_option = f"{index}. {option}"
            numbered_options.append(numbered_option)
        return numbered_options

    def _get_staff_options(self, filter_by_available_slots: bool = False) -> List[str]:
        """Generate numbered staff options from cached data"""
        try:
            if filter_by_available_slots:
                # Use cached practitioners with available slots
                practitioners_data = self._get_practitioners_with_slots()
                
                staff_options = []
                for index, (staff_id, full_name) in enumerate(practitioners_data.items(), 1):
                    numbered_option = f"{index}. {full_name}"
                    staff_options.append(numbered_option)
                
                logger.info(f"Generated {len(staff_options)} numbered options from cached practitioners with slots")
                return staff_options
            else:
                # For non-filtered options, get all staff (fallback for other use cases)
                connection = self._get_mysql_connection()
                cursor = connection.cursor(dictionary=True)
                
                cursor.execute("SELECT firstName, lastName FROM staffs")
                staff_records = cursor.fetchall()
                
                staff_options = []
                for index, staff in enumerate(staff_records, 1):
                    full_name = f"{staff['firstName']} {staff['lastName']}"
                    numbered_option = f"{index}. {full_name}"
                    staff_options.append(numbered_option)
                
                cursor.close()
                logger.info(f"Generated {len(staff_options)} numbered options for all staff")
                return staff_options
            
        except Exception as e:
            logger.error(f"Error generating staff options: {str(e)}")
            return []
    
    def _get_option_by_number(self, selected_number: str, available_options: List[str]) -> str:
        """Get option by number selection from any numbered list"""
        try:
            # Extract number from user input (handle cases like "1", "1.", "1. Option")
            selected_number = selected_number.strip()
            
            # Extract just the number part
            number_match = None
            for char in selected_number:
                if char.isdigit():
                    if number_match is None:
                        number_match = char
                    else:
                        number_match += char
                elif number_match is not None:
                    break
            
            if not number_match:
                logger.error(f"Could not extract number from selection: {selected_number}")
                return available_options[0] if available_options else ""
            
            option_index = int(number_match) - 1  # Convert to 0-based index
            
            # Validate index is within bounds
            if 0 <= option_index < len(available_options):
                selected_option = available_options[option_index]
                logger.info(f"Selected option by number {number_match}: {selected_option}")
                return selected_option
            else:
                logger.error(f"Invalid option number {number_match}, available range: 1-{len(available_options)}")
                return available_options[0] if available_options else ""
                
        except Exception as e:
            logger.error(f"Error getting option by number: {str(e)}")
            return available_options[0] if available_options else ""

    def _get_practitioner_by_number(self, selected_number: str, available_practitioners: List[str]) -> str:
        """Get practitioner name by number selection (legacy method)"""
        return self._get_option_by_number(selected_number, available_practitioners)
    
    def _get_staff_id_by_name(self, firstName: str, lastName: str) -> Optional[int]:
        """Get staff ID from cached data using firstName and lastName (legacy method)"""
        try:
            full_name = f"{firstName} {lastName}"
            for staff_id, cached_name in self.practitioners_with_slots.items():
                if cached_name == full_name:
                    logger.info(f"Found staff ID {staff_id} for {full_name} in cache")
                    return staff_id
            
            logger.warning(f"No staff found with name {full_name} in cache")
            return None
                
        except Exception as e:
            logger.error(f"Error fetching staff ID from cache: {str(e)}")
            return None

    def _get_patient_name_by_id(self, patient_id: int) -> Optional[str]:
        """Get patient name from database using patient userId"""
        try:
            connection = self._get_mysql_connection()
            cursor = connection.cursor(dictionary=True)
            
            cursor.execute(
                "SELECT firstName, lastName FROM patients WHERE userId = %s",
                (patient_id,)
            )
            result = cursor.fetchone()
            
            cursor.close()
            
            if result:
                full_name = f"{result['firstName']} {result['lastName']}"
                logger.info(f"Found patient name: {full_name} for userId {patient_id}")
                return full_name
            else:
                logger.warning(f"No patient found with userId {patient_id}")
                return None
                
        except Exception as e:
            logger.error(f"Error fetching patient name: {str(e)}")
            return None

    def _get_next_date_for_day(self, day_name: str) -> str:
        """Get the next coming date for the specified day name"""
        try:
            # Map day names to weekday numbers (0=Monday, 6=Sunday)
            day_map = {
                "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                "friday": 4, "saturday": 5, "sunday": 6
            }
            
            day_name_lower = day_name.lower()
            if day_name_lower not in day_map:
                logger.error(f"Invalid day name: {day_name}")
                return ""
            
            target_weekday = day_map[day_name_lower]
            today = date.today()
            
            # Calculate days until target weekday
            days_ahead = target_weekday - today.weekday()
            if days_ahead <= 0:  # Target day already passed this week
                days_ahead += 7  # Move to next week
            
            next_date = today + timedelta(days=days_ahead)
            return next_date.strftime("%Y-%m-%d")
            
        except Exception as e:
            logger.error(f"Error calculating next date for {day_name}: {str(e)}")
            return ""

    def _extract_time_from_selection(self, time_selection: str) -> tuple[str, str]:
        """Extract time and day from time selection string"""
        try:
            # Format: "1. Monday: 09:00 - 10:00" or "Monday: 09:00 - 10:00"
            # Remove numbering if present
            if '. ' in time_selection:
                time_selection = time_selection.split('. ', 1)[1]
            
            # Split by colon to get day and time parts
            if ': ' in time_selection:
                day_part, time_part = time_selection.split(': ', 1)
                day_name = day_part.strip()
                
                # Extract start time (before the dash)
                if ' - ' in time_part:
                    start_time = time_part.split(' - ')[0].strip()
                else:
                    start_time = time_part.strip()
                
                return day_name, start_time
            
            logger.error(f"Invalid time selection format: {time_selection}")
            return "", ""
            
        except Exception as e:
            logger.error(f"Error extracting time from selection: {str(e)}")
            return "", ""

    def _normalize_time(self, time_str: str) -> str:
        """Normalize time format (e.g., '09:00' -> '9:00 AM')"""
        try:
            # Parse time in HH:MM format
            time_obj = datetime.strptime(time_str, "%H:%M").time()
            
            # Convert to 12-hour format with AM/PM
            normalized = time_obj.strftime("%-I:%M %p")  # -I removes leading zero
            
            return normalized
            
        except Exception as e:
            logger.error(f"Error normalizing time {time_str}: {str(e)}")
            return time_str

    def _create_appointment_payload(self, session: SchedulingSession) -> Dict[str, Any]:
        """Convert scheduling session data to appointment payload format"""
        try:
            answers = session.answers
            
            # Ensure practitioners cache is populated
            practitioners_data = self._get_practitioners_with_slots()
            logger.info(f"Practitioners cache populated with {len(practitioners_data)} practitioners")
            
            # Extract appointment title (remove numbering if present)
            appointment_title = answers.get("appointment_title", "")
            if '. ' in appointment_title:
                appointment_title = appointment_title.split('. ', 1)[1]
            
            # Extract appointment type (remove numbering and convert to lowercase)
            appointment_type_raw = answers.get("appointment_type", "")
            if '. ' in appointment_type_raw:
                appointment_type_raw = appointment_type_raw.split('. ', 1)[1]
            appointment_type = appointment_type_raw.lower()
            
            # Extract consultant type (remove numbering and convert to lowercase with underscores)
            consultant_type_raw = answers.get("consultant_type", "")
            if '. ' in consultant_type_raw:
                consultant_type_raw = consultant_type_raw.split('. ', 1)[1]
            consultant_type = consultant_type_raw.lower().replace(" ", "_")
            
            # Extract appointment location (remove numbering and convert to lowercase)
            appointment_location_raw = answers.get("appointment_location", "")
            if '. ' in appointment_location_raw:
                appointment_location_raw = appointment_location_raw.split('. ', 1)[1]
            appointment_location = appointment_location_raw.lower()
            
            # Extract practitioner info
            practitioner_selection = answers.get("practitioner_selection", "")
            if '. ' in practitioner_selection:
                practitioner_selection = practitioner_selection.split('. ', 1)[1]
            
            logger.info(f"Looking for practitioner: '{practitioner_selection}' in cached data: {list(practitioners_data.values())}")
            
            # Get practitioner ID and name from cached data
            provider_id = None
            provider_name = ""
            for staff_id, cached_name in practitioners_data.items():
                logger.info(f"Checking practitioner: '{cached_name}' in cached data")
                if cached_name == practitioner_selection:
                    provider_id = staff_id
                    provider_name = cached_name
                    break
            
            logger.info(f"Found provider - ID: {provider_id}, Name: {provider_name}")
            
            # Extract time selection
            time_selection = answers.get("time_selection", "")
            day_name, start_time = self._extract_time_from_selection(time_selection)
            
            # Calculate appointment date
            appointment_date = self._get_next_date_for_day(day_name)
            
            # Get patient ID and name
            patient_id = self._get_patient_id_by_user_id(session.user_id)
            if not patient_id:
                logger.error(f"No patient ID found for userId {session.user_id}")
                return {}
            
            patient_name = self._get_patient_name_by_id(session.user_id) or ""
            
            # Format time properly (ensure it has seconds)
            if start_time and ":" in start_time:
                time_parts = start_time.split(":")
                if len(time_parts) == 2:  # HH:MM format
                    formatted_time = f"{start_time}:00"
                else:  # Already has seconds
                    formatted_time = start_time
            else:
                formatted_time = start_time

            # Validate required fields before creating payload
            if not provider_id:
                logger.error(f"No provider ID found for practitioner: {practitioner_selection}")
                return {}
            
            if not appointment_date:
                logger.error(f"No appointment date calculated for day: {day_name}")
                return {}
                
            if not formatted_time:
                logger.error(f"No appointment time extracted from: {time_selection}")
                return {}
            
            # Create normalized time for display
            normalized_time = ""
            if start_time and ":" in start_time:
                try:
                    normalized_time = self._normalize_time(start_time.split(":")[0] + ":" + start_time.split(":")[1])
                except:
                    normalized_time = start_time
            
            # Create the appointment payload matching the exact format from the example
            start_datetime = f"{appointment_date}T{formatted_time}.000Z"
            
            payload = {
                "appointmentTitle": appointment_title or "New Followup",
                "appointmentType": appointment_type or "individual",
                "patientId": patient_id,  # Use actual patient ID from database
                "appointmentId": "",
                "patientName": patient_name or "David Banner",
                "appointmentLocation": appointment_location or "opd-1",
                "providerId": provider_id,  # Keep as integer like in example
                "providerName": provider_name,
                "statusCode": "confirmed",
                "appointmentDate": appointment_date,
                "appointmentTime": formatted_time,
                "appointmentDuration": "",
                "reason": "",
                "consultantType": consultant_type or "face_to_face",
                "isRecurring": False,  # Boolean like in example
                "typeCode": appointment_type.replace(" ", "_") if appointment_type else "individual",
                "startRecurringDate": start_datetime,
                "endRecurringDate": None,  # null like in example
                "repeateType": "Day",
                "repeateEvery": 1,
                "isOnDay": "",
                "repeateWeek": [],  # Empty array like in example
                "monthOnDay": "",
                "monthWeekDay": [],  # Empty array like in example
                "monthWeek": [],  # Empty array like in example
                "normalizedAppointmentTime": normalized_time,
                "editSeries": False  # Boolean like in example
            }
            
            logger.info(f"Created appointment payload for user {session.user_id}: {payload}")
            return payload
            
        except Exception as e:
            logger.error(f"Error creating appointment payload: {str(e)}")
            return {}

    def _create_appointment_summary(self, payload: Dict[str, Any]) -> str:
        """Create a human-readable summary from the appointment payload"""
        try:
            summary_parts = []
            
            if payload.get("appointmentTitle"):
                summary_parts.append(f"**Appointment Title:** {payload['appointmentTitle']}")
            
            if payload.get("appointmentType"):
                summary_parts.append(f"**Appointment Type:** {payload['appointmentType'].title()}")
            
            if payload.get("consultantType"):
                consultant_type = payload['consultantType'].replace("_", " ").title()
                summary_parts.append(f"**Consultation Type:** {consultant_type}")
            
            if payload.get("appointmentLocation"):
                location = payload['appointmentLocation'].upper()
                summary_parts.append(f"**Location:** {location}")
            
            if payload.get("providerName"):
                summary_parts.append(f"**Practitioner:** {payload['providerName']}")
            
            if payload.get("appointmentDate"):
                # Format date nicely
                try:
                    from datetime import datetime
                    date_obj = datetime.strptime(payload['appointmentDate'], "%Y-%m-%d")
                    formatted_date = date_obj.strftime("%A, %B %d, %Y")
                    summary_parts.append(f"**Date:** {formatted_date}")
                except:
                    summary_parts.append(f"**Date:** {payload['appointmentDate']}")
            
            if payload.get("normalizedAppointmentTime"):
                summary_parts.append(f"**Time:** {payload['normalizedAppointmentTime']}")
            
            if payload.get("patientName"):
                summary_parts.append(f"**Patient:** {payload['patientName']}")
            
            summary_parts.append(f"**Status:** {payload.get('statusCode', 'confirmed').title()}")
            
            return "\n".join(summary_parts)
            
        except Exception as e:
            logger.error(f"Error creating appointment summary: {str(e)}")
            return "Appointment summary could not be generated."

    def _get_patient_id_by_user_id(self, user_id: int) -> Optional[int]:
        """Get patient ID from patients table using userId"""
        try:
            connection = self._get_mysql_connection()
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                "SELECT id FROM patients WHERE userId = %s",
                (user_id,)
            )
            result = cursor.fetchone()
            cursor.close()
            connection.close()
            if result:
                patient_id = result['id']
                logger.info(f"Found patient ID {patient_id} for userId {user_id}")
                return patient_id
            else:
                logger.warning(f"No patient found with userId {user_id}")
                return None
        except Exception as e:
            logger.error(f"Error fetching patient ID: {str(e)}")
            return None

    async def _create_appointment_in_database(self, appointment_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create appointment directly in MySQL database"""
        try:
            connection = self._get_mysql_connection()
            cursor = connection.cursor(dictionary=True)
            
            # First, verify that the patient exists in the patients table
            patient_id = appointment_payload.get('patientId')
            provider_id = appointment_payload.get('providerId')
            
            logger.info(f"Verifying patient ID {patient_id} and provider ID {provider_id} exist in database")
            
            # Check if patient exists
            cursor.execute("SELECT id FROM patients WHERE id = %s", (patient_id,))
            patient_exists = cursor.fetchone()
            if not patient_exists:
                logger.error(f"Patient with ID {patient_id} does not exist in patients table")
                return {
                    "success": False,
                    "error": f"Patient with ID {patient_id} not found",
                    "message": "Patient not found in database"
                }
            
            # Check if provider exists in staffs table
            cursor.execute("SELECT id FROM staffs WHERE id = %s", (provider_id,))
            provider_exists = cursor.fetchone()
            if not provider_exists:
                logger.error(f"Provider with ID {provider_id} does not exist in staffs table")
                return {
                    "success": False,
                    "error": f"Provider with ID {provider_id} not found",
                    "message": "Provider not found in database"
                }
            
            logger.info(f"Patient {patient_id} and provider {provider_id} verified to exist")
            
            # Get current timestamp
            current_time = datetime.utcnow()
            
            # Convert datetime strings to proper MySQL format
            def convert_datetime_for_mysql(datetime_str):
                """Convert ISO datetime string to MySQL datetime format"""
                if not datetime_str:
                    return None
                try:
                    # Parse the ISO datetime string and convert to MySQL format
                    dt = datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
                    return dt.strftime('%Y-%m-%d %H:%M:%S')
                except:
                    return None
            
            # Prepare the appointment data for database insertion
            appointment_data = {
                'appointmentTitle': appointment_payload.get('appointmentTitle', 'New Appointment'),
                'appointmentType': appointment_payload.get('appointmentType', 'individual'),
                'patientId': appointment_payload.get('patientId'),
                'appointmentLocation': appointment_payload.get('appointmentLocation', 'opd-1'),
                'providerId': appointment_payload.get('providerId'),
                'providerName': appointment_payload.get('providerName', ''),
                'statusCode': appointment_payload.get('statusCode', 'confirmed'),
                'appointmentDate': appointment_payload.get('appointmentDate'),
                'appointmentTime': appointment_payload.get('appointmentTime'),
                'appointmentDuration': appointment_payload.get('appointmentDuration', ''),
                'reason': appointment_payload.get('reason', ''),
                'isRecurring': appointment_payload.get('isRecurring', False),
                'typeCode': appointment_payload.get('typeCode', 'individual'),
                'isDeleted': False,
                'isActive': True,
                'createdById': appointment_payload.get('patientId'),
                'updatedById': appointment_payload.get('patientId'),
                'deletedById': None,
                'createdAt': current_time,
                'updatedAt': current_time,
                'startDateTime': convert_datetime_for_mysql(appointment_payload.get('startRecurringDate')),
                'endDateTime': convert_datetime_for_mysql(appointment_payload.get('startRecurringDate')),  # Same as start for non-recurring
                'recurringSettingId': None,
                'appointmentDate_utc': appointment_payload.get('appointmentDate'),
                'startDateTime_utc': convert_datetime_for_mysql(appointment_payload.get('startRecurringDate')),
                'endDateTime_utc': convert_datetime_for_mysql(appointment_payload.get('startRecurringDate')),
                'appointmentMode': 'in_person',
                'consultantType': appointment_payload.get('consultantType', 'face_to_face')
            }
            
            # Insert the appointment
            insert_query = """
                INSERT INTO appointments (
                    appointmentTitle, appointmentType, patientId, appointmentLocation,
                    providerId, providerName, statusCode, appointmentDate, appointmentTime,
                    appointmentDuration, reason, isRecurring, typeCode, isDeleted, isActive,
                    createdById, updatedById, deletedById, createdAt, updatedAt,
                    startDateTime, endDateTime, recurringSettingId, appointmentDate_utc,
                    startDateTime_utc, endDateTime_utc, appointmentMode, consultantType
                ) VALUES (
                    %(appointmentTitle)s, %(appointmentType)s, %(patientId)s, %(appointmentLocation)s,
                    %(providerId)s, %(providerName)s, %(statusCode)s, %(appointmentDate)s, %(appointmentTime)s,
                    %(appointmentDuration)s, %(reason)s, %(isRecurring)s, %(typeCode)s, %(isDeleted)s, %(isActive)s,
                    %(createdById)s, %(updatedById)s, %(deletedById)s, %(createdAt)s, %(updatedAt)s,
                    %(startDateTime)s, %(endDateTime)s, %(recurringSettingId)s, %(appointmentDate_utc)s,
                    %(startDateTime_utc)s, %(endDateTime_utc)s, %(appointmentMode)s, %(consultantType)s
                )
            """
            
            logger.info(f"Inserting appointment into database with data: {appointment_data}")
            logger.info(f"Converted datetime values - startDateTime: {appointment_data['startDateTime']}, startDateTime_utc: {appointment_data['startDateTime_utc']}")
            
            cursor.execute(insert_query, appointment_data)
            connection.commit()
            
            # Get the inserted appointment ID
            appointment_id = cursor.lastrowid
            
            cursor.close()
            connection.close()
            
            logger.info(f"Successfully created appointment with ID: {appointment_id}")
            
            return {
                "success": True,
                "appointment_id": appointment_id,
                "message": "Appointment created successfully in database"
            }
            
        except Exception as e:
            logger.error(f"Error creating appointment in database: {str(e)}")
            if 'cursor' in locals():
                cursor.close()
            if 'connection' in locals():
                connection.close()
            return {
                "success": False,
                "error": str(e),
                "message": "Failed to create appointment in database"
            }

    async def _test_api_connection(self) -> Dict[str, Any]:
        """Test API connection with minimal payload"""
        try:
            api_url = "https://portal.ehrlisa.com/api/v1/appointment"
            test_payload = {
                "appointmentTitle": "Test Appointment",
                "appointmentType": "individual",
                "patientId": 1,
                "appointmentId": "",
                "patientName": "Test Patient",
                "appointmentLocation": "opd-1",
                "providerId": 1,
                "providerName": "Test Provider",
                "statusCode": "confirmed",
                "appointmentDate": "2025-01-15",
                "appointmentTime": "09:00:00",
                "appointmentDuration": "",
                "reason": "",
                "consultantType": "face_to_face",
                "isRecurring": False,
                "typeCode": "individual",
                "startRecurringDate": "2025-01-15T09:00:00.000Z",
                "endRecurringDate": None,
                "repeateType": "Day",
                "repeateEvery": 1,
                "isOnDay": "",
                "repeateWeek": [],
                "monthOnDay": "",
                "monthWeekDay": [],
                "monthWeek": [],
                "normalizedAppointmentTime": "9:00 AM",
                "editSeries": False
            }
            
            logger.info(f"Testing API with minimal payload: {json.dumps(test_payload, indent=2, default=str)}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    api_url,
                    json=test_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json"
                    }
                )
                
                logger.info(f"Test API Response Status: {response.status_code}")
                logger.info(f"Test API Response Body: {response.text}")
                
                return {
                    "success": response.status_code in [200, 201],
                    "status_code": response.status_code,
                    "response": response.text
                }
                
        except Exception as e:
            logger.error(f"Test API call failed: {str(e)}")
            return {"success": False, "error": str(e)}

    async def _create_appointment_via_api(self, appointment_payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create appointment via external API"""
        try:
            api_url = "https://portal.ehrlisa.com/api/v1/appointment"
            
            logger.info(f"Creating appointment via API: {api_url}")
            logger.info(f"Full appointment payload: {json.dumps(appointment_payload, indent=2, default=str)}")
            
            # Validate payload before sending
            required_fields = ["providerId", "appointmentDate", "appointmentTime", "patientId"]
            for field in required_fields:
                if not appointment_payload.get(field):
                    logger.error(f"Missing required field in payload: {field}")
                    return {
                        "success": False,
                        "error": f"Missing required field: {field}",
                        "details": f"The appointment payload is missing the required field: {field}"
                    }
            
            # Log payload summary for debugging
            logger.info(f"Payload summary - patientId: {appointment_payload.get('patientId')} (type: {type(appointment_payload.get('patientId'))})")
            logger.info(f"Payload summary - providerId: {appointment_payload.get('providerId')} (type: {type(appointment_payload.get('providerId'))})")
            logger.info(f"Payload summary - appointmentDuration: '{appointment_payload.get('appointmentDuration')}' (type: {type(appointment_payload.get('appointmentDuration'))})")
            logger.info(f"Payload summary - isRecurring: {appointment_payload.get('isRecurring')} (type: {type(appointment_payload.get('isRecurring'))})")
            logger.info(f"Payload summary - appointmentDate: '{appointment_payload.get('appointmentDate')}' (type: {type(appointment_payload.get('appointmentDate'))})")
            logger.info(f"Payload summary - appointmentTime: '{appointment_payload.get('appointmentTime')}' (type: {type(appointment_payload.get('appointmentTime'))})")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    api_url,
                    json=appointment_payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json"
                    }
                )
                
                logger.info(f"API Response Status: {response.status_code}")
                logger.info(f"API Response Body: {response.text}")
                
                if response.status_code in [200, 201]:
                    # Success
                    response_data = response.json() if response.text else {}
                    logger.info(f"Appointment created successfully via API")
                    return {
                        "success": True,
                        "data": response_data,
                        "status_code": response.status_code
                    }
                else:
                    # Error
                    logger.error(f"API call failed with status {response.status_code}: {response.text}")
                    return {
                        "success": False,
                        "error": f"API call failed with status {response.status_code}",
                        "details": response.text,
                        "status_code": response.status_code
                    }
                    
        except httpx.TimeoutException:
            logger.error("API call timed out")
            return {
                "success": False,
                "error": "API call timed out",
                "details": "The appointment booking service did not respond in time"
            }
        except httpx.RequestError as e:
            logger.error(f"API request error: {str(e)}")
            return {
                "success": False,
                "error": "API request failed",
                "details": str(e)
            }
        except Exception as e:
            logger.error(f"Unexpected error during API call: {str(e)}")
            return {
                "success": False,
                "error": "Unexpected error",
                "details": str(e)
            }
    
    def _get_available_times(self, staff_id: int) -> List[str]:
        """Get available times from staffavailabilities table for a staff member"""
        try:
            connection = self._get_mysql_connection()
            cursor = connection.cursor(dictionary=True)
            
            cursor.execute(
                "SELECT dayId, startTime, endTime FROM staffavailabilities WHERE staffId = %s",
                (staff_id,)
            )
            availability_records = cursor.fetchall()
            
            # Map day IDs to day names (1=Monday, 2=Tuesday, etc.)
            day_names = {
                1: "Monday",
                2: "Tuesday", 
                3: "Wednesday",
                4: "Thursday",
                5: "Friday",
                6: "Saturday",
                7: "Sunday"
            }
            
            time_options = []
            for record in availability_records:
                day_id = record['dayId']
                start_time = record['startTime']
                end_time = record['endTime']
                
                # Convert day ID to day name
                day_name = day_names.get(day_id, f"Day {day_id}")
                
                # Format the time slot
                time_slot = f"{day_name}: {start_time} - {end_time}"
                time_options.append(time_slot)
            
            cursor.close()
            logger.info(f"Fetched {len(time_options)} available time slots for staff ID {staff_id}")
            return time_options
            
        except Exception as e:
            logger.error(f"Error fetching available times: {str(e)}")
            # Return default time options if database fetch fails
            return [
                "Monday: 09:00 - 10:00",
                "Monday: 10:00 - 11:00", 
                "Monday: 11:00 - 12:00",
                "Tuesday: 09:00 - 10:00",
                "Tuesday: 10:00 - 11:00"
            ]
    
    def _get_time_options_for_practitioner(self, selected_practitioner: str) -> List[str]:
        """Get available time options for a selected practitioner using cached data"""
        try:
            # Get cached practitioners data
            practitioners_data = self._get_practitioners_with_slots()
            
            # Generate numbered options from cached data to match with user selection
            available_practitioners = []
            for index, (staff_id, full_name) in enumerate(practitioners_data.items(), 1):
                numbered_option = f"{index}. {full_name}"
                available_practitioners.append(numbered_option)
            
            # Get practitioner by number selection
            matched_practitioner = self._get_practitioner_by_number(selected_practitioner, available_practitioners)
            
            # Extract name from numbered format (e.g., "1. Dr. John Smith" -> "Dr. John Smith")
            name_parts = matched_practitioner.split('. ', 1)
            if len(name_parts) == 2:
                full_name = name_parts[1]  # Get the name part after "1. "
            else:
                full_name = matched_practitioner  # Fallback if format is unexpected
            
            # Find staff ID from cached data using the full name
            staff_id = None
            for cached_staff_id, cached_name in practitioners_data.items():
                if cached_name == full_name:
                    staff_id = cached_staff_id
                    break
            
            if staff_id is None:
                logger.error(f"Could not find staff ID for {full_name} in cached data")
                return []
            
            # Get available times for this staff member
            time_options = self._get_available_times(staff_id)
            
            if not time_options:
                logger.warning(f"No time options found for {full_name}")
                return []
            
            # Convert time options to numbered format
            numbered_time_options = self._create_numbered_options(time_options)
            
            logger.info(f"Generated {len(numbered_time_options)} numbered time options for {full_name} (ID: {staff_id})")
            return numbered_time_options
            
        except Exception as e:
            logger.error(f"Error getting time options for practitioner: {str(e)}")
            return []
    
    def _get_scheduling_questions(self) -> List[SchedulingQuestion]:
        """Define the scheduling workflow questions"""
        return [
            SchedulingQuestion(
                step_id="appointment_title",
                question="What's the title for this appointment?",
                step_type=SchedulingStepType.FREE_TEXT,
                required=False
            ),
            SchedulingQuestion(
                step_id="appointment_type",
                question="What type of appointment do you need?",
                step_type=SchedulingStepType.MULTIPLE_CHOICE,
                options=self._create_numbered_options([
                        "Individual",
                        "Group",
                        "Follow Up",
                        "New Patient",
                        "Physical Exam",
                        "Sick Visit",
                        "Telehealth",
                        "Procedure",
                        "Lab Work",
                        "Consultation",
                        "Vaccine / Injection",
                        "Medication Refill",
                        "Emergency"
                ]),
                required=True
            ),
            SchedulingQuestion(
                step_id="consultant_type",
                question="What type of consultation do you need?",
                step_type=SchedulingStepType.MULTIPLE_CHOICE,
                options=self._create_numbered_options([
                        "Face to Face",
                        "Video Conference"
                ]),
                required=True
            ),
            SchedulingQuestion(
                step_id="appointment_location",
                question="where will this appointment take place?",
                step_type=SchedulingStepType.MULTIPLE_CHOICE,
                options=self._create_numbered_options([
                        "OPD-1",
                        "OPD-2",
                        "OPD-3",
                        "OPD-4"
                ]),
                required=True
            ),
            SchedulingQuestion(
                step_id="practitioner_selection",
                question="Which practitioner would you like to book an appointment with?",
                step_type=SchedulingStepType.MULTIPLE_CHOICE,
                options=[],  # Will be populated dynamically when user reaches this question
                required=True
            ),
            SchedulingQuestion(
                step_id="time_selection",
                question="What time are you looking for?",
                step_type=SchedulingStepType.MULTIPLE_CHOICE,
                options=[],  # Will be populated dynamically based on selected practitioner
                required=True
            )
        ]
    
    async def start_scheduling_session(self, user_id: int) -> SchedulingSession:
        """Start a new scheduling session"""
        try:
            redis = await self._get_redis()
            
            # Check if user already has an active session
            user_sessions_key = await self._get_user_sessions_key(user_id)
            active_sessions = await redis.smembers(user_sessions_key)
            
            # Cancel any existing active sessions
            for session_id in active_sessions:
                await self.cancel_session(user_id, session_id)
            
            # Create new session
            session_id = self._generate_session_id()
            questions = self._get_scheduling_questions()
            current_time = datetime.utcnow()
            
            # Get first question and populate options if needed
            first_question = questions[0]
            if first_question.step_id == "practitioner_selection" and not first_question.options:
                first_question.options = self._get_staff_options(filter_by_available_slots=True)
                logger.info(f"Populated practitioner options for first question: {len(first_question.options)} practitioners with available slots")
            
            session = SchedulingSession(
                session_id=session_id,
                user_id=user_id,
                current_step="appointment_title",
                completed_steps=[],
                answers={},
                next_question=first_question,
                session_start=current_time,
                last_activity=current_time,
                status="active"
            )
            
            # Store session in Redis
            session_key = await self._get_session_key(user_id, session_id)
            await redis.setex(
                session_key,
                self.session_ttl,
                json.dumps(session.dict(), default=str)
            )
            
            # Add to user's active sessions
            await redis.sadd(user_sessions_key, session_id)
            await redis.expire(user_sessions_key, self.session_ttl)
            
            logger.info(f"Started scheduling session {session_id} for user {user_id}")
            return session
            
        except Exception as e:
            logger.error(f"Error starting scheduling session: {str(e)}")
            raise
    
    async def get_current_session(self, user_id: int) -> Optional[SchedulingSession]:
        """Get the current active scheduling session for a user"""
        try:
            redis = await self._get_redis()
            user_sessions_key = await self._get_user_sessions_key(user_id)
            active_sessions = await redis.smembers(user_sessions_key)
            
            if not active_sessions:
                return None
            
            # Get the most recent session
            session_id = list(active_sessions)[-1]
            session_key = await self._get_session_key(user_id, session_id)
            session_data = await redis.get(session_key)
            
            if not session_data:
                # Clean up orphaned session reference
                await redis.srem(user_sessions_key, session_id)
                return None
            
            session_dict = json.loads(session_data)
            return SchedulingSession(**session_dict)
            
        except Exception as e:
            logger.error(f"Error getting current session: {str(e)}")
            return None
    
    async def submit_answer(self, user_id: int, session_id: str, step_id: str, answer: Any) -> SchedulingSession:
        """Submit an answer to the current scheduling question"""
        try:
            redis = await self._get_redis()
            session_key = await self._get_session_key(user_id, session_id)
            
            # Get current session
            session_data = await redis.get(session_key)
            if not session_data:
                raise Exception("Session not found or expired")
            
            session_dict = json.loads(session_data)
            session = SchedulingSession(**session_dict)
            
            # Process numbered selection if this is a multiple choice question
            processed_answer = answer
            questions = self._get_scheduling_questions()
            current_question = next((q for q in questions if q.step_id == step_id), None)
            
            # Special handling for practitioner selection - populate options if not already populated
            if current_question and current_question.step_id == "practitioner_selection" and not current_question.options:
                current_question.options = self._get_staff_options(filter_by_available_slots=True)
                logger.info(f"Populated practitioner options for current question: {len(current_question.options)} options")
            
            # Special handling for time selection - populate options if not already populated
            elif current_question and current_question.step_id == "time_selection" and not current_question.options:
                selected_practitioner = session.answers.get("practitioner_selection", "")
                if selected_practitioner:
                    time_options = self._get_time_options_for_practitioner(selected_practitioner)
                    current_question.options = time_options
                    logger.info(f"Populated time options for current question: {len(time_options)} options")
            
            if (current_question and 
                current_question.step_type == SchedulingStepType.MULTIPLE_CHOICE and 
                current_question.options):
                # Convert numbered selection to actual option
                processed_answer = self._get_option_by_number(str(answer), current_question.options)
                logger.info(f"Converted numbered selection '{answer}' to '{processed_answer}' for step {step_id}")
            
            # Validate answer
            if not await self._validate_answer(step_id, processed_answer):
                raise Exception(f"Invalid answer for step {step_id}")
            
            # Update session
            session.answers[step_id] = processed_answer
            session.completed_steps.append(step_id)
            session.last_activity = datetime.utcnow()
            
            # Move to next question
            questions = self._get_scheduling_questions()
            current_index = next((i for i, q in enumerate(questions) if q.step_id == step_id), -1)
            
            if current_index >= 0 and current_index + 1 < len(questions):
                next_question = questions[current_index + 1]
                
                # Special handling for practitioner selection question
                if next_question.step_id == "practitioner_selection":
                    # Populate practitioner options when user reaches this question
                    if not next_question.options:
                        next_question.options = self._get_staff_options(filter_by_available_slots=True)
                        logger.info(f"Populated practitioner options: {len(next_question.options)} practitioners with available slots")
                
                # Special handling for time selection question
                elif next_question.step_id == "time_selection":
                    # Get the selected practitioner from previous answers
                    selected_practitioner = session.answers.get("practitioner_selection", "")
                    if selected_practitioner:
                        # Get dynamic time options for the selected practitioner
                        time_options = self._get_time_options_for_practitioner(selected_practitioner)
                        
                        # If no time options available, go back to practitioner selection with updated list
                        if not time_options:
                            logger.warning(f"No time options available for practitioner {selected_practitioner}")
                            # Remove the practitioner selection answer and go back to practitioner selection
                            if "practitioner_selection" in session.answers:
                                del session.answers["practitioner_selection"]
                            if "practitioner_selection" in session.completed_steps:
                                session.completed_steps.remove("practitioner_selection")
                            
                            # Get updated practitioner list (without the practitioner with no slots)
                            updated_practitioners = self._get_staff_options(filter_by_available_slots=True)
                            practitioner_question = next((q for q in questions if q.step_id == "practitioner_selection"), None)
                            if practitioner_question:
                                practitioner_question.options = updated_practitioners
                                session.current_step = "practitioner_selection"
                                session.next_question = practitioner_question
                                session.status = "active"
                                
                                # Update session in Redis
                                await redis.setex(
                                    session_key,
                                    self.session_ttl,
                                    json.dumps(session.dict(), default=str)
                                )
                                
                                # Raise custom exception to indicate no slots available
                                raise Exception("No available slots for the selected practitioner. Please select another practitioner.")
                        else:
                            next_question.options = time_options
                            logger.info(f"Updated time options for practitioner {selected_practitioner}: {len(time_options)} options")
                
                session.current_step = next_question.step_id
                session.next_question = next_question
            else:
                # All questions completed
                session.current_step = "summary"
                session.next_question = None
                session.status = "ready_for_confirmation"
            
            # Update session in Redis
            await redis.setex(
                session_key,
                self.session_ttl,
                json.dumps(session.dict(), default=str)
            )
            
            logger.info(f"Updated session {session_id} with answer for step {step_id}")
            return session
            
        except Exception as e:
            logger.error(f"Error submitting answer: {str(e)}")
            raise
    
    async def _validate_answer(self, step_id: str, answer: Any) -> bool:
        """Validate the answer for a specific step"""
        try:
            if step_id == "preferred_date":
                # Validate date format and ensure it's not in the past
                try:
                    appointment_date = datetime.strptime(str(answer), "%Y-%m-%d").date()
                    if appointment_date < date.today():
                        return False
                except ValueError:
                    return False
            elif step_id == "duration":
                # Validate duration format
                valid_durations = ["15 minutes", "30 minutes", "45 minutes", "1 hour", "1.5 hours"]
                if answer not in valid_durations:
                    return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error validating answer: {str(e)}")
            return False
    
    async def get_session_summary(self, user_id: int, session_id: str) -> Dict[str, Any]:
        """Get a summary of the scheduling session for user confirmation"""
        try:
            session = await self.get_current_session(user_id)
            if not session or session.session_id != session_id:
                raise Exception("Session not found")
            
            if session.status != "ready_for_confirmation":
                raise Exception("Session not ready for confirmation")
            
            # Format the summary
            summary = {
                "session_id": session.session_id,
                "answers": session.answers,
                "summary_text": self._format_summary(session.answers)
            }
            
            return summary
            
        except Exception as e:
            logger.error(f"Error getting session summary: {str(e)}")
            raise
    
    def _format_summary(self, answers: Dict[str, Any]) -> str:
        """Format the answers into a readable summary"""
        summary_parts = []
        
        if "reason" in answers:
            summary_parts.append(f"**Reason:** {answers['reason']}")
        if "appointment_type" in answers:
            summary_parts.append(f"**Type:** {answers['appointment_type']}")
        if "preferred_date" in answers:
            summary_parts.append(f"**Date:** {answers['preferred_date']}")
        if "preferred_time" in answers:
            summary_parts.append(f"**Time:** {answers['preferred_time']}")
        if "duration" in answers:
            summary_parts.append(f"**Duration:** {answers['duration']}")
        if "notes" in answers and answers["notes"]:
            summary_parts.append(f"**Notes:** {answers['notes']}")
        
        return "\n".join(summary_parts)
    
    async def confirm_appointment(self, user_id: int, session_id: str) -> Dict[str, Any]:
        """Confirm and create the appointment, returning the appointment payload with summary"""
        try:
            session = await self.get_current_session(user_id)
            if not session or session.session_id != session_id:
                raise Exception("Session not found")
            
            if session.status != "ready_for_confirmation":
                raise Exception("Session not ready for confirmation")
            
            # Create appointment payload from session data
            appointment_payload = self._create_appointment_payload(session)
            
            if not appointment_payload:
                raise Exception("Failed to create appointment payload")
            
            # Create appointment directly in database
            logger.info("Creating appointment in MySQL database...")
            db_result = await self._create_appointment_in_database(appointment_payload)
            logger.info(f"Database insertion result: {db_result}")
            
            # Create summary from payload
            appointment_summary = self._create_appointment_summary(appointment_payload)
            
            # Add summary to payload with database result
            result = {
                "payload": appointment_payload,
                "summary": appointment_summary,
                "db_result": db_result
            }
            
            # Clear the session after successful appointment creation
            # This allows the user to start a new appointment booking session
            redis = await self._get_redis()
            session_key = await self._get_session_key(user_id, session_id)
            
            if db_result["success"]:
                # Delete the session to allow new appointment booking
                await redis.delete(session_key)
                logger.info(f"Cleared scheduling session for user {user_id} after successful appointment creation")
            else:
                # Keep session alive if appointment creation failed
                session.status = "completed"
                session.last_activity = datetime.utcnow()
                await redis.setex(
                    session_key,
                    self.session_ttl,
                    json.dumps(session.dict(), default=str)
                )
                logger.info(f"Kept scheduling session for user {user_id} due to appointment creation failure")
            
            logger.info(f"Created appointment payload and summary for session {session_id}")
            return result
            
        except Exception as e:
            logger.error(f"Error confirming appointment: {str(e)}")
            raise
    
    def _parse_duration(self, duration_str: str) -> int:
        """Parse duration string to minutes"""
        if "15 minutes" in duration_str:
            return 15
        elif "30 minutes" in duration_str:
            return 30
        elif "45 minutes" in duration_str:
            return 45
        elif "1 hour" in duration_str:
            return 60
        elif "1.5 hours" in duration_str:
            return 90
        else:
            return 30  # default
    
    async def cancel_session(self, user_id: int, session_id: str) -> bool:
        """Cancel a scheduling session"""
        try:
            redis = await self._get_redis()
            session_key = await self._get_session_key(user_id, session_id)
            user_sessions_key = await self._get_user_sessions_key(user_id)
            
            # Remove session from Redis
            await redis.delete(session_key)
            await redis.srem(user_sessions_key, session_id)
            
            logger.info(f"Cancelled scheduling session {session_id} for user {user_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error cancelling session: {str(e)}")
            return False
    
    async def cleanup_expired_sessions(self):
        """Clean up expired scheduling sessions"""
        try:
            redis = await self._get_redis()
            # This would typically be run by a background task
            # For now, we'll rely on Redis TTL
            logger.info("Cleanup of expired sessions relies on Redis TTL")
            
        except Exception as e:
            logger.error(f"Error cleaning up expired sessions: {str(e)}")
