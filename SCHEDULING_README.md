# LisaAI Scheduling System

This document describes the scheduling workflow system integrated into LisaAI, which allows users to schedule appointments through a conversational interface.

## Overview

The scheduling system provides a multi-step workflow where users can:
1. Start a scheduling session
2. Answer a series of questions about their appointment
3. Review and confirm their appointment details
4. Have their appointment saved to the database

The system is designed to handle interruptions gracefully - users can ask non-scheduling questions mid-flow and return to scheduling later, with the system maintaining context.

## Architecture

### Components

1. **SchedulingService** (`app/src/modules/scheduling.py`): Core service handling scheduling logic
2. **Scheduling Endpoints** (`app/src/view.py`): REST API endpoints for scheduling operations
3. **Database Integration** (`app/src/modules/databases.py`): Appointment storage and retrieval
4. **LLM Integration** (`app/src/modules/services.py`): Intent detection and context awareness

### Data Flow

1. User sends a message to the `/generate` endpoint
2. LLM detects if the message is scheduling-related
3. If yes, the system checks for existing scheduling sessions
4. User progresses through questions via the scheduling endpoints
5. Final confirmation creates the appointment in the database

## API Endpoints

### 1. Start Scheduling Session

**POST** `/schedule/start`

Starts a new scheduling session for a user.

**Headers:**
- `userId`: User's ID (required)
- `userName`: User's name (optional, defaults to "User")

**Response:**
```json
{
  "session_id": "uuid-string",
  "current_question": "What is the reason for your appointment?",
  "step_type": "free_text",
  "options": null,
  "message": "Scheduling session started. Please answer the following question:"
}
```

### 2. Submit Answer

**POST** `/schedule/answer`

Submits an answer to the current scheduling question.

**Headers:**
- `userId`: User's ID (required)

**Body:**
```json
{
  "session_id": "uuid-string",
  "step_id": "reason",
  "answer": "Annual checkup"
}
```

**Response (Next Question):**
```json
{
  "status": "next_question",
  "session_id": "uuid-string",
  "current_question": "What type of appointment do you need?",
  "step_type": "multiple_choice",
  "options": ["Consultation", "Follow-up", "Emergency", "Routine Check", "Other"],
  "message": "Thank you! Here's the next question:"
}
```

**Response (Ready for Confirmation):**
```json
{
  "status": "ready_for_confirmation",
  "session_id": "uuid-string",
  "summary": {
    "session_id": "uuid-string",
    "answers": {...},
    "summary_text": "**Reason:** Annual checkup\n**Type:** Routine Check..."
  },
  "message": "All questions answered! Please review and confirm your appointment details."
}
```

### 3. Confirm Appointment

**POST** `/schedule/confirm`

Confirms and creates the appointment.

**Headers:**
- `userId`: User's ID (required)
- `userName`: User's name (optional)

**Body:**
```json
{
  "session_id": "uuid-string",
  "confirm": true
}
```

**Response:**
```json
{
  "status": "confirmed",
  "appointment": {
    "id": "uuid-string",
    "date": "2024-01-15",
    "time": "10:00 AM",
    "reason": "Annual checkup",
    "duration": 30,
    "notes": null
  },
  "message": "Appointment scheduled successfully!"
}
```

### 4. Cancel Session

**POST** `/schedule/cancel`

Cancels an active scheduling session.

**Headers:**
- `userId`: User's ID (required)

**Body:**
```json
{
  "session_id": "uuid-string"
}
```

**Response:**
```json
{
  "status": "cancelled",
  "message": "Scheduling session cancelled successfully."
}
```

### 5. Get Scheduling Status

**GET** `/schedule/status/{user_id}`

Gets the current scheduling status for a user.

**Response:**
```json
{
  "status": "in_progress",
  "session_id": "uuid-string",
  "current_step": "preferred_date",
  "next_question": "What is your preferred date for the appointment? (YYYY-MM-DD)",
  "step_type": "date_picker",
  "options": null,
  "message": "Scheduling session in progress."
}
```

### 6. Get User Appointments

**GET** `/appointments/{user_id}`

Retrieves all appointments for a specific user.

**Response:**
```json
{
  "user_id": 123,
  "appointments": [
    {
      "id": "uuid-string",
      "user_name": "John Doe",
      "appointment_date": "2024-01-15",
      "appointment_time": "10:00 AM",
      "reason": "Annual checkup",
      "duration_minutes": 30,
      "notes": null,
      "status": "scheduled",
      "created_at": "2024-01-10T10:00:00"
    }
  ],
  "total": 1
}
```

## Scheduling Questions Flow

The system asks the following questions in sequence:

1. **Reason** (free text): "What is the reason for your appointment?"
2. **Appointment Type** (multiple choice): "What type of appointment do you need?"
   - Options: Consultation, Follow-up, Emergency, Routine Check, Other
3. **Preferred Date** (date picker): "What is your preferred date for the appointment? (YYYY-MM-DD)"
4. **Preferred Time** (multiple choice): "What is your preferred time?"
   - Options: 09:00 AM, 10:00 AM, 11:00 AM, 02:00 PM, 03:00 PM, 04:00 PM
5. **Duration** (multiple choice): "How long do you expect the appointment to take?"
   - Options: 15 minutes, 30 minutes, 45 minutes, 1 hour, 1.5 hours
6. **Notes** (free text, optional): "Any additional notes or special requirements?"

## LLM Integration

### Intent Detection

The LLM agent includes a `check_scheduling_intent` tool that:
- Analyzes user queries for scheduling-related keywords
- Checks if the user already has an active scheduling session
- Provides context-aware suggestions for next actions

### Context Awareness

The system maintains scheduling context in Redis, allowing the LLM to:
- Detect when users are in the middle of scheduling
- Provide appropriate responses based on current session state
- Handle interruptions and resumptions seamlessly

## Database Schema

### Appointments Table

```sql
CREATE TABLE appointments (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id text NOT NULL,
    user_name text NOT NULL,
    appointment_date date NOT NULL,
    appointment_time text NOT NULL,
    reason text NOT NULL,
    duration_minutes integer DEFAULT 30,
    notes text,
    status text DEFAULT 'scheduled',
    created_at timestamp DEFAULT current_timestamp,
    updated_at timestamp DEFAULT current_timestamp
);
```

### Redis Storage

Scheduling sessions are stored in Redis with keys:
- Session data: `{PROJECT_NAME}:schedule:{user_id}:{session_id}`
- User sessions: `{PROJECT_NAME}:schedule:{user_id}:sessions`

Sessions expire after 1 hour of inactivity.

## Usage Examples

### Frontend Integration

```javascript
// Start scheduling
const startResponse = await fetch('/schedule/start', {
  method: 'POST',
  headers: {
    'userId': '123',
    'userName': 'John Doe'
  }
});

// Submit answer
const answerResponse = await fetch('/schedule/answer', {
  method: 'POST',
  headers: {
    'userId': '123'
  },
  body: JSON.stringify({
    session_id: 'session-uuid',
    step_id: 'reason',
    answer: 'Annual checkup'
  })
});

// Confirm appointment
const confirmResponse = await fetch('/schedule/confirm', {
  method: 'POST',
  headers: {
    'userId': '123',
    'userName': 'John Doe'
  },
  body: JSON.stringify({
    session_id: 'session-uuid',
    confirm: true
  })
});
```

### Error Handling

The system provides detailed error messages for common issues:
- Missing user ID
- Invalid session ID
- Session expired
- Validation failures
- Database errors

## Configuration

### Environment Variables

- `REDIS_URL`: Redis connection string
- `PROJECT_NAME`: Project identifier for Redis key namespacing

### Session TTL

Scheduling sessions expire after 1 hour (3600 seconds) of inactivity. This can be configured in the `SchedulingService` class.

## Security Considerations

- User ID validation on all endpoints
- Session isolation between users
- Input validation and sanitization
- Rate limiting (recommended for production)

## Future Enhancements

- Calendar integration
- Conflict detection
- Reminder notifications
- Recurring appointments
- Provider availability management
- Payment integration

## Troubleshooting

### Common Issues

1. **Session not found**: Check if the session has expired or was cancelled
2. **Redis connection errors**: Verify Redis URL and connectivity
3. **Database errors**: Check database connection and schema
4. **Validation failures**: Ensure answers match expected formats

### Debugging

Enable debug logging by setting the environment variable:
```bash
export log=debug
```

The system logs all scheduling operations with detailed information for debugging.

