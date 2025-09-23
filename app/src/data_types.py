from typing import Union, Optional, List, Dict, Any
from pydantic import BaseModel
import uuid
from datetime import datetime, date
from enum import Enum


class Rating(BaseModel):
    rating: Union[int, None]
    query_id: Union[str, None]
    review: str


class Prompt(BaseModel):
    llm_model: str
    persona: str
    glossary: str
    tone: str
    response_length: str
    content: str


class Query(BaseModel):
    """
    chatbot query input
    """
    input: str
    chat_history: Optional[list] = None
    convo_id: Union[str, None]
    language: Optional[str] = None
    current_page: Optional[str] = None
    userId: Optional[str] = None  # Deprecated: Use authentication instead


class SmartSearchInput(BaseModel):
    """
    Smart search input
    """
    filter: str
    search: str


class PptInput(BaseModel):
    """
    Ppt input
    """
    vertical: str
    domains: list
    desc: str


class CaseStudyMakerInput(BaseModel):
    """
    Case Study Maker input
    """
    client: str


class Analysis(BaseModel):
    """analysis data"""
    number_of_queries: int


class SignUp(BaseModel):
    email: str
    name: str
    password: str
    designation: str
    department: str


class Login(BaseModel):
    email: str
    password: str


class Conversation(BaseModel):
    conversation_id: str


class EmailInput(BaseModel):
    thread_of_emails: list


class EmailInputUserData(BaseModel):
    userInput: str
    role: str
    tone: str
    emailLength: str
    thread_of_emails: list


class GoogleSignup(BaseModel):
    email: str
    name: str
    uid: str


class ChangeRole(BaseModel):
    email: str
    role: str


class HRBotInput(BaseModel):
    input: str
    chat_history: list
    convo_id: Union[str, None]


class IngestFile(BaseModel):
    key: str
    data_type: str
    bucket_name: str
    event_type: str


class Delete(BaseModel):
    type: str


class DeleteFile(BaseModel):
    file_name: str


class ActiveFile(BaseModel):
    file_name: str
    active: bool


class Template(BaseModel):
    template_name: str
    attributes: list


class HRRefrenceInput(BaseModel):
    input: str


class UpdateUser(BaseModel):
    email: Union[str, None]
    name: Union[str, None]
    designation: Union[str, None]
    department: Union[str, None]
    role: Union[str, None]
    time: Union[str, None]


class TreatmentPlanRequest(BaseModel):
    patient_id: int
    organization_id: str
    doctor_name: Optional[str] = None
    doctor_id: Optional[str] = None
    reference_number: Optional[str] = None
    language: Optional[str] = None


class SchedulingStepType(str, Enum):
    FREE_TEXT = "free_text"
    MULTIPLE_CHOICE = "multiple_choice"
    DATE_PICKER = "date_picker"
    TIME_PICKER = "time_picker"

class SchedulingQuestion(BaseModel):
    step_id: str
    question: str
    step_type: SchedulingStepType
    options: Optional[List[str]] = None
    validation_rules: Optional[Dict[str, Any]] = None
    required: bool = True

class SchedulingSession(BaseModel):
    session_id: str
    user_id: int
    current_step: str
    completed_steps: List[str]
    answers: Dict[str, Any]
    next_question: Optional[SchedulingQuestion] = None
    session_start: datetime
    last_activity: datetime
    status: str = "active"  # active, completed, cancelled

class SchedulingAnswer(BaseModel):
    session_id: str
    step_id: str
    answer: Any
    user_id: int

class Appointment(BaseModel):
    user_id: int
    user_name: str
    appointment_date: date
    appointment_time: str
    reason: str
    duration_minutes: int = 30
    notes: Optional[str] = None
    status: str = "scheduled"  # scheduled, confirmed, cancelled, completed

class SchedulingStartRequest(BaseModel):
    user_id: int
    user_name: str

class SchedulingConfirmRequest(BaseModel):
    session_id: str
    user_id: int
    confirm: bool

class SchedulingCancelRequest(BaseModel):
    session_id: str
    user_id: int

